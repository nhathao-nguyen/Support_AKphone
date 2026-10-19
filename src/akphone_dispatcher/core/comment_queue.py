"""Persistent queue of posts waiting for their delayed comment.

Life of a job::

    fed ──(Done flag removed)──► posted ──(new reel seen on the page)──► linked
    linked ──(views >= threshold)──► marked ──(CMT flag removed)──► done
    marked ──(flag still there after timeout)──► failed
    posted / linked ──(too old)──► expired

The comment text is captured at feed time, because the payload archive holding the
original comment file is cleaned at the end of each round.

Finished jobs (done / failed / expired) stay visible for a while, then move to one
JSON-lines file per day in ``comment_archive/`` so the live queue stays small.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from akphone_dispatcher.utils.logger import logger

FED = "fed"
POSTED = "posted"
LINKED = "linked"
MARKED = "marked"
DONE = "done"
FAILED = "failed"
EXPIRED = "expired"

ACTIVE_STATES = (FED, POSTED, LINKED, MARKED)
MAX_SEEN_URLS = 500

STATE_LABELS = {
    FED: "Đã nạp, chờ đăng",
    POSTED: "Đã đăng, chờ thấy link",
    LINKED: "Đang theo dõi view",
    MARKED: "Đã đẩy comment, chờ script",
    DONE: "✅ Đã comment",
    FAILED: "❌ Comment lỗi",
    EXPIRED: "⌛ Hết hạn",
}


class CommentQueue:
    """Thread-safe job list persisted to a JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.archive_dir = self.path.parent / "comment_archive"
        self.lock = threading.RLock()
        self.jobs: List[Dict[str, Any]] = []
        # fanpage name -> {"baseline_at": float, "urls": [url, ...] newest first}
        self.seen: Dict[str, Dict[str, Any]] = {}
        self.load()

    # ---------- persistence ----------
    def load(self):
        with self.lock:
            if not self.path.is_file():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.jobs = data.get("jobs", [])
                self.seen = data.get("seen", {})
            except Exception as e:
                logger.error(f"Không đọc được hàng đợi comment {self.path}: {e}")

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"jobs": self.jobs, "seen": self.seen}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)

    # ---------- events from the post feeder ----------
    def add_fed(
        self,
        serial: str,
        page_id: str,
        fanpage_name: str,
        post_num: int,
        video_name: str,
        comment_text: str,
    ):
        """Register a post just cut into the running dir (replaces an unposted one)."""
        with self.lock:
            self.jobs = [
                j
                for j in self.jobs
                if not (j["serial"] == serial and j["page_id"] == page_id and j["state"] == FED)
            ]
            self.jobs.append(
                {
                    "uid": uuid.uuid4().hex[:12],
                    "serial": serial,
                    "page_id": page_id,
                    "fanpage_name": fanpage_name,
                    "post_num": post_num,
                    "video_name": video_name,
                    "comment_text": comment_text,
                    "state": FED,
                    "fed_at": time.time(),
                    "posted_at": None,
                    "post_url": "",
                    "link_attempts": 0,
                    "next_link_at": None,
                    "views": None,
                    "views_at": None,
                    "marked_at": None,
                    "flag_created": False,
                    "finished_at": None,
                    "note": "",
                }
            )
            self.save()

    def mark_posted(self, serial: str, page_id: str) -> Optional[Dict[str, Any]]:
        """The page's Done flag vanished: its fed post is now live."""
        with self.lock:
            for job in self.jobs:
                if job["serial"] == serial and job["page_id"] == page_id and job["state"] == FED:
                    job["state"] = POSTED
                    job["posted_at"] = time.time()
                    self.save()
                    return job
            return None

    # ---------- queries ----------
    def active_jobs(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [j for j in self.jobs if j["state"] in ACTIVE_STATES]

    def snapshot(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(j) for j in self.jobs]

    def has_marked(self, serial: str, page_id: str) -> bool:
        with self.lock:
            return any(
                j["serial"] == serial and j["page_id"] == page_id and j["state"] == MARKED
                for j in self.jobs
            )

    def tracked_urls(self, fanpage_name: str) -> Set[str]:
        """Links whose views are still needed on this page."""
        with self.lock:
            return {
                j["post_url"]
                for j in self.jobs
                if j["fanpage_name"] == fanpage_name and j["state"] == LINKED
            }

    def update(self, job: Dict[str, Any], **fields):
        with self.lock:
            job.update(fields)
            self.save()

    # ---------- archive ----------
    def archive_finished(self, older_than_s: float = 0, now: Optional[float] = None) -> int:
        """Move finished jobs (finished more than ``older_than_s`` ago) to the archive."""
        now = time.time() if now is None else now
        with self.lock:
            keep, moved = [], []
            for job in self.jobs:
                finished = job["state"] not in ACTIVE_STATES
                if finished and now - (job.get("finished_at") or 0) >= older_than_s:
                    moved.append(job)
                else:
                    keep.append(job)
            if not moved:
                return 0
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            by_day: Dict[str, List[Dict[str, Any]]] = {}
            for job in moved:
                day = datetime.fromtimestamp(job.get("finished_at") or now).strftime("%Y-%m-%d")
                by_day.setdefault(day, []).append(job)
            for day, jobs in by_day.items():
                with (self.archive_dir / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
                    for job in jobs:
                        handle.write(json.dumps(job, ensure_ascii=False) + "\n")
            self.jobs = keep
            self.save()
            return len(moved)

    def clear_finished(self) -> int:
        return self.archive_finished(older_than_s=0)

    # ---------- link matching ----------
    def needs_baseline(self, fanpage_name: str) -> bool:
        with self.lock:
            return fanpage_name not in self.seen

    def apply_scan(
        self, fanpage_name: str, scanned: List[Tuple[str, Optional[int]]]
    ) -> List[Dict[str, Any]]:
        """Use one scan of a page's reels (newest first) to assign links and refresh views.

        The first scan of a page only records a baseline: every reel already there is
        an old post. After that, new reels are the ones listed BEFORE the newest reel
        already known, so scrolling deeper never turns an old reel into a "new" one.
        New reels go to posted jobs without a link, oldest post to oldest new reel.
        Returns the jobs that just got a link.
        """
        with self.lock:
            now = time.time()
            views_by_url = {url: views for url, views in scanned}
            page_seen = self.seen.get(fanpage_name)
            newly_linked: List[Dict[str, Any]] = []

            if page_seen is None:
                self.seen[fanpage_name] = {
                    "baseline_at": now,
                    "urls": [url for url, _ in scanned][:MAX_SEEN_URLS],
                }
                for job in self.jobs:
                    if job["fanpage_name"] == fanpage_name and job["state"] == POSTED:
                        job["note"] = "Đăng trước lần quét mốc; không xác định được link"
                        job["state"] = EXPIRED
                        job["finished_at"] = now
                self.save()
                return newly_linked

            known = set(page_seen["urls"])
            new_urls: List[str] = []
            hit_known = not known  # a page that had no reel at baseline: all are new
            for url, _ in scanned:
                if url in known:
                    hit_known = True
                    break
                new_urls.append(url)
            if not hit_known:
                # None of the known reels is on screen: cannot tell new from old.
                if scanned:
                    logger.warning(
                        f"[Comment] {fanpage_name}: không thấy reel mốc nào trong {len(scanned)} "
                        "reel vừa đọc; tạm chưa ghép link."
                    )
                new_urls = []

            waiting = sorted(
                (
                    j
                    for j in self.jobs
                    if j["fanpage_name"] == fanpage_name and j["state"] == POSTED
                ),
                key=lambda j: j["posted_at"] or 0,
            )
            if new_urls and len(new_urls) != len(waiting):
                logger.warning(
                    f"[Comment] {fanpage_name}: thấy {len(new_urls)} reel mới nhưng có "
                    f"{len(waiting)} bài chờ link; ghép theo thứ tự đăng."
                )
            # Oldest new reel first; when there are more reels than posts, keep the newest.
            candidates = list(reversed(new_urls))[-len(waiting):] if waiting else []
            for job, url in zip(waiting, candidates):
                job["post_url"] = url
                job["state"] = LINKED
                job["note"] = ""
                newly_linked.append(job)

            page_seen["urls"] = (new_urls + page_seen["urls"])[:MAX_SEEN_URLS]

            for job in self.jobs:
                if job["fanpage_name"] == fanpage_name and job["state"] == LINKED:
                    views = views_by_url.get(job["post_url"])
                    if views is not None:
                        job["views"] = views
                        job["views_at"] = now
            self.save()
            return newly_linked
