"""Persistent queue of posts waiting for their delayed comment.

Life of a job::

    fed ──(Done flag removed)──► posted ──(new reel verified: same caption, created
                                           around the Done flag removal)──► linked
    linked ──(views >= threshold)──► marked ──(CMT flag removed)──► done
    marked ──(flag still there after timeout)──► failed
    posted / linked ──(too old)──► expired

Re-post (by hand, after the day's auto run): a post that never showed up as a reel
(still no link) goes to ``repost``: "queued" waits for the page's slot, "staged" has its
video + caption back in the running dir and a new Done flag; when the user runs the
script in AKPhoneS and the flag disappears, it is ``posted`` again. A failed comment can
be sent back to ``linked`` to be pushed again.

The comment and caption texts are captured at feed time, because the payload archive
holding the original files is cleaned at the end of each round.

New reels seen on a page are only *candidates*: a separate reader opens each one, reads
its original caption and creation time, and a candidate gets a post only when both match
(``match_candidates``). A reel that matches no post (posted by hand...) is never linked.

Finished jobs (done / failed / expired) stay visible for a while, then move to one
JSON-lines file per day in ``comment_archive/`` so the live queue stays small.
"""

from contextlib import contextmanager
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from akphone_dispatcher.core.reel_match import (
    captions_match,
    describe_delta,
    time_delta,
    time_matches,
)
from akphone_dispatcher.utils.logger import logger

FED = "fed"
POSTED = "posted"
LINKED = "linked"
MARKED = "marked"
DONE = "done"
FAILED = "failed"
EXPIRED = "expired"
REPOST = "repost"

ACTIVE_STATES = (FED, POSTED, LINKED, MARKED, REPOST)


def posted_delta(job: Dict[str, Any], created_at: Optional[float]) -> Optional[float]:
    """Seconds from the job's posting time to a reel's creation. A post that went live
    while the tool was away has a window (posted_from .. posted_at): 0 inside it."""
    start = job.get("posted_from")
    if start is None or created_at is None or job.get("posted_at") is None:
        return time_delta(created_at, job.get("posted_at"))
    if created_at < start:
        return float(created_at) - float(start)
    return max(0.0, float(created_at) - float(job["posted_at"]))

# new reel seen on a page, before it is proven to be one of our posts
CAND_NEW = "new"  # waiting to be opened
CAND_READ = "read"  # caption + time read, no post matched yet
CAND_MATCHED = "matched"
CAND_FOREIGN = "foreign"  # matched no post in time: not from the tool
CAND_UNREADABLE = "unreadable"
CANDIDATE_WAIT_S = 6 * 3600  # a read reel waits this long for its post before "foreign"
CANDIDATE_KEEP_S = 3 * 86400
BACKUP_EVERY_S = 600  # comment_queue.json.bak refreshed at most this often
REPOSTABLE_STATES = (FED, POSTED, EXPIRED)  # only while no reel link was found
REPOST_QUEUED = "queued"
REPOST_STAGED = "staged"
MAX_SEEN_URLS = 500

STATE_LABELS = {
    FED: "Đã nạp, chờ đăng",
    POSTED: "Đã đăng, chờ thấy link",
    LINKED: "Đang theo dõi view",
    MARKED: "Đã đẩy comment, chờ script",
    DONE: "✅ Đã comment",
    FAILED: "❌ Comment lỗi",
    EXPIRED: "⌛ Hết hạn",
    REPOST: "🔁 Đăng lại",
}


