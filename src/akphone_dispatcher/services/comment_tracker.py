"""Background service: find the link of each new reel, watch its views, push the comment.

Two threads:

* Flags thread (every 20 s, never waits for the browser):
  CMT flag gone -> done; still there after timeout -> failed; expire old posts;
  push link + comment for posts at the threshold (write linkcmt{ID}.txt and
  Comment_{ID}.txt into SCRIPTS/FILES, then create the CMT/{ID}.txt flag; one comment
  job per page at a time, so the files are never overwritten while a script reads
  them); stage re-posts asked from the GUI (one per page, only while the device's
  auto run is not running) and watch their Done flag; archive finished jobs.
* Scan thread (one page load at a time, a short random pause in between), by priority:
  1. baseline: first look at a page, before its first post goes live;
  2. new stream: a post just left the Done flag -> read the top of the Reels tab for
     new reels (no scrolling), retry a few times while Facebook processes the reel;
  3. old stream: pages with linked posts -> one load reads the views of all of them,
     scrolling only until every tracked reel is on screen; once per views interval.
  New reels are only candidates: the scan thread never decides whose they are.
* Verify thread (in parallel, its own browser with the scan profile's login): opens each
  candidate reel, reads its original caption and creation time, and links it to the
  posted job whose caption matches and whose Done flag vanished around that time.
"""

from email.utils import parsedate_to_datetime
import os
import random
import shutil
import threading
import time
import urllib.error
import urllib.request
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
    REPOST,
    REPOST_QUEUED,
    REPOST_STAGED,
    CommentQueue,
)
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.payload import read_flat_text
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.core.fb_profiles import FbProfileStore
from akphone_dispatcher.services.fb_pool import ProfilePool
from akphone_dispatcher.services.fb_web import (
    FbBlocked,
    FbWebSource,
    NoSession,
    NotLoggedIn,
    ReelReader,
)
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.utils.helpers import resolve_akphone_layout
from akphone_dispatcher.utils.logger import logger

TICK_SECONDS = 20
FAST_TICK_SECONDS = 5  # while a comment job is pushed
UNREADABLE_ALERT = 5  # reels in a row whose page could not be read -> alert
SCAN_FAIL_ALERT = 5  # page loads in a row that failed -> "network down?" alert
CLOCK_CHECK_S = 3600
CLOCK_WARN_S = 120


