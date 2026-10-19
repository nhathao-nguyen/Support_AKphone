"""Background service: find the link of each new reel, watch its views, push the comment.

Two threads:

* Flags thread (every 20 s, never waits for the browser):
  CMT flag gone -> done; still there after timeout -> failed; expire old posts;
  push link + comment for posts at the threshold (write linkcmt{ID}.txt and
  Comment_{ID}.txt into SCRIPTS/FILES, then create the CMT/{ID}.txt flag; one comment
  job per page at a time, so the files are never overwritten while a script reads
  them); archive finished jobs.
* Scan thread (one page load at a time, a short random pause in between), by priority:
  1. baseline: first look at a page, before its first post goes live;
  2. new stream: a post just left the Done flag -> read the top of the Reels tab for
     its link (no scrolling), retry a few times while Facebook processes the reel;
  3. old stream: pages with linked posts -> one load reads the views of all of them,
     scrolling only until every tracked reel is on screen; once per views interval.
"""

import os
import random
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple

from akphone_dispatcher.core.comment_queue import (
    DONE,
    EXPIRED,
    FAILED,
    FED,
    LINKED,
    MARKED,
    POSTED,
    CommentQueue,
)
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.fb_web import FbBlocked, FbWebSource, parse_page_urls
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.utils.helpers import resolve_akphone_layout
from akphone_dispatcher.utils.logger import logger

TICK_SECONDS = 20
IDLE_SECONDS = 5
NEW_STREAM_MIN_REELS = 10

BASELINE = "baseline"
LINK = "link"
VIEWS = "views"


def link_filename(page_id: str) -> str:
    return f"linkcmt{page_id}.txt"


def comment_filename(page_id: str) -> str:
    return f"Comment_{page_id}.txt"


