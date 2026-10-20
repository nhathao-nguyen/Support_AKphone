"""Multi-device concurrent worker and farm orchestration manager."""

from datetime import datetime
from pathlib import Path
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from akphone_dispatcher.core.comment_queue import CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import DeviceProfile, PageInfo
from akphone_dispatcher.core.payload import (
    clean_completed_payloads,
    read_flat_text,
    transfer_payload,
)
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.services.session import SessionManager
from akphone_dispatcher.utils.logger import logger

FEED_RETRY_S = 60  # retry a failed feed this often (the user may fix the store meanwhile)
SEEN_SAVE_S = 30  # while a round runs, "flags last seen" is saved this often


def queue_post_comment(
    comment_queue: Optional[CommentQueue],
    serial: str,
    p_info: PageInfo,
    post_idx: int,
    transferred: Dict[str, Any],
):
    """Hand the comment of a just-fed post to the comment queue (written later)."""
    if comment_queue is None or not transferred.get("comment_file"):
        return
    try:
        text = read_flat_text(transferred["comment_file"])
    except Exception as e:
        logger.warning(f"[Comment] Không đọc được comment bài {post_idx} của [{p_info.page_id}]: {e}")
        return
    if not text:
        return
    try:  # kept to prove later which reel is this post (caption match)
        caption = read_flat_text(transferred["caption_file"]) if transferred.get("caption_file") else ""
    except Exception:
        caption = ""
    comment_queue.add_fed(
        serial=serial,
        page_id=p_info.page_id,
        fanpage_name=p_info.fanpage_name,
        post_num=post_idx,
        video_name=Path(transferred["video_file"]).name,
        comment_text=text,
        page_url=p_info.page_url,
        archive={
            "dir": transferred.get("_payload_archive") or "",
            "video": transferred.get("video_file") or "",
            "caption": transferred.get("caption_file") or "",
        },
        caption_text=caption,
    )