def job_caption(job: Dict[str, Any]) -> str:
    """The post's caption: stored at feed time, or read back from its archived file
    (jobs queued before captions were stored)."""
    if job.get("caption_text"):
        return job["caption_text"]
    path = Path(job.get("caption_raw") or "")
    if job.get("caption_raw") and path.is_file():
        try:
            return " ".join(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            return ""
    return ""


class CommentQueue:
    """Thread-safe job list persisted to a JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.archive_dir = self.path.parent / "comment_archive"
        self.lock = threading.RLock()
        self.jobs: List[Dict[str, Any]] = []
        # Fanpage link -> {"baseline_at": float, "urls": [reel url, ...] newest first}
        self.seen: Dict[str, Dict[str, Any]] = {}
        self._batch_depth = 0
        self._dirty = False
        self._backup_at = 0.0
        self.load()

    # ---------- persistence ----------
    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def load(self):
        """Read the queue; a damaged file falls back to the backup (at most
        BACKUP_EVERY_S old) instead of starting empty and overwriting everything."""
        with self.lock:
            for source in (self.path, self.backup_path):
                if not source.is_file():
                    continue
                try:
                    data = json.loads(source.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.error(f"Không đọc được hàng đợi comment {source}: {e}")
                    continue
                self.jobs = data.get("jobs", [])
                self.seen = data.get("seen", {})
                if source == self.backup_path:
                    logger.error(
                        f"[Comment] Hàng đợi comment hỏng/mất: đã dùng bản dự phòng {source.name} "
                        f"(có thể thiếu thay đổi của tối đa {BACKUP_EVERY_S // 60} phút cuối)."
                    )
                    broken = self.path.with_name(self.path.name + ".broken")
                    if self.path.is_file():
                        os.replace(self.path, broken)
                    self.save()
                return

    @contextmanager
    def batch(self):
        """Several updates, one write (the file can hold thousands of jobs)."""
        with self.lock:
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
                if self._batch_depth == 0 and self._dirty:
                    self.save()

    def save(self):
        with self.lock:
            if self._batch_depth:
                self._dirty = True
                return
            self._dirty = False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"jobs": self.jobs, "seen": self.seen}, ensure_ascii=False))
                handle.flush()
                os.fsync(handle.fileno())  # a power cut leaves the old or the new file, whole
            now = time.time()
            if self.path.is_file() and now - self._backup_at >= BACKUP_EVERY_S:
                shutil.copy2(self.path, self.backup_path)
                self._backup_at = now
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
        page_url: str = "",
        archive: Optional[Dict[str, str]] = None,
        caption_text: str = "",
    ):
        """Register a post just cut into the running dir.

        ``page_url`` is a snapshot of the Fanpage link at feed time: replacing the page
        later (new name + new link) does not move this post to the new page.
        ``archive`` = payload-store copy {"dir", "video", "caption"} used to re-post it.

        A post of the same page still waiting to be posted can only be here after a
        re-init (e.g. switching to the next day's store): its source already left the
        store, so instead of dropping it silently it becomes "expired, never posted" and
        stays re-postable from the GUI.
        """
        archive = archive or {}
        with self.lock:
            now = time.time()
            for j in self.jobs:
                if j["serial"] == serial and j["page_id"] == page_id and j["state"] == FED:
                    j.update(
                        state=EXPIRED,
                        finished_at=now,
                        note="Chưa đăng được, bị thay khi khởi tạo lại: bấm Đăng lại nếu cần",
                    )
                    logger.warning(
                        f"[Comment] [{page_id}] bài {j['post_num']} ({j['video_name']}) chưa đăng "
                        "được đã bị thay khi khởi tạo lại; vẫn có thể Đăng lại ở Tab 5."
                    )
            self.jobs.append(
                {
                    "uid": uuid.uuid4().hex[:12],
                    "serial": serial,
                    "page_id": page_id,
                    "fanpage_name": fanpage_name,
                    "page_url": page_url.strip(),
                    "post_num": post_num,
                    "video_name": video_name,
                    "comment_text": comment_text,
                    "caption_text": caption_text,
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
                    "archive_dir": archive.get("dir", ""),
                    "video_raw": archive.get("video", ""),
                    "caption_raw": archive.get("caption", ""),
                    "repost_state": "",
                    "repost_count": 0,
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

    def mark_posted_away(
        self, serial: str, page_id: str, since: float, until: float
    ) -> Optional[Dict[str, Any]]:
        """The page's Done flag vanished while the tool was away (between ``since``, last
        seen, and ``until``): the post went live at an unknown time in that window."""
        with self.lock:
            for job in self.jobs:
                if job["serial"] == serial and job["page_id"] == page_id and job["state"] == FED:
                    job.update(
                        state=POSTED,
                        posted_at=until,
                        posted_from=min(since, until),
                        note=(
                            "Đăng lúc tool tắt (" + datetime.fromtimestamp(min(since, until)).strftime("%d/%m %H:%M")
                            + " – " + datetime.fromtimestamp(until).strftime("%d/%m %H:%M")
                            + "): khớp reel theo caption trong khoảng này"
                        ),
                    )
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

    def protected_archives(self) -> Set[str]:
        """Payload copies to keep: posts with no reel link yet may need a re-post."""
        with self.lock:
            return {
                j["archive_dir"]
                for j in self.jobs
                if j.get("archive_dir") and not j.get("post_url")
            }

    def can_repost(self, job: Dict[str, Any]) -> bool:
        return (
            job["state"] in REPOSTABLE_STATES
            and not job.get("post_url")
            and bool(job.get("video_raw"))
            and Path(job["video_raw"]).is_file()
        )

    def queue_repost(self, uids: Set[str]) -> List[Dict[str, Any]]:
        """Mark posts for a re-post; the tracker stages them one per page."""
        with self.lock:
            queued = []
            for job in self.jobs:
                if job["uid"] in uids and self.can_repost(job):
                    job.update(
                        state=REPOST,
                        repost_state=REPOST_QUEUED,
                        finished_at=None,
                        note="Chờ nạp lại để đăng lại",
                    )
                    queued.append(job)
            if queued:
                self.save()
            return queued

    def retry_comment(self, uids: Set[str]) -> List[Dict[str, Any]]:
        """Send failed comments back to 'linked' so they are pushed again."""
        with self.lock:
            retried = []
            for job in self.jobs:
                if job["uid"] in uids and job["state"] == FAILED and job.get("post_url"):
                    job.update(
                        state=LINKED,
                        marked_at=None,
                        flag_created=False,
                        finished_at=None,
                        note="Comment lại",
                    )
                    retried.append(job)
            if retried:
                self.save()
            return retried

    def tracked_urls(self, page_url: str) -> Set[str]:
        """Links whose views are still needed on this page."""
        with self.lock:
            return {
                j["post_url"]
                for j in self.jobs
                if j.get("page_url") == page_url and j["state"] == LINKED
            }

    def update(self, job: Dict[str, Any], **fields):
        with self.lock:
            job.update(fields)
            self.save()

    def set_page_url(self, serial: str, page_id: str, fanpage_name: str, page_url: str) -> int:
        """Fill in / correct the Fanpage link of the active jobs of ONE page.

        Only jobs of the same Fanpage name change: when a page_id now holds a new page
        (new name), the older jobs keep tracking the old page with the old link.
        Returns how many jobs changed.
        """
        page_url = page_url.strip()
        with self.lock:
            changed = 0
            for job in self.jobs:
                if (
                    job["state"] in ACTIVE_STATES
                    and job["serial"] == serial
                    and job["page_id"] == page_id
                    and job["fanpage_name"] == fanpage_name
                    and job.get("page_url", "") != page_url
                ):
                    job["page_url"] = page_url
                    changed += 1
            if changed:
                self.save()
            return changed

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

    def load_archived(self, days: int = 7, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Finished jobs archived during the last ``days`` days (read-only history)."""
        now = time.time() if now is None else now
        jobs: List[Dict[str, Any]] = []
        for back in range(days):
            day = datetime.fromtimestamp(now - back * 86400).strftime("%Y-%m-%d")
            path = self.archive_dir / f"{day}.jsonl"
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    jobs.append(json.loads(line))
                except ValueError:
                    continue
        return jobs

    def clear_finished(self) -> int:
        return self.archive_finished(older_than_s=0)

    # ---------- link matching ----------
    def needs_baseline(self, page_url: str) -> bool:
        with self.lock:
            return page_url not in self.seen

    def apply_scan(
        self, page_url: str, scanned: List[Tuple[str, Optional[int]]], now: Optional[float] = None
    ) -> List[str]:
        """Use one scan of a page's reels (newest first): find new reels, refresh views.

        The first scan of a page only records a baseline: every reel already there is
        an old post. After that, new reels are the ones listed BEFORE the newest reel
        already known, so scrolling deeper never turns an old reel into a "new" one.
        New reels become candidates waiting for verification (never linked by order).
        Returns the new candidate urls.
        """
        with self.lock:
            now = time.time() if now is None else now
            views_by_url = {url: views for url, views in scanned}
            page_seen = self.seen.get(page_url)

            if page_seen is None:
                self.seen[page_url] = {
                    "baseline_at": now,
                    "urls": [url for url, _ in scanned][:MAX_SEEN_URLS],
                    "candidates": {},
                }
                for job in self.jobs:
                    if job.get("page_url") == page_url and job["state"] == POSTED:
                        job["note"] = "Đăng trước lần quét mốc; không xác định được link"
                        job["state"] = EXPIRED
                        job["finished_at"] = now
                self.save()
                return []

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
                        f"[Comment] {page_url}: không thấy reel mốc nào trong {len(scanned)} "
                        "reel vừa đọc; tạm chưa xét reel mới."
                    )
                new_urls = []

            candidates = page_seen.setdefault("candidates", {})
            for url in new_urls:
                candidates.setdefault(
                    url,
                    {"found_at": now, "state": CAND_NEW, "tries": 0, "next_try": 0,
                     "caption": "", "created_at": None, "views": None, "job": "", "note": ""},
                )
            for url, cand in candidates.items():
                if views_by_url.get(url) is not None:
                    cand["views"] = views_by_url[url]
            self._prune_candidates(candidates, now)
            page_seen["urls"] = (new_urls + page_seen["urls"])[:MAX_SEEN_URLS]

            for job in self.jobs:
                if job.get("page_url") == page_url and job["state"] == LINKED:
                    views = views_by_url.get(job["post_url"])
                    if views is not None:
                        job["views"] = views
                        job["views_at"] = now
            self.save()
            return new_urls

    # ---------- candidates (new reels waiting for verification) ----------
    @staticmethod
    def _prune_candidates(candidates: Dict[str, Dict[str, Any]], now: float):
        for url in [u for u, c in candidates.items()
                    if c["state"] in (CAND_MATCHED, CAND_FOREIGN, CAND_UNREADABLE)
                    and now - c["found_at"] > CANDIDATE_KEEP_S]:
            del candidates[url]

    def pending_reads(self, now: Optional[float] = None) -> List[Tuple[str, str]]:
        """(page url, reel url) still to open, oldest first."""
        now = time.time() if now is None else now
        with self.lock:
            due = [
                (cand["found_at"], page, url)
                for page, seen in self.seen.items()
                for url, cand in seen.get("candidates", {}).items()
                if cand["state"] == CAND_NEW and cand.get("next_try", 0) <= now
            ]
            return [(page, url) for _, page, url in sorted(due)]

    def accept_suggestions(self, uids: Set[str], now: Optional[float] = None) -> List[Dict[str, Any]]:
        """The user confirmed the suggested reel of these posted jobs: link them."""
        now = time.time() if now is None else now
        with self.lock:
            accepted = []
            taken = {j.get("post_url") for j in self.jobs if j.get("post_url")}
            for job in self.jobs:
                url = job.get("suggested_url")
                if job["uid"] not in uids or job["state"] != POSTED or not url or url in taken:
                    continue
                cand = self.seen.get(job.get("page_url"), {}).get("candidates", {}).get(url, {})
                job.update(
                    post_url=url, state=LINKED, views=cand.get("views"),
                    views_at=now if cand.get("views") is not None else None,
                    note="Gán tay theo reel gợi ý (đã kiểm tra bằng mắt)",
                    verify_note="", suggested_url="",
                )
                if cand:
                    cand.update(state=CAND_MATCHED, job=job["uid"])
                taken.add(url)
                accepted.append(job)
            if accepted:
                self.save()
            return accepted

    def pages_with_read_candidates(self) -> List[str]:
        with self.lock:
            return [
                page for page, seen in self.seen.items()
                if any(c["state"] == CAND_READ for c in seen.get("candidates", {}).values())
            ]

    def count_unverified(self) -> int:
        with self.lock:
            return sum(
                1 for seen in self.seen.values() for c in seen.get("candidates", {}).values()
                if c["state"] in (CAND_NEW, CAND_READ)
            )

    def record_read(
        self, page_url: str, url: str, caption: Optional[str], created_at: Optional[int],
        now: Optional[float] = None, max_tries: int = 5, retry_s: float = 120,
    ):
        """Store what the reel page said (caption None = page could not be read)."""
        now = time.time() if now is None else now
        with self.lock:
            cand = self.seen.get(page_url, {}).get("candidates", {}).get(url)
            if cand is None:
                return
            cand["tries"] = cand.get("tries", 0) + 1
            if caption is None or created_at is None:
                if cand["tries"] >= max_tries:
                    cand.update(state=CAND_UNREADABLE, note="Không đọc được trang reel")
                else:
                    cand["next_try"] = now + retry_s
            else:
                cand.update(state=CAND_READ, caption=caption, created_at=int(created_at))
            self.save()

    def match_candidates(
        self, page_url: str, before_s: float, after_s: float, now: Optional[float] = None,
        clock_offset: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Give each read reel of the page to the posted job it proves to be.

        Proof = same caption AND created between ``before_s`` before and ``after_s``
        after the job's Done flag vanished; several fits -> the closest time. A reel
        nobody fits stays waiting for CANDIDATE_WAIT_S (its post may not be marked
        posted yet), then counts as not ours. ``clock_offset`` = Facebook time minus this
        PC's time, so a PC clock that is off does not break the time proof.
        Returns the jobs just linked.
        """
        now = time.time() if now is None else now
        with self.lock:
            seen = self.seen.get(page_url)
            if not seen:
                return []
            candidates = seen.get("candidates", {})
            waiting = [
                j for j in self.jobs
                if j.get("page_url") == page_url and j["state"] == POSTED and not j.get("post_url")
            ]
            linked: List[Dict[str, Any]] = []
            reads = sorted(
                ((url, c) for url, c in candidates.items() if c["state"] == CAND_READ),
                key=lambda item: item[1]["created_at"],
            )
            for url, cand in reads:
                best, best_delta, near_misses = None, None, []
                for job in waiting:
                    if job in linked:
                        continue
                    caption = job_caption(job)
                    caption_ok = captions_match(caption, cand["caption"]) if caption else None
                    delta = posted_delta(job, cand["created_at"] - clock_offset)
                    time_ok = time_matches(delta, before_s, after_s)
                    if caption_ok is False or not time_ok:
                        if delta is not None and (caption_ok or time_ok):
                            near_misses.append((job, delta, bool(caption_ok)))
                        continue
                    if best is None or abs(delta) < abs(best_delta):
                        best, best_delta = job, delta
                if best is not None:
                    how = "caption khớp" if job_caption(best) else "chỉ khớp giờ (bài cũ không lưu caption)"
                    best.update(
                        post_url=url,
                        state=LINKED,
                        views=cand.get("views"),
                        views_at=now if cand.get("views") is not None else None,
                        verified={"caption_ok": bool(job_caption(best)), "created_at": cand["created_at"],
                                  "delta_s": best_delta},
                        note=f"✓ Đã xác minh: {how}, reel đăng {describe_delta(best_delta)}",
                        verify_note="",
                        suggested_url="",
                    )
                    cand.update(state=CAND_MATCHED, job=best["uid"])
                    linked.append(best)
                    continue
                for job, delta, same_caption in near_misses:
                    if same_caption:
                        why = f"Có reel cùng caption nhưng đăng {describe_delta(delta)}, ngoài khoảng cho phép"
                    else:
                        why = (f"Có reel đăng đúng giờ ({describe_delta(delta)}) nhưng caption khác "
                               "(caption bị sửa trên Facebook?)")
                    old = job.get("suggested_url")
                    if not old or abs(delta) <= abs(job.get("suggested_delta") or 1e12):
                        job.update(verify_note=f"{why}: {url}. Kiểm tra rồi bấm 'Nhận reel gợi ý' nếu đúng.",
                                   suggested_url=url, suggested_delta=delta)
                if now - cand["found_at"] > CANDIDATE_WAIT_S:
                    cand.update(state=CAND_FOREIGN, note="Không khớp bài nào của tool (đăng tay?)")
            if linked or reads:
                self.save()
            return linked