def _write_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class CommentTracker:
    def __init__(
        self,
        config: AppConfig,
        adb: AdbService,
        notifier: TelegramNotifier,
        queue: CommentQueue,
        source_factory: Optional[Callable[[], object]] = None,
    ):
        self.config = config
        self.adb = adb
        self.notifier = notifier
        self.queue = queue
        self.source_factory = source_factory or self._default_source

        self.stop_event = threading.Event()
        self.scan_wake = threading.Event()
        self.flags_thread: Optional[threading.Thread] = None
        self.scan_thread: Optional[threading.Thread] = None

        self.views_scanned: Dict[str, float] = {}  # fanpage -> last views read
        self.baseline_retry: Dict[str, float] = {}  # fanpage -> earliest retry
        self.forced: Set[str] = set()
        self.blocked_reason = ""
        self.scan_times: Deque[float] = deque()
        self._warned_no_url: Set[str] = set()

    # ---------- lifecycle ----------
    def _default_source(self):
        cfg = self.config.comment
        return FbWebSource(
            self.config.browser_profile_dir, headless=cfg.headless, channel=cfg.browser_channel
        )

    def start(self):
        if self.is_running():
            return
        self.stop_event.clear()
        self.blocked_reason = ""
        self.flags_thread = threading.Thread(
            target=self._flags_loop, name="CommentFlags", daemon=True
        )
        self.scan_thread = threading.Thread(target=self._scan_loop, name="CommentScan", daemon=True)
        self.flags_thread.start()
        self.scan_thread.start()
        logger.info("💬 [Comment] Đã bật theo dõi (luồng cờ CMT + luồng quét).")

    def stop(self):
        self.stop_event.set()
        self.scan_wake.set()
        logger.info("💬 [Comment] Đang dừng theo dõi lượt xem...")

    def join(self, timeout: float = 90):
        for thread in (self.scan_thread, self.flags_thread):
            if thread is not None:
                thread.join(timeout)

    def is_alive(self) -> bool:
        return any(t is not None and t.is_alive() for t in (self.scan_thread, self.flags_thread))

    def is_running(self) -> bool:
        return self.is_alive() and not self.stop_event.is_set()

    def scan_now(self):
        """Clear a pause and read every page with work right away."""
        self.blocked_reason = ""
        self.forced = set(self._pages_with_work())
        self.scan_wake.set()

    def status(self, now: Optional[float] = None) -> Dict[str, int]:
        now = time.time() if now is None else now
        counts = defaultdict(int)
        for job in self.queue.active_jobs():
            counts[job["state"]] += 1
        while self.scan_times and now - self.scan_times[0] > 3600:
            self.scan_times.popleft()
        return {
            "posted": counts[POSTED],
            "linked": counts[LINKED],
            "marked": counts[MARKED],
            "scans_last_hour": len(self.scan_times),
        }

    # ---------- threads ----------
    def _flags_loop(self):
        while not self.stop_event.is_set():
            try:
                self.flags_tick()
            except Exception as e:
                logger.error(f"[Comment] Lỗi luồng cờ CMT: {e}", exc_info=True)
            self.stop_event.wait(TICK_SECONDS)

    def _scan_loop(self):
        cfg = self.config.comment
        source = None
        try:
            source = self.source_factory()
            while not self.stop_event.is_set():
                if self.blocked_reason:
                    self.scan_wake.wait(IDLE_SECONDS)
                    self.scan_wake.clear()
                    continue
                try:
                    did_scan = self.scan_step(source)
                except Exception as e:
                    self._block(str(e))
                    did_scan = False
                if did_scan:
                    self.stop_event.wait(random.uniform(cfg.scan_gap_min_s, cfg.scan_gap_max_s))
                else:
                    self.scan_wake.wait(IDLE_SECONDS)
                    self.scan_wake.clear()
        finally:
            if source is not None:
                source.close()
            logger.info("💬 [Comment] Đã dừng luồng quét.")

    def _block(self, reason: str):
        self.blocked_reason = reason
        logger.error(f"🔒 [Comment] Tạm dừng quét: {reason}. Xử lý xong thì bấm 'Quét ngay'.")
        self.notifier.send_message(f"🔒 <b>[Comment] Tạm dừng quét web</b>\n{reason}")

    # ---------- flags side ----------
    def flags_tick(self, now: Optional[float] = None):
        now = time.time() if now is None else now
        self._check_marked(now)
        self._expire(now)
        self._push_ready(now)
        self.queue.archive_finished(self.config.comment.archive_after_hours * 3600, now)

    def tick(self, source, now: Optional[float] = None, max_scans: int = 1000):
        """One full pass, scanning every page that is due (used by tests / 'run once')."""
        now = time.time() if now is None else now
        self._check_marked(now)
        self._expire(now)
        for _ in range(max_scans):
            if self.blocked_reason or not self.scan_step(source, now):
                break
        self._push_ready(now)

    def _check_marked(self, now: float):
        cfg = self.config.comment
        by_serial: Dict[str, List[dict]] = defaultdict(list)
        for job in self.queue.active_jobs():
            if job["state"] == MARKED:
                by_serial[job["serial"]].append(job)

        for serial, jobs in by_serial.items():
            flags = self.adb.list_flags(serial, cfg.flag_dir)
            if flags is None:
                continue
            for job in jobs:
                pid = job["page_id"]
                if not job["flag_created"]:
                    if self.adb.touch_flag(serial, cfg.flag_dir, pid):
                        self.queue.update(job, flag_created=True)
                elif pid not in flags:
                    self.queue.update(job, state=DONE, finished_at=now)
                    logger.info(
                        f"💬 [MÁY {serial}] Đã comment xong [{pid}] bài {job['post_num']} "
                        f"({job['views']} lượt xem)."
                    )
                    self.notifier.send_message(
                        f"💬 <b>Đã comment</b> {job['fanpage_name']} ({pid}) bài {job['post_num']}\n"
                        f"{job['post_url']}"
                    )
                elif job["marked_at"] is not None and now - job["marked_at"] > cfg.cmt_timeout_s:
                    self.adb.remove_flag(serial, cfg.flag_dir, pid)
                    self.queue.update(
                        job,
                        state=FAILED,
                        finished_at=now,
                        note=f"Cờ CMT còn sau {cfg.cmt_timeout_s}s",
                    )
                    logger.warning(
                        f"⚠️ [MÁY {serial}] Comment [{pid}] bài {job['post_num']} lỗi: cờ CMT "
                        f"còn sau {cfg.cmt_timeout_s}s (script comment chưa chạy hoặc lỗi)."
                    )
                    self.notifier.send_message(
                        f"⚠️ <b>Comment lỗi</b> {job['fanpage_name']} ({pid}) bài {job['post_num']}: "
                        f"cờ CMT còn sau {cfg.cmt_timeout_s}s"
                    )

    def _expire(self, now: float):
        limit = self.config.comment.expire_hours * 3600
        for job in self.queue.active_jobs():
            if (
                job["state"] in (POSTED, LINKED)
                and job["posted_at"] is not None
                and now - job["posted_at"] > limit
            ):
                self.queue.update(
                    job,
                    state=EXPIRED,
                    finished_at=now,
                    note=f"Quá {self.config.comment.expire_hours}h chưa đủ view ({job['views']})",
                )
                logger.info(
                    f"⌛ [Comment] [{job['page_id']}] bài {job['post_num']} hết hạn "
                    f"({job['views']} lượt xem)."
                )

    def _push_ready(self, now: float):
        cfg = self.config.comment
        files_dir = resolve_akphone_layout(self.config.akphone_dir)[0] / "FILES"
        ready = sorted(
            (
                j
                for j in self.queue.active_jobs()
                if j["state"] == LINKED and (j["views"] or 0) >= cfg.view_threshold
            ),
            key=lambda j: j["posted_at"] or 0,
        )
        for job in ready:
            serial, pid = job["serial"], job["page_id"]
            if self.queue.has_marked(serial, pid):
                continue  # one comment job per page at a time
            if not self.adb.is_device_ready(serial):
                continue
            try:
                _write_atomic(files_dir / link_filename(pid), job["post_url"])
                _write_atomic(files_dir / comment_filename(pid), job["comment_text"])
            except Exception as e:
                logger.error(f"[Comment] Không ghi được file link/comment cho [{pid}]: {e}")
                continue
            # Persist MARKED before creating the flag: a crash cannot comment twice.
            self.queue.update(job, state=MARKED, marked_at=now, flag_created=False)
            if self.adb.touch_flag(serial, cfg.flag_dir, pid):
                self.queue.update(job, flag_created=True)
                logger.info(
                    f"💬 [MÁY {serial}] [{pid}] bài {job['post_num']} đạt {job['views']} lượt xem: "
                    f"đã đẩy link + comment, tạo cờ {cfg.flag_dir}/{pid}.txt."
                )
            else:
                logger.warning(f"[Comment] Chưa tạo được cờ CMT cho [{pid}]; thử lại vòng sau.")

    # ---------- scan side ----------
    def _pages_with_work(self) -> Set[str]:
        return {
            j["fanpage_name"] for j in self.queue.active_jobs() if j["state"] in (POSTED, LINKED)
        }

    def _page_urls(self) -> Dict[str, str]:
        urls = parse_page_urls(self.config.comment.page_urls)
        for job in self.queue.active_jobs():
            name = job["fanpage_name"]
            if name not in urls and name not in self._warned_no_url:
                self._warned_no_url.add(name)
                logger.warning(f"[Comment] Chưa có link Fanpage cho '{name}' trong tab Comment.")
        return urls

    def next_scan(self, now: float) -> Optional[Tuple[str, str]]:
        """Pick the most urgent (fanpage, kind) to read now, or None."""
        urls = self._page_urls()
        jobs = [j for j in self.queue.active_jobs() if j["fanpage_name"] in urls]
        views_every = self.config.comment.views_interval_min * 60

        for job in sorted(jobs, key=lambda j: j["fed_at"] or 0):
            name = job["fanpage_name"]
            if (
                job["state"] in (FED, POSTED)
                and self.queue.needs_baseline(name)
                and self.baseline_retry.get(name, 0) <= now
            ):
                return name, BASELINE

        def link_due_at(job) -> float:
            # first look link_delay_s after posting, then whatever the retries scheduled
            if job.get("next_link_at"):
                return job["next_link_at"]
            return (job["posted_at"] or 0) + self.config.comment.link_delay_s

        link_due = [
            j
            for j in jobs
            if j["state"] == POSTED
            and (j["fanpage_name"] in self.forced or link_due_at(j) <= now)
        ]
        if link_due:
            return min(link_due, key=link_due_at)["fanpage_name"], LINK

        views_due = {
            j["fanpage_name"]
            for j in jobs
            if j["state"] == LINKED
            and (
                j["fanpage_name"] in self.forced
                or now - self.views_scanned.get(j["fanpage_name"], 0) >= views_every
            )
        }
        if views_due:
            return min(views_due, key=lambda name: self.views_scanned.get(name, 0)), VIEWS
        return None

    def scan_step(self, source, now: Optional[float] = None) -> bool:
        """Read ONE page if something is due. Returns True when a page was loaded."""
        now = time.time() if now is None else now
        pick = self.next_scan(now)
        if pick is None:
            self.forced.clear()
            return False
        name, kind = pick
        cfg = self.config.comment
        url = parse_page_urls(cfg.page_urls)[name]
        want = self.queue.tracked_urls(name) if kind == VIEWS else set()

        self.scan_times.append(now)
        try:
            reels = source.scan(url, want_urls=want, min_count=NEW_STREAM_MIN_REELS)
        except FbBlocked as e:
            self._block(str(e))
            return True
        except Exception as e:
            logger.warning(f"[Comment] Quét '{name}' ({kind}) lỗi: {e}")
            reels = None

        self.forced.discard(name)
        if reels is None:  # failed load: back off like a normal pass
            self.baseline_retry[name] = now + cfg.link_retry_s
            self.views_scanned[name] = now
            self._schedule_link_retry(name, now, "Quét lỗi")
            return True

        tracked_before = self.queue.tracked_urls(name)
        for job in self.queue.apply_scan(name, reels):
            logger.info(
                f"🔗 [Comment] [{job['page_id']}] bài {job['post_num']} -> {job['post_url']}"
            )
        found = {u for u, _ in reels}
        if kind == VIEWS or tracked_before <= found:
            self.views_scanned[name] = now
            missing = tracked_before - found
            if kind == VIEWS and missing:
                logger.warning(
                    f"[Comment] {name}: {len(missing)} reel đang theo dõi không thấy trên tab Reels."
                )
        self._schedule_link_retry(name, now, "Chưa thấy reel mới")
        return True

    def _schedule_link_retry(self, name: str, now: float, reason: str):
        cfg = self.config.comment
        for job in self.queue.active_jobs():
            if job["fanpage_name"] != name or job["state"] != POSTED:
                continue
            attempts = (job.get("link_attempts") or 0) + 1
            fast = attempts < cfg.link_fast_attempts
            delay = cfg.link_retry_s if fast else cfg.views_interval_min * 60
            self.queue.update(
                job,
                link_attempts=attempts,
                next_link_at=now + delay,
                note=f"{reason} (lần {attempts})",
            )