class DeviceFeederWorker:
    """Independent worker thread monitoring and rolling posts for a SINGLE Phone/Via."""

    def __init__(
        self,
        serial: str,
        profile: DeviceProfile,
        config: AppConfig,
        adb_service: AdbService,
        notifier: TelegramNotifier,
        session_manager: SessionManager,
        on_progress_update: Callable[[], None],
        comment_queue: Optional[CommentQueue] = None,
    ):
        self.serial = serial
        self.profile = profile
        self.config = config
        self.adb = adb_service
        self.notifier = notifier
        self.session_manager = session_manager
        self.on_progress_update = on_progress_update
        self.comment_queue = comment_queue

        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

        # State tracking
        self.round_number = 0
        self.active_round_pages: List[str] = []
        self.current_round_flags: Set[str] = set()
        self.round_succeeded: Set[str] = set()
        self.round_failed: Set[str] = set()
        self.pending_feed: Dict[str, Any] = {}
        self.sentinel_active = False
        self.last_flag_change_time = time.time()
        self.feed_retry_at: Dict[str, float] = {}  # page -> next try after a failed feed
        self._timeouts_in_row = 0  # watchdog fired with no flag removed since
        # the saved round (profile.round_flags) is checked against the phone once per start,
        # before anything else is saved or flagged
        self._reconciled = False
        self._flags_seen_at = 0.0  # last poll that read the Done folder
        self._seen_saved_at = 0.0

    def start(self):
        self.stop_event.clear()
        resumed = [pid for pid, info in self.profile.pages_data.items() if info.paused_reason]
        for info in self.profile.pages_data.values():  # Start again = give paused pages a new try
            info.paused_reason = ""
            info.fail_streak = 0
        if resumed:
            logger.info(
                f"▶️ [MÁY {self.serial}] Chạy lại cả các page đã tạm ngưng vì lỗi liên tiếp: {', '.join(resumed)}"
            )
        self._timeouts_in_row = 0
        self.round_number = 0
        self.round_succeeded = set()
        self.round_failed = set()
        self.current_round_flags = set()
        self.pending_feed = {}
        self.sentinel_active = False
        self.feed_retry_at = {}
        self._reconciled = False
        self.thread = threading.Thread(
            target=self._run, name=f"Worker-{self.serial}-{self.profile.via_tag}", daemon=True
        )
        self.thread.start()
        self.profile.status = "🟢 Đang chạy"
        logger.info(
            f"🚀 [MÁY {self.serial} | {self.profile.via_tag}] ĐÃ BẬT TIẾN TRÌNH GIÁM SÁT ĐỘC LẬP!"
        )

    def stop(self):
        self.stop_event.set()
        self.profile.status = "⏹️ Đã dừng"
        logger.info(f"⏹️ [MÁY {self.serial} | {self.profile.via_tag}] Đang dừng giám sát...")

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()

    def _save_progress(self):
        """Persist this phone's progress right away: a power cut resumes from here."""
        if self._reconciled:  # until then the profile still holds the previous run's round
            self.profile.round_flags = sorted(self.current_round_flags)
            self.profile.round_flags_seen_at = self._flags_seen_at if self.current_round_flags else 0.0
            self._seen_saved_at = time.time()
        try:
            self.session_manager.save_device(self.serial, self.profile)
        except Exception as e:
            logger.error(f"Không lưu được tiến độ máy {self.serial}: {e}")

    def _feed_post(self, page_id: str, post_idx: int) -> bool:
        """Advance payload to the next post index for this device's page."""
        p_info = self.profile.pages_data.get(page_id)
        if not p_info:
            return False
        if post_idx < 1 or post_idx > len(p_info.posts):
            self._feed_failed(p_info, post_idx, "không có dữ liệu bài này (kho nguồn thiếu bài)")
            return False

        target_post = p_info.posts[post_idx - 1]
        captions = p_info.caption_destinations or []

        target_dict = {
            "post_num": target_post.post_num,
            "video_file": target_post.video_file,
            "caption_file": target_post.caption_file,
            "comment_file": target_post.comment_file,
            "_payload_archive": getattr(target_post, "archive_dir", None),
        }
        try:
            transfer_payload(
                target_dict,
                p_info.video_dir,
                captions,
                [],  # comment is written later by the comment tracker
                p_info.fanpage_name,
                post_idx,
            )
        except Exception as e:
            self._feed_failed(p_info, post_idx, str(e))
            return False
        p_info.current_idx = post_idx
        p_info.feed_error = ""
        p_info.last_status = f"Đã nạp Bài {post_idx}"
        self.feed_retry_at.pop(page_id, None)
        self._save_progress()  # the source just left the store: record it at once
        queue_post_comment(self.comment_queue, self.serial, p_info, post_idx, target_dict)
        logger.info(
            f"✂ [MÁY {self.serial} | {self.profile.via_tag}] Đã chuyển bài {post_idx} của [{page_id}] khỏi kho."
        )
        return True

    def _feed_failed(self, p_info: PageInfo, post_idx: int, reason: str):
        """The next post could not be loaded: keep the page out of the rounds.

        Its running dir still holds the post that is already live, so flagging the page
        again would post that video twice. The feed is retried every FEED_RETRY_S.
        """
        page_id = p_info.page_id
        first_time = p_info.feed_error != reason
        p_info.feed_error = reason
        p_info.last_status = f"❌ Lỗi nạp bài {post_idx}: tạm ngưng page"
        self.feed_retry_at[page_id] = time.time() + FEED_RETRY_S
        self._save_progress()
        if not first_time:
            return
        logger.error(
            f"❌ [MÁY {self.serial} | {self.profile.via_tag}] Không nạp được bài {post_idx} cho "
            f"[{page_id}]: {reason}. Page tạm ngưng (không đăng lại bài cũ); tự thử lại mỗi "
            f"{FEED_RETRY_S}s."
        )
        self.notifier.send_message(
            f"❌ <b>[LỖI NẠP BÀI]</b>\n"
            f"• Thiết bị: <code>{self.serial}</code> ({self.profile.via_tag})\n"
            f"• Fanpage: <b>{p_info.fanpage_name}</b> ({page_id}) bài {post_idx}\n"
            f"• Lý do: {reason}\n"
            f"• Page tạm ngưng để không đăng trùng bài cũ; tool tự thử lại."
        )

    def _advance_page(self, page_id: str) -> bool:
        """The page's current post is live: load the next one, or close the page."""
        info = self.profile.pages_data[page_id]
        if info.current_idx >= info.total_posts:
            info.current_idx = info.total_posts + 1
            info.feed_error = ""
            info.last_status = "🎉 Hết bài"
            self._save_progress()
            logger.info(
                f"✅ [MÁY {self.serial}] [{page_id}] hoàn tất toàn bộ {info.total_posts} bài!"
            )
            return True
        if self.profile.page_off(page_id):  # turned off: the next post stays in the store
            logger.info(f"⛔ [MÁY {self.serial}] [{page_id}] page đã tắt: không nạp bài tiếp.")
            return False
        next_idx = info.current_idx + 1
        if self._feed_post(page_id, next_idx):
            logger.info(
                f"⚡ [MÁY {self.serial}] Đã bơm bài mới {next_idx}/{info.total_posts} cho [{page_id}]!"
            )
            return True
        return False

    def _retry_pending_feeds(self):
        """Load the next post of pages whose current post is live but not followed yet
        (feed failed earlier, or the tool stopped between posting and feeding)."""
        now = time.time()
        for pid, info in self.profile.pages_data.items():
            if self.profile.page_off(pid):
                continue
            if info.needs_feed and self.feed_retry_at.get(pid, 0) <= now:
                self._advance_page(pid)

    def _init_done_round(self) -> bool:
        """Seed indicator flag files specifically onto THIS phone.

        Only pages whose running dir holds a post not yet live take part.
        """
        self._retry_pending_feeds()
        active = [
            pid for pid, info in self.profile.pages_data.items()
            if info.ready_to_post and not self.profile.page_off(pid)
        ]
        if not active:
            return False
        if not self.adb.init_round_flags(self.serial, active):
            return False

        self.round_number += 1
        self.profile.round_number = self.round_number
        self.active_round_pages = active
        self.current_round_flags = set(active)
        self._flags_seen_at = time.time()
        self.round_succeeded = set()
        self.round_failed = set()
        self.pending_feed = {}
        self.last_flag_change_time = time.time()
        self.sentinel_active = True
        self._save_progress()
        logger.info(
            f"🚩 [MÁY {self.serial} | {self.profile.via_tag} | VÒNG {self.round_number}] "
            f"Đã nạp {len(active)} cờ Done lên điện thoại ({', '.join(active)})."
        )
        return True

    def _reconcile_away_posts(self) -> bool:
        """Pages of the saved round whose Done flag vanished while no worker watched
        (tool closed / stopped while AKPhoneS kept running) were posted then: count them
        as posted instead of flagging the same video again. False = phone unreadable."""
        saved = [pid for pid in self.profile.round_flags if pid in self.profile.pages_data]
        if saved:
            present = self.adb.list_done_flags(self.serial)
            if present is None:
                return False  # never guess: an unreadable folder is not "flags removed"
            now = time.time()
            since = self.profile.round_flags_seen_at or now
            for pid in saved:
                info = self.profile.pages_data[pid]
                if pid in present or info.is_completed or info.posted_idx >= info.current_idx:
                    continue
                info.posted_idx = info.current_idx
                info.fail_streak = 0
                info.last_status = f"Hoàn tất Bài {info.current_idx} (đăng lúc tool tắt)"
                if self.comment_queue is not None:
                    self.comment_queue.mark_posted_away(self.serial, pid, since, now)
                logger.warning(
                    f"🔎 [MÁY {self.serial} | {self.profile.via_tag}] [{pid}] cờ Done đã mất lúc tool tắt: "
                    f"AKPhoneS đã đăng bài {info.current_idx}. Không cắm cờ lại, nạp bài kế tiếp."
                )
        self._reconciled = True
        self._save_progress()
        return True

    def _all_posted(self) -> bool:
        return all(
            info.is_completed or self.profile.page_off(pid) for pid, info in self.profile.pages_data.items()
        )

    def _pages_failed(self, pids: List[str], status: str, reason: str):
        """These pages failed this round: one screenshot + alert + Facebook restart for all,
        and a page failing MAX rounds in a row is paused instead of retried forever."""
        if not pids:
            return
        limit = max(1, int(getattr(self.config, "max_page_fail_rounds", 3) or 3))
        paused = []
        for pid in pids:
            self.current_round_flags.discard(pid)
            self.round_failed.add(pid)
            info = self.profile.pages_data[pid]
            info.fail_streak = (info.fail_streak or 0) + 1
            info.last_status = status
            logger.warning(f"⚠️ [MÁY {self.serial}] [{pid}] {status}: {reason} (lỗi {info.fail_streak} vòng liên tiếp)")
            if info.fail_streak >= limit:
                info.paused_reason = f"Lỗi {info.fail_streak} vòng liên tiếp ({status})"
                info.last_status = f"⛔ Tạm ngưng: lỗi {info.fail_streak} vòng liên tiếp"
                paused.append(pid)
        self._capture_and_notify_error(pids[0], reason, pids)
        if paused:
            message = (
                f"⛔ <b>[PAGE TẠM NGƯNG]</b>\n• Thiết bị: <code>{self.serial}</code> ({self.profile.via_tag})\n"
                f"• Page: {', '.join(paused)}\n• Lỗi {limit} vòng liên tiếp, tool không chạy lại nữa.\n"
                "• Sửa xong thì Dừng rồi Chạy lại máy này để thử lại."
            )
            logger.error(
                f"⛔ [MÁY {self.serial}] Tạm ngưng {', '.join(paused)} sau {limit} vòng lỗi liên tiếp. "
                "Sửa xong thì Dừng rồi Chạy lại máy để thử lại."
            )
            self.notifier.send_message(message)
        self._save_progress()

    def _capture_and_notify_error(self, pid: str, reason: str, pids: Optional[List[str]] = None):
        """Take screenshot of device and alert user via Telegram."""
        now_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = self.config.base_dir / "logs" / "screenshots"
        screencap_file = log_dir / f"error_{self.serial}_{pid}_{now_ts}.png"

        if self.config.auto_screencap:
            self.adb.take_screenshot(self.serial, screencap_file)

        pages = ", ".join(
            f"{self.profile.pages_data.get(p, PageInfo('', '', p, 0)).fanpage_name} ({p})" for p in (pids or [pid])
        )
        msg = (
            f"⚠️ <b>[CẢNH BÁO LỖI DÀN MÁY]</b>\n"
            f"• Thiết bị: <code>{self.serial}</code> ({self.profile.via_tag})\n"
            f"• Fanpage: <b>{pages}</b>\n"
            f"• Lý do: {reason}\n"
            f"• Watchdog đã kích hoạt tự phục hồi trên máy này."
        )
        self.notifier.send_message(msg)

        if self.config.auto_screencap and screencap_file.is_file():
            self.notifier.send_photo(screencap_file, caption=f"Màn hình lỗi {self.serial} - {pid}")

        # Watchdog healing on this phone
        self.adb.force_stop_facebook(self.serial)

    def _run(self):
        base_interval = max(0.8, float(self.config.check_interval))
        round_initialized = False

        # Add small initial random jitter to avoid all workers hammering ADB at the same millisecond
        time.sleep(random.uniform(0.1, 0.6))

        while not self.stop_event.is_set():
            # Fast check if target device disconnected or offline
            if not self.adb.is_device_ready(self.serial):
                self.profile.status = "⚠️ Mất kết nối USB"
                logger.warning(
                    f"🔌 [MÁY {self.serial} | {self.profile.via_tag}] MẤT KẾT NỐI USB! Tạm dừng giám sát..."
                )
                if self.config.telegram.enabled:
                    self.notifier.send_message(
                        f"🔌 <b>[CẢNH BÁO MẤT KẾT NỐI USB]</b>\n"
                        f"• Thiết bị: <code>{self.serial}</code> ({self.profile.via_tag})\n"
                        f"• Trạng thái: Mất kết nối hoặc offline!\n"
                        f"• Tiến trình đã tự động tạm dừng. Vui lòng kiểm tra lại cáp kết nối."
                    )
                self.on_progress_update()

                while not self.stop_event.is_set():
                    time.sleep(2.5)
                    if self.adb.is_device_ready(self.serial):
                        logger.info(
                            f"🔌 [MÁY {self.serial} | {self.profile.via_tag}] ĐÃ KẾT NỐI LẠI! Tiếp tục giám sát."
                        )
                        self.profile.status = "🟢 Đang chạy"
                        self.last_flag_change_time = time.time()
                        self.on_progress_update()
                        break
                if self.stop_event.is_set():
                    break

            time.sleep(base_interval)

            if not self._reconciled and not self._reconcile_away_posts():
                continue

            # Initialize first round on this phone
            if not round_initialized:
                if not self._init_done_round():
                    if self._all_posted():
                        self.profile.status = "🎉 Hoàn tất toàn bộ"
                        break
                    if any(info.feed_error for info in self.profile.pages_data.values()):
                        self.profile.status = "⚠️ Chờ sửa lỗi nạp bài"
                        self.on_progress_update()
                    elif any(info.paused_reason for info in self.profile.pages_data.values()):
                        self.profile.status = "⛔ Page tạm ngưng (lỗi liên tiếp)"
                        self.on_progress_update()
                    time.sleep(2)
                    continue
                self.profile.status = "🟢 Đang chạy"
                round_initialized = True
                self.on_progress_update()
                continue

            try:
                files_in_done = self.adb.list_done_flags(self.serial)
                if files_in_done is None:
                    time.sleep(1)
                    continue
                self._flags_seen_at = time.time()
                if self.current_round_flags and self._flags_seen_at - self._seen_saved_at >= SEEN_SAVE_S:
                    self._save_progress()  # keeps "last seen" fresh for the away window

                # 0. Check sentinel flag round_done.txt on this phone
                sentinel_triggered = False
                if self.sentinel_active and ("round_done" not in files_in_done):
                    self.sentinel_active = False
                    sentinel_triggered = True
                    logger.info(
                        f"🎯 [MÁY {self.serial} | {self.profile.via_tag}] Cờ chốt sổ [round_done.txt] ĐÃ BỊ XÓA!"
                    )
                    # Pages still flagged are only marked failed AFTER the flags removed in
                    # this same poll count as done (step 2b): removedone.txt runs right after
                    # the last page, so both flags often vanish within one poll.

                # Flags removed in current round on this phone
                disappeared = [
                    pid
                    for pid in self.active_round_pages
                    if pid in self.current_round_flags and pid not in files_in_done
                ]

                if disappeared:
                    self.last_flag_change_time = time.time()
                    self._timeouts_in_row = 0

                for pid in disappeared:
                    target_idx = self.active_round_pages.index(pid)

                    # 1. Skip / Jump detection
                    skipped = [
                        prev_pid for prev_pid in self.active_round_pages[:target_idx]
                        if prev_pid in self.current_round_flags
                    ]
                    if skipped:
                        self._pages_failed(
                            skipped, "Bị bỏ qua/Lỗi", "Bị nhảy cóc/kịch bản đứng trước bị lỗi"
                        )

                    # 2. Page succeeded
                    self.current_round_flags.discard(pid)
                    self.round_succeeded.add(pid)
                    info = self.profile.pages_data[pid]
                    c_idx = info.current_idx
                    total = info.total_posts
                    info.last_status = f"Hoàn tất Bài {c_idx}"
                    info.posted_idx = max(info.posted_idx, c_idx)
                    info.fail_streak = 0
                    self._save_progress()  # never flag this post again, even after a crash
                    if self.comment_queue is not None:
                        self.comment_queue.mark_posted(self.serial, pid)
                    logger.info(
                        f"🎉 [MÁY {self.serial} | {self.profile.via_tag}] Xong bài {c_idx}/{total} của [{pid}]!"
                    )

                    # Queue safe feed
                    self.pending_feed[pid] = {
                        "time": time.time(),
                        "target_idx": target_idx,
                        "c_idx": c_idx,
                        "total": total,
                    }
                    self.on_progress_update()

                # 2b. Sentinel reached: pages whose flag is still there failed this round
                if sentinel_triggered and self.current_round_flags:
                    stuck = [p for p in self.active_round_pages if p in self.current_round_flags]
                    self._pages_failed(stuck, "Lỗi (Bỏ qua)", "Cờ chưa kịp xóa khi chạm cờ chốt sổ")
                    self.on_progress_update()

                # Process safe delayed feed
                if self.pending_feed:
                    now = time.time()
                    to_feed = []
                    for p_key, pdata in list(self.pending_feed.items()):
                        has_subsequent = any(
                            self.active_round_pages.index(s) > pdata["target_idx"]
                            for s in self.round_succeeded
                            if s != p_key
                        )
                        if has_subsequent or (now - pdata["time"] >= 20):
                            to_feed.append(p_key)

                    for p_key in to_feed:
                        self.pending_feed.pop(p_key)
                        self._advance_page(p_key)
                        self.on_progress_update()

                # 3. Timeout Watchdog
                timeout_limit = self.config.watchdog_timeout
                if self.current_round_flags and (
                    time.time() - self.last_flag_change_time > timeout_limit
                ):
                    waiting = [p for p in self.active_round_pages if p in self.current_round_flags]
                    if self._timeouts_in_row == 0:
                        # the page AKPhoneS is on is stuck: only that one failed; Facebook is
                        # restarted and the next pages get a fresh window
                        self._pages_failed(
                            waiting[:1], "Timeout treo máy", f"Treo quá {timeout_limit} giây không phản hồi"
                        )
                    else:
                        # still nothing after the recovery: AKPhoneS is not running this round
                        self._pages_failed(
                            waiting, "Timeout treo máy",
                            f"Không page nào chạy tiếp sau khi tự phục hồi ({timeout_limit} giây)",
                        )
                    self._timeouts_in_row += 1
                    self.last_flag_change_time = time.time()
                    self.on_progress_update()

                # 4. End of round check
                processed_count = len(self.round_succeeded) + len(self.round_failed)
                if processed_count >= len(self.active_round_pages) or sentinel_triggered:
                    # Flush pending feeds
                    for p_key in list(self.pending_feed):
                        self._advance_page(p_key)
                    self.pending_feed.clear()
                    self.on_progress_update()

                    logger.info(
                        f"🏁 [MÁY {self.serial} | {self.profile.via_tag}] KẾT THÚC VÒNG {self.round_number}! "
                        f"Thành công: {len(self.round_succeeded)}, Lỗi: {len(self.round_failed)}."
                    )

                    if self.config.auto_clean_storage:
                        self.adb.clean_storage(self.serial)

                    if getattr(self.config, "auto_clean_payloads", True):
                        retention_days = getattr(self.config, "payload_retention_days", 0)
                        keep = self.comment_queue.protected_archives() if self.comment_queue else None
                        clean_completed_payloads(
                            self.config.running_dir, max_age_days=retention_days, keep=keep
                        )

                    if self.config.telegram.enabled:
                        round_msg = (
                            f"🏁 <b>[VÒNG {self.round_number} - {self.profile.via_tag}]</b>\n"
                            f"• Thiết bị: <code>{self.serial}</code>\n"
                            f"• Thành công: <b>{len(self.round_succeeded)}</b>\n"
                            f"• Lỗi: <b>{len(self.round_failed)}</b>"
                        )
                        self.notifier.send_message(round_msg)

                    still_has_posts = [
                        p for p, inf in self.profile.pages_data.items()
                        if not inf.is_completed and not self.profile.page_off(p)
                    ]
                    if not still_has_posts:
                        self.profile.status = "🎉 Hoàn tất toàn bộ"
                        logger.info(
                            f"🎊 [MÁY {self.serial} | {self.profile.via_tag}] ĐÃ ĐĂNG HẾT TOÀN BỘ KHO BÀI!"
                        )
                        break
                    else:
                        logger.info(
                            f"🔄 [MÁY {self.serial} | {self.profile.via_tag}] Chờ 20 giây trước khi chuẩn bị Vòng {self.round_number + 1} ({len(still_has_posts)} Page cần chạy)..."
                        )
                        time.sleep(20)
                        if self.stop_event.is_set():
                            break
                        if self._init_done_round():
                            self.on_progress_update()
                        else:
                            round_initialized = False

            except Exception as e:
                logger.error(f"Lỗi worker máy {self.serial}: {e}", exc_info=True)

        if self.stop_event.is_set() or not self._all_posted():
            self.profile.status = "⏹️ Đã dừng"
        self._save_progress()
        self.on_progress_update()