def facebook_server_time(timeout: float = 10.0) -> Optional[float]:
    """Facebook's clock (HTTP Date header), None when unreachable."""
    request = urllib.request.Request("https://www.facebook.com/", method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            header = response.headers.get("Date")
    except urllib.error.HTTPError as e:  # any answer still carries the date
        header = e.headers.get("Date") if e.headers else None
    except Exception:
        return None
    if not header:
        return None
    try:
        return parsedate_to_datetime(header).timestamp()
    except (TypeError, ValueError):
        return None
DONE_DIR = "/storage/emulated/0/Android/Done"
IDLE_SECONDS = 5
NEW_STREAM_MIN_REELS = 10

BASELINE = "baseline"
LINK = "link"
VIEWS = "views"


def link_filename(page_id: str) -> str:
    return f"linkcmt{page_id}.txt"


def comment_filename(page_id: str) -> str:
    return f"Comment_{page_id}.txt"


def caption_filename(page_id: str) -> str:
    return f"Caption_{page_id}.txt"


def cmt_script_filename(page_id: str) -> str:
    return f"cmt{page_id}.txt"


# What cmt{ID}.txt holds while the page has no comment job: running it does nothing,
# so AKPhoneS can never comment twice on an old link however often it runs the script.
IDLE_CMT_SCRIPT = "WAIT=1\n"


DEFAULT_CMT_DIR = "/storage/emulated/0/Android/CMT"


def render_cmt_script(
    template: str, page_id: str, fanpage_name: str, flag_dir: str = DEFAULT_CMT_DIR
) -> str:
    """The comment script; its REMOVE line follows the CMT flag folder set in Tab 5
    ({CMT_DIR} in the template, or the default folder written literally)."""
    flag_dir = (flag_dir or DEFAULT_CMT_DIR).rstrip("/")
    text = (
        template.replace("{ID}", page_id)
        .replace("{PAGE_NAME}", fanpage_name)
        .replace("{LINK_PATH}", link_filename(page_id))
        .replace("{COMMENT_PATH}", comment_filename(page_id))
        .replace("{CMT_DIR}", flag_dir)
    )
    if flag_dir != DEFAULT_CMT_DIR:
        text = text.replace(DEFAULT_CMT_DIR + "/", flag_dir + "/")
    return text


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
        is_feeding: Optional[Callable[[str], bool]] = None,
        profile_store: Optional[FbProfileStore] = None,
        reader_factory: Optional[Callable[[], object]] = None,
    ):
        self.config = config
        # True while the device's auto run is posting: re-posts wait for it to finish
        self.is_feeding = is_feeding or (lambda serial: False)
        self.adb = adb
        self.notifier = notifier
        self.queue = queue
        self.profile_store = profile_store
        self.source_factory = source_factory or self._default_source
        self.reader_factory = reader_factory or self._default_reader
        self.active_source = None  # what the scan thread reads with (for the status line)
        self.verify_note = ""  # why the verifier is waiting ("" = fine)
        self.network_note = ""  # set while Facebook cannot be loaded
        self._unreadable_streak = 0
        self._scan_fail_streak = 0
        # Facebook time minus PC time (seconds), measured by the verifier
        self.clock_offset = 0.0
        self.clock_source = facebook_server_time
        self._clock_checked_at = 0.0
        self._clock_warned = False

        self.stop_event = threading.Event()
        self.scan_wake = threading.Event()
        self.verify_wake = threading.Event()
        self.flags_thread: Optional[threading.Thread] = None
        self.scan_thread: Optional[threading.Thread] = None
        self.verify_thread: Optional[threading.Thread] = None

        self.views_scanned: Dict[str, float] = {}  # fanpage -> last views read
        self.baseline_retry: Dict[str, float] = {}  # fanpage -> earliest retry
        self.forced: Set[str] = set()
        self.blocked_reason = ""
        self.scan_times: Deque[float] = deque()
        self._warned_no_url: Set[str] = set()

    # ---------- lifecycle ----------
    def _default_source(self):
        cfg = self.config.comment
        if self.profile_store is not None:
            return ProfilePool(
                self.profile_store, cfg.headless, cfg.browser_channel, on_event=self._profile_event
            )
        return FbWebSource(
            self.config.browser_profile_dir, headless=cfg.headless, channel=cfg.browser_channel
        )

    def _default_reader(self):
        cfg = self.config.comment
        return ReelReader(self._shared_session, channel=cfg.browser_channel, headless=cfg.headless)

    def _shared_session(self):
        """Login of the scan profile in use (None until a logged-in profile is open)."""
        return getattr(self.active_source, "shared_session", None)

    def _profile_event(self, profile: dict, message: str):
        logger.warning(f"👤 [Comment] Profile '{profile['name']}' {message}")
        self.notifier.send_message(f"👤 <b>[Comment] Profile {profile['name']}</b>\n{message}")

    def start(self):
        if self.is_running():
            return
        self.stop_event.clear()
        self.blocked_reason = ""
        self.flags_thread = threading.Thread(
            target=self._flags_loop, name="CommentFlags", daemon=True
        )
        self.scan_thread = threading.Thread(target=self._scan_loop, name="CommentScan", daemon=True)
        self.verify_thread = threading.Thread(
            target=self._verify_loop, name="CommentVerify", daemon=True
        )
        self.flags_thread.start()
        self.scan_thread.start()
        self.verify_thread.start()
        logger.info("💬 [Comment] Đã bật theo dõi (luồng cờ CMT + luồng quét view + luồng xác minh reel).")

    def stop(self):
        self.stop_event.set()
        self.scan_wake.set()
        self.verify_wake.set()
        logger.info("💬 [Comment] Đang dừng theo dõi lượt xem...")

    def join(self, timeout: float = 90):
        for thread in (self.scan_thread, self.verify_thread, self.flags_thread):
            if thread is not None:
                thread.join(timeout)

    def is_alive(self) -> bool:
        return any(
            t is not None and t.is_alive()
            for t in (self.scan_thread, self.verify_thread, self.flags_thread)
        )

    def is_running(self) -> bool:
        return self.is_alive() and not self.stop_event.is_set()

    def scan_now(self):
        """Clear a pause and read every page with work right away."""
        self.blocked_reason = ""
        reset = getattr(self.active_source, "reset_cooldowns", None)
        if reset is not None:
            reset()
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
            "profile": getattr(self.active_source, "current_name", ""),
            "verifying": self.queue.count_unverified(),
            "verify_note": self.verify_note or self.network_note,
        }

    # ---------- threads ----------
    def _flags_loop(self):
        while not self.stop_event.is_set():
            try:
                self.flags_tick()
            except Exception as e:
                logger.error(f"[Comment] Lỗi luồng cờ CMT: {e}", exc_info=True)
            self.stop_event.wait(self.flags_wait())

    def flags_wait(self) -> float:
        """Poll fast while a comment script may be running: the moment its flag vanishes the
        script is parked again, so a quick AKPhoneS loop cannot run it twice on one link."""
        busy = any(job["state"] == MARKED for job in self.queue.active_jobs())
        return FAST_TICK_SECONDS if busy else TICK_SECONDS

    def _scan_loop(self):
        cfg = self.config.comment
        source = None
        try:
            source = self.source_factory()
            self.active_source = source
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
            self.active_source = None
            logger.info("💬 [Comment] Đã dừng luồng quét.")

    def _verify_loop(self):
        reader = None
        try:
            reader = self.reader_factory()
            while not self.stop_event.is_set():
                did_read = False
                self._check_clock()  # network call: only in the real thread, never in tests
                try:
                    did_read = self.verify_step(reader)
                    self.verify_note = ""
                except NoSession as e:
                    self.verify_note = str(e)
                except FbBlocked as e:  # the borrowed login expired / checkpoint
                    self._verifier_profile_failed(reader, e)
                except Exception as e:
                    self.verify_note = f"Lỗi đọc reel: {e}"
                    logger.warning(f"[Comment] Luồng xác minh reel lỗi: {e}")
                    self.stop_event.wait(30)
                if did_read:
                    self.stop_event.wait(random.uniform(1.0, 3.0))
                else:
                    self.verify_wake.wait(IDLE_SECONDS)
                    self.verify_wake.clear()
        finally:
            if reader is not None:
                reader.close()
            logger.info("💬 [Comment] Đã dừng luồng xác minh reel.")

    def _verifier_profile_failed(self, reader, error: Exception):
        session = getattr(reader, "failed_session", None) or getattr(reader, "session", None) or {}
        pid = session.get("profile_id")
        reader.close()
        drop = getattr(self.active_source, "drop_session", None)
        if drop is not None and pid:
            drop(pid)
        if self.profile_store is not None and pid:
            if isinstance(error, NotLoggedIn):
                self.profile_store.set_account(pid, "")
            else:
                self.profile_store.set_blocked(pid, str(error))
            self._profile_event(
                {"name": session.get("name", pid)},
                f"luồng xác minh: {error}",
            )
        self.verify_note = str(error)

    # ---------- verify side ----------
    def verify_step(self, reader, now: Optional[float] = None) -> bool:
        """Re-match read reels, then open ONE candidate reel. True when a page was opened."""
        now = time.time() if now is None else now
        cfg = self.config.comment
        before, after = cfg.verify_before_min * 60, cfg.verify_after_min * 60
        offset = self.clock_offset
        for page in self.queue.pages_with_read_candidates():
            self._log_linked(self.queue.match_candidates(page, before, after, now, offset))
        pending = self.queue.pending_reads(now)
        if not pending:
            return False
        page, url = pending[0]
        info = reader.read_reel(url)
        if info is None:
            self._unreadable_streak += 1
            if self._unreadable_streak == UNREADABLE_ALERT:
                message = (
                    f"{UNREADABLE_ALERT} reel liên tiếp không đọc được caption/giờ đăng "
                    "(Facebook có thể đã đổi giao diện); bài mới chưa được gán link."
                )
                logger.error(f"[Comment] {message}")
                self.notifier.send_message(f"⚠️ <b>[Comment] Xác minh reel</b>\n{message}")
        else:
            self._unreadable_streak = 0
        self.queue.record_read(
            page, url, info.caption if info else None, info.created_at if info else None, now
        )
        self._log_linked(self.queue.match_candidates(page, before, after, now, offset))
        return True

    def _check_clock(self):
        """Measure this PC's clock against Facebook's once an hour (keeps the last value)."""
        local = time.time()
        if local - self._clock_checked_at < CLOCK_CHECK_S:
            return
        self._clock_checked_at = local
        try:
            server = self.clock_source()
        except Exception:
            server = None
        if server is None:
            return
        self.clock_offset = server - (local + time.time()) / 2
        minutes = abs(self.clock_offset) / 60
        if abs(self.clock_offset) >= CLOCK_WARN_S and not self._clock_warned:
            self._clock_warned = True
            message = (
                f"Đồng hồ máy tính lệch {minutes:.0f} phút so với Facebook. Tool đã tự bù khi so giờ đăng, "
                "nhưng nên bật đồng bộ giờ tự động của Windows."
            )
            logger.warning(f"🕒 [Comment] {message}")
            self.notifier.send_message(f"🕒 <b>[Comment] Lệch đồng hồ</b>\n{message}")

    def _log_linked(self, jobs: List[dict]):
        for job in jobs:
            logger.info(
                f"🔗 [Comment] [{job['page_id']}] bài {job['post_num']} -> {job['post_url']} ({job['note']})"
            )

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
        self._drive_reposts(now)
        self.queue.archive_finished(self.config.comment.archive_after_hours * 3600, now)

    def tick(self, source, now: Optional[float] = None, max_scans: int = 1000, reader=None):
        """One full pass: scan every page that is due, verify every new reel (tests / 'run once')."""
        now = time.time() if now is None else now
        self._check_marked(now)
        self._expire(now)
        for _ in range(max_scans):
            if self.blocked_reason or not self.scan_step(source, now):
                break
        reader = reader or (source if hasattr(source, "read_reel") else None)
        if reader is not None:
            for _ in range(max_scans):
                if not self.verify_step(reader, now):
                    break
        self._push_ready(now)

    def _check_marked(self, now: float):
        cfg = self.config.comment
        by_serial: Dict[str, List[dict]] = defaultdict(list)
        for job in self.queue.active_jobs():
            if job["state"] == MARKED:
                by_serial[job["serial"]].append(job)

        for serial, jobs in by_serial.items():
            for job in jobs:
                if not job["flag_created"] and self.adb.touch_flag(serial, cfg.flag_dir, job["page_id"]):
                    self.queue.update(job, flag_created=True)
            # None = phone unreachable / storage not ready: never read that as "flag gone"
            flags = self.adb.list_flags(serial, cfg.flag_dir)
            if flags is None:
                continue
            for job in jobs:
                pid = job["page_id"]
                if not job["flag_created"]:
                    continue
                if pid not in flags:
                    self.queue.update(job, state=DONE, finished_at=now)
                    self._idle_comment_files(pid)
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
                    self._idle_comment_files(pid)
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
        with self.queue.batch():
            self._expire_jobs(now, limit)

    def _expire_jobs(self, now: float, limit: float):
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

    def _idle_comment_files(self, pid: str):
        """Comment finished: blank the link/comment files and park the comment script."""
        scripts_dir = resolve_akphone_layout(self.config.akphone_dir)[0]
        try:
            _write_atomic(scripts_dir / cmt_script_filename(pid), IDLE_CMT_SCRIPT)
            _write_atomic(scripts_dir / "FILES" / link_filename(pid), "")
            _write_atomic(scripts_dir / "FILES" / comment_filename(pid), "")
        except Exception as e:
            logger.error(f"[Comment] Không dọn được file comment của [{pid}]: {e}")

    def _push_ready(self, now: float):
        cfg = self.config.comment
        scripts_dir = resolve_akphone_layout(self.config.akphone_dir)[0]
        files_dir = scripts_dir / "FILES"
        template_path = self.config.template_cmt_path
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
                template = template_path.read_text(encoding="utf-8")
                _write_atomic(files_dir / link_filename(pid), job["post_url"])
                _write_atomic(files_dir / comment_filename(pid), job["comment_text"])
                _write_atomic(
                    scripts_dir / cmt_script_filename(pid),
                    render_cmt_script(template, pid, job["fanpage_name"], cfg.flag_dir),
                )
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

    # ---------- re-post (asked from the GUI) ----------
    def _drive_reposts(self, now: float):
        by_page: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
        for job in self.queue.active_jobs():
            if job["state"] == REPOST:
                by_page[(job["serial"], job["page_id"])].append(job)

        for (serial, pid), jobs in by_page.items():
            staged = [j for j in jobs if j["repost_state"] == REPOST_STAGED]
            if self.is_feeding(serial):
                for job in staged:  # the auto run rewrites the running dir and Done flags
                    self.queue.update(
                        job,
                        repost_state=REPOST_QUEUED,
                        note="Máy đang chạy auto: chờ chạy xong rồi nạp lại",
                    )
                continue
            if staged:
                job = staged[0]
                flags = self.adb.list_flags(serial, DONE_DIR)
                if flags is not None and pid not in flags:
                    self.queue.update(
                        job,
                        state=POSTED,
                        posted_at=now,
                        repost_state="",
                        link_attempts=0,
                        next_link_at=None,
                        note=f"Đã đăng lại (lần {job['repost_count']})",
                    )
                    logger.info(
                        f"🔁 [MÁY {serial}] [{pid}] bài {job['post_num']} đã đăng lại; đang chờ thấy reel."
                    )
                continue
            if self.adb.is_device_ready(serial):
                self._stage_repost(min(jobs, key=lambda j: j["fed_at"] or 0), now)

    def _stage_repost(self, job: dict, now: float):
        """Put the post's video + caption back for its page and create its Done flag."""
        serial, pid = job["serial"], job["page_id"]
        video = Path(job.get("video_raw") or "")
        if not self.config.running_dir:
            self.queue.update(job, note="Chưa cấu hình thư mục đệm (Running Dir)")
            return
        if not video.is_file():
            self.queue.update(
                job,
                state=EXPIRED,
                repost_state="",
                finished_at=now,
                note="Mất bản lưu video: không đăng lại được",
            )
            logger.warning(f"[Comment] [{pid}] bài {job['post_num']}: mất bản lưu video, bỏ đăng lại.")
            return
        try:
            video_dir = Path(self.config.running_dir) / pid
            video_dir.mkdir(parents=True, exist_ok=True)
            for old in video_dir.iterdir():
                if old.is_file():
                    old.unlink()
            shutil.copy2(video, video_dir / video.name)
            caption_raw = Path(job.get("caption_raw") or "")
            caption = read_flat_text(caption_raw) if caption_raw.is_file() else ""
            files_dir = resolve_akphone_layout(self.config.akphone_dir)[0] / "FILES"
            _write_atomic(files_dir / caption_filename(pid), caption)
        except Exception as e:
            self.queue.update(job, note=f"Lỗi nạp lại: {e}")
            logger.error(f"[Comment] Không nạp lại được bài {job['post_num']} của [{pid}]: {e}")
            return
        if not self.adb.touch_flag(serial, DONE_DIR, pid):
            self.queue.update(job, note="Chưa tạo được cờ Done trên máy; thử lại sau")
            return
        count = (job.get("repost_count") or 0) + 1
        self.queue.update(
            job,
            repost_state=REPOST_STAGED,
            repost_count=count,
            note=f"Đã nạp lại video + caption: chạy {pid}.txt trên AKPhoneS",
        )
        logger.info(
            f"🔁 [MÁY {serial}] [{pid}] đã nạp lại bài {job['post_num']} ({video.name}) và cờ Done: "
            f"chạy {pid}.txt trên AKPhoneS để đăng lại."
        )
        self.notifier.send_message(
            f"🔁 <b>Đã nạp lại bài để đăng lại</b>\n{job['fanpage_name']} ({pid}) bài {job['post_num']}: "
            f"chạy {pid}.txt trên AKPhoneS"
        )

    # ---------- scan side ----------
    # A page is identified by the Fanpage link stored in each job (snapshot at feed
    # time), so replacing a page later never mixes old and new posts.
    def _pages_with_work(self) -> Set[str]:
        return {
            j["page_url"]
            for j in self.queue.active_jobs()
            if j["state"] in (POSTED, LINKED) and j.get("page_url")
        }

    def _scannable_jobs(self) -> List[dict]:
        jobs = []
        for job in self.queue.active_jobs():
            if job.get("page_url"):
                jobs.append(job)
                continue
            key = f"{job['serial']}/{job['page_id']}/{job['fanpage_name']}"
            if key not in self._warned_no_url:
                self._warned_no_url.add(key)
                logger.warning(
                    f"[Comment] [{job['page_id']}] {job['fanpage_name']} (máy {job['serial']}) "
                    "chưa có link Fanpage: điền ở Tab 1 cạnh tên page rồi bấm Lưu."
                )
        return jobs

    def _page_label(self, page_url: str) -> str:
        for job in self.queue.active_jobs():
            if job.get("page_url") == page_url:
                return f"{job['fanpage_name']} ({job['page_id']})"
        return page_url

    def next_scan(self, now: float) -> Optional[Tuple[str, str]]:
        """Pick the most urgent (page link, kind) to read now, or None."""
        jobs = self._scannable_jobs()
        views_every = self.config.comment.views_interval_min * 60

        for job in sorted(jobs, key=lambda j: j["fed_at"] or 0):
            page = job["page_url"]
            if (
                job["state"] in (FED, POSTED, REPOST)
                and self.queue.needs_baseline(page)
                and self.baseline_retry.get(page, 0) <= now
            ):
                return page, BASELINE

        def link_due_at(job) -> float:
            # first look link_delay_s after posting, then whatever the retries scheduled
            if job.get("next_link_at"):
                return job["next_link_at"]
            return (job["posted_at"] or 0) + self.config.comment.link_delay_s

        link_due = [
            j
            for j in jobs
            if j["state"] == POSTED and (j["page_url"] in self.forced or link_due_at(j) <= now)
        ]
        if link_due:
            return min(link_due, key=link_due_at)["page_url"], LINK

        views_due = {
            j["page_url"]
            for j in jobs
            if j["state"] == LINKED
            and (
                j["page_url"] in self.forced
                or now - self.views_scanned.get(j["page_url"], 0) >= views_every
            )
        }
        if views_due:
            return min(views_due, key=lambda page: self.views_scanned.get(page, 0)), VIEWS
        return None

    def scan_step(self, source, now: Optional[float] = None) -> bool:
        """Read ONE page if something is due. Returns True when a page was loaded."""
        now = time.time() if now is None else now
        pick = self.next_scan(now)
        if pick is None:
            self.forced.clear()
            return False
        page, kind = pick
        cfg = self.config.comment
        label = self._page_label(page)
        want = self.queue.tracked_urls(page) if kind == VIEWS else set()

        self.scan_times.append(now)
        try:
            reels = source.scan(page, want_urls=want, min_count=NEW_STREAM_MIN_REELS)
        except FbBlocked as e:
            self._block(str(e))
            return True
        except Exception as e:
            logger.warning(f"[Comment] Quét {label} ({kind}) lỗi: {e}")
            reels = None

        self.forced.discard(page)
        if reels is None:  # failed load (often the network): retry later, not a try
            self.baseline_retry[page] = now + cfg.link_retry_s
            self.views_scanned[page] = now
            self._postpone_link(page, now)
            self._scan_failed()
            return True
        self._scan_succeeded()

        if kind == BASELINE and not reels:
            logger.warning(
                f"[Comment] {label}: tab Reels không có reel nào ({page}). Page mới thì bình thường; "
                "nếu page đã có reel thì link Fanpage ở Tab 1 có thể sai."
            )
        tracked_before = self.queue.tracked_urls(page)
        new_urls = self.queue.apply_scan(page, reels, now=now)
        if new_urls and kind != BASELINE:
            logger.info(
                f"[Comment] {label}: thấy {len(new_urls)} reel mới, đang xác minh caption + giờ đăng."
            )
            self.verify_wake.set()
        found = {u for u, _ in reels}
        if kind == VIEWS or tracked_before <= found:
            self.views_scanned[page] = now
            missing = tracked_before - found
            if kind == VIEWS and missing:
                logger.warning(
                    f"[Comment] {label}: {len(missing)} reel đang theo dõi không thấy trên tab Reels."
                )
        self._schedule_link_retry(
            page, now, "Đã thấy reel mới, đang xác minh" if new_urls else "Chưa thấy reel mới"
        )
        return True

    def _schedule_link_retry(self, page: str, now: float, reason: str):
        cfg = self.config.comment
        with self.queue.batch():
            for job in self.queue.active_jobs():
                if job.get("page_url") != page or job["state"] != POSTED:
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

    def _postpone_link(self, page: str, now: float):
        """Page did not load: look again later without counting a try (no false
        "suspect" while the network is down)."""
        with self.queue.batch():
            for job in self.queue.active_jobs():
                if job.get("page_url") == page and job["state"] == POSTED:
                    self.queue.update(
                        job,
                        next_link_at=now + self.config.comment.link_retry_s,
                        note="Không tải được trang (mạng?), sẽ thử lại; không tính là 1 lần thử",
                    )

    def _scan_failed(self):
        self._scan_fail_streak += 1
        if self._scan_fail_streak == SCAN_FAIL_ALERT:
            message = (
                f"{SCAN_FAIL_ALERT} lần liên tiếp không tải được Facebook (mất mạng / proxy?). "
                "Tool vẫn tự thử lại; bài chờ link không bị tính là lỗi."
            )
            self.network_note = "Không tải được Facebook (mất mạng?)"
            logger.error(f"🌐 [Comment] {message}")
            self.notifier.send_message(f"🌐 <b>[Comment] Mất kết nối Facebook</b>\n{message}")

    def _scan_succeeded(self):
        if self._scan_fail_streak >= SCAN_FAIL_ALERT:
            logger.info("🌐 [Comment] Đã tải lại được Facebook, quét tiếp bình thường.")
            self.notifier.send_message("🌐 <b>[Comment] Đã kết nối lại Facebook</b>")
        self._scan_fail_streak = 0
        self.network_note = ""