class FarmFeederManager:
    """Manages concurrent worker threads across all phones in the farm."""

    def __init__(
        self,
        config: AppConfig,
        adb_service: AdbService,
        notifier: TelegramNotifier,
        session_manager: SessionManager,
        on_progress_update: Callable[[], None],
        comment_queue: Optional[CommentQueue] = None,
    ):
        self.config = config
        self.adb = adb_service
        self.notifier = notifier
        self.session_manager = session_manager
        self.on_progress_update = on_progress_update
        self.comment_queue = comment_queue
        self.workers: Dict[str, DeviceFeederWorker] = {}
        self.lock = threading.Lock()

    def start_device(self, serial: str, profile: DeviceProfile) -> bool:
        """Start a dedicated feeder worker for a specific device."""
        with self.lock:
            if serial in self.workers and self.workers[serial].is_running():
                return True
            if not profile.pages_data:
                logger.warning(
                    f"Máy {serial} chưa có dữ liệu kịch bản; vui lòng khởi tạo trước."
                )
                return False

            worker = DeviceFeederWorker(
                serial=serial,
                profile=profile,
                config=self.config,
                adb_service=self.adb,
                notifier=self.notifier,
                session_manager=self.session_manager,
                on_progress_update=self.on_progress_update,
                comment_queue=self.comment_queue,
            )
            self.workers[serial] = worker
            worker.start()
            return True

    def stop_device(self, serial: str):
        """Stop feeder worker for a specific device."""
        with self.lock:
            if serial in self.workers:
                self.workers[serial].stop()

    def start_all(self, profiles: Dict[str, DeviceProfile]) -> int:
        """Launch feeder workers for all enabled device profiles."""
        count = 0
        for serial, profile in profiles.items():
            if profile.enabled and profile.pages_data:
                if self.start_device(serial, profile):
                    count += 1
        return count

    def stop_all(self):
        """Halt all active device workers."""
        with self.lock:
            for worker in self.workers.values():
                worker.stop()

    def is_running(self, serial: Optional[str] = None) -> bool:
        if serial:
            return bool(serial in self.workers and self.workers[serial].is_running())
        return any(w.is_running() for w in self.workers.values())
