"""Multi-device concurrent worker and farm orchestration manager."""

from datetime import datetime
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import DeviceProfile, PageInfo
from akphone_dispatcher.core.payload import (
    clean_completed_payloads,
    mark_payload_posted,
    transfer_payload,
)
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.services.session import SessionManager
from akphone_dispatcher.utils.logger import logger


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
    ):
        self.serial = serial
        self.profile = profile
        self.config = config
        self.adb = adb_service
        self.notifier = notifier
        self.session_manager = session_manager
        self.on_progress_update = on_progress_update

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
        self.all_done_since = 0.0
        self.last_flag_change_time = time.time()

    def start(self):
        self.stop_event.clear()
        self.round_number = 0
        self.round_succeeded = set()
        self.round_failed = set()
        self.current_round_flags = set()
        self.pending_feed = {}
        self.sentinel_active = False
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

    def _feed_post(self, page_id: str, post_idx: int) -> bool:
        """Advance payload to the next post index for this device's page."""
        p_info = self.profile.pages_data.get(page_id)
        if not p_info or post_idx < 1 or post_idx > len(p_info.posts):
            return False

        target_post = p_info.posts[post_idx - 1]
        captions = p_info.caption_destinations or []
        comments = p_info.comment_destinations or []

        try:
            target_dict = {
                "post_num": target_post.post_num,
                "video_file": target_post.video_file,
                "caption_file": target_post.caption_file,
                "comment_file": target_post.comment_file,
            }
            archive = transfer_payload(
                target_dict,
                p_info.video_dir,
                captions,
                comments,
                p_info.fanpage_name,
                post_idx,
            )
            # Previous post is confirmed posted: its archive may now be cleaned up
            mark_payload_posted(p_info.active_archive)
            p_info.active_archive = archive
            p_info.current_idx = post_idx
            p_info.last_status = f"Đã nạp Bài {post_idx}"
            logger.info(
                f"✂ [MÁY {self.serial} | {self.profile.via_tag}] Đã chuyển bài {post_idx} của [{page_id}] khỏi kho."
            )
            return True
        except Exception as e:
            logger.error(
                f"Lỗi khi chuyển bài cho [{page_id}] trên máy {self.serial}: {e}"
            )
            return False

    def _queue_feed(self, pid: str):
        """A script runs exactly once per post: success or failure, the page moves on."""
        info = self.profile.pages_data.get(pid)
        if info is None or pid in self.pending_feed:
            return
        self.pending_feed[pid] = {
            "time": time.time(),
            "target_idx": self.active_round_pages.index(pid),
            "c_idx": info.current_idx,
            "total": info.total_posts,
        }

    def _init_done_round(self) -> bool:
        """Seed indicator flag files specifically onto THIS phone."""
        self.round_number += 1
        self.profile.round_number = self.round_number

        self.active_round_pages = [
            pid
            for pid, info in self.profile.pages_data.items()
            if info.current_idx <= info.total_posts
        ]
        if not self.active_round_pages:
            return False

        self.current_round_flags = set(self.active_round_pages)
        self.all_done_since = 0.0
        self.round_succeeded = set()
        self.round_failed = set()
        self.pending_feed = {}
        self.last_flag_change_time = time.time()

        if self.adb.init_round_flags(self.serial, self.active_round_pages):
            self.sentinel_active = True
            flags_str = ", ".join(self.active_round_pages)
            logger.info(
                f"🚩 [MÁY {self.serial} | {self.profile.via_tag} | VÒNG {self.round_number}] "
                f"Đã nạp {len(self.active_round_pages)} cờ Done lên điện thoại ({flags_str})."
            )
            return True
        return False

    def _capture_and_notify_error(self, pid: str, reason: str):
        """Take screenshot of device and alert user via Telegram."""
        now_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = self.config.base_dir / "logs" / "screenshots"
        screencap_file = log_dir / f"error_{self.serial}_{pid}_{now_ts}.png"

        if self.config.auto_screencap:
            self.adb.take_screenshot(self.serial, screencap_file)

        msg = (
            f"⚠️ <b>[CẢNH BÁO LỖI DÀN MÁY]</b>\n"
            f"• Thiết bị: <code>{self.serial}</code> ({self.profile.via_tag})\n"
            f"• Fanpage: <b>{self.profile.pages_data.get(pid, PageInfo('', '', pid, 0)).fanpage_name}</b> ({pid})\n"
            f"• Lý do: {reason}\n"
            f"• Watchdog đã kích hoạt tự phục hồi trên máy này."
        )
        tg = self.config.telegram
        if tg.notify_on_error:
            self.notifier.send_message(msg)
            if tg.send_screenshots and self.config.auto_screencap and screencap_file.is_file():
                self.notifier.send_photo(
                    screencap_file, caption=f"Màn hình lỗi {self.serial} - {pid}"
                )

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

            # Initialize first round on this phone
            if not round_initialized:
                if not self._init_done_round():
                    time.sleep(2)
                    continue
                round_initialized = True
                self.on_progress_update()
                continue

            try:
                files_in_done = self.adb.list_done_flags(self.serial)
                if files_in_done is None:
                    time.sleep(1)
                    continue

                # 0. Detect sentinel flag round_done.txt removal (handled AFTER normal flags,
                # so pages finishing in the same poll are not wrongly marked as failed)
                sentinel_triggered = False
                if self.sentinel_active and ("round_done" not in files_in_done):
                    self.sentinel_active = False
                    sentinel_triggered = True
                    logger.info(
                        f"🎯 [MÁY {self.serial} | {self.profile.via_tag}] Cờ chốt sổ [round_done.txt] ĐÃ BỊ XÓA!"
                    )

                # Flags removed in current round on this phone
                disappeared = [
                    pid
                    for pid in self.active_round_pages
                    if pid in self.current_round_flags and pid not in files_in_done
                ]

                if disappeared:
                    self.last_flag_change_time = time.time()

                for pid in disappeared:
                    target_idx = self.active_round_pages.index(pid)

                    # 1. Skip / Jump detection
                    for prev_pid in self.active_round_pages[:target_idx]:
                        if prev_pid in self.current_round_flags:
                            self.current_round_flags.discard(prev_pid)
                            self.round_failed.add(prev_pid)
                            self._queue_feed(prev_pid)
                            self.profile.pages_data[prev_pid].last_status = "Bị bỏ qua/Lỗi"
                            logger.warning(
                                f"⚠️ [MÁY {self.serial}] Phát hiện [{prev_pid}] bị bỏ qua/lỗi kịch bản!"
                            )
                            self._capture_and_notify_error(
                                prev_pid, "Bị nhảy cóc/kịch bản đứng trước bị lỗi"
                            )

                    # 2. Page succeeded
                    self.current_round_flags.discard(pid)
                    self.round_succeeded.add(pid)
                    info = self.profile.pages_data[pid]
                    c_idx = info.current_idx
                    total = info.total_posts
                    info.last_status = f"Hoàn tất Bài {c_idx}"
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

                if sentinel_triggered and self.current_round_flags:
                    for stuck_pid in list(self.current_round_flags):
                        self.current_round_flags.discard(stuck_pid)
                        self.round_failed.add(stuck_pid)
                        self._queue_feed(stuck_pid)
                        self.profile.pages_data[stuck_pid].last_status = "Lỗi (Bỏ qua)"
                        self._capture_and_notify_error(
                            stuck_pid, "Cờ chưa kịp xóa khi chạm cờ chốt sổ"
                        )
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
                        pdata = self.pending_feed.pop(p_key)
                        c_idx = pdata["c_idx"]
                        total = pdata["total"]
                        if c_idx < total:
                            next_idx = c_idx + 1
                            if self._feed_post(p_key, next_idx):
                                logger.info(
                                    f"⚡ [MÁY {self.serial}] Đã bơm bài mới {next_idx}/{total} cho [{p_key}]!"
                                )
                        else:
                            mark_payload_posted(self.profile.pages_data[p_key].active_archive)
                            self.profile.pages_data[p_key].current_idx = total + 1
                            self.profile.pages_data[p_key].last_status = "🎉 Hết bài"
                            logger.info(
                                f"✅ [MÁY {self.serial}] [{p_key}] hoàn tất toàn bộ {total} bài!"
                            )
                        self.on_progress_update()

                # 3. Timeout Watchdog
                timeout_limit = self.config.watchdog_timeout
                if self.current_round_flags and (
                    time.time() - self.last_flag_change_time > timeout_limit
                ):
                    for stuck_pid in list(self.current_round_flags):
                        self.current_round_flags.discard(stuck_pid)
                        self.round_failed.add(stuck_pid)
                        self._queue_feed(stuck_pid)
                        self.profile.pages_data[stuck_pid].last_status = "Timeout treo máy"
                        logger.warning(
                            f"⏱️ [MÁY {self.serial}] [{stuck_pid}] quá {timeout_limit}s không phản hồi. Watchdog kích hoạt."
                        )
                        self._capture_and_notify_error(
                            stuck_pid, f"Treo quá {timeout_limit} giây không phản hồi"
                        )
                    self.on_progress_update()

                # 4. End of round check
                processed_count = len(self.round_succeeded) + len(self.round_failed)
                all_processed = processed_count >= len(self.active_round_pages)
                if all_processed and self.sentinel_active and not sentinel_triggered:
                    # Every page is done but round_done.txt is still on the phone: wait for the
                    # AKPhone closing script, otherwise its late removal would hit the NEXT round.
                    if not self.all_done_since:
                        self.all_done_since = time.time()
                        logger.info(
                            f"⏳ [MÁY {self.serial}] Các page đã xong, chờ kịch bản chốt sổ xóa round_done..."
                        )
                    grace = min(max(float(self.config.watchdog_timeout), 30.0), 180.0)
                    if time.time() - self.all_done_since < grace:
                        all_processed = False
                if all_processed or sentinel_triggered:
                    # Flush pending feeds
                    for p_key, pdata in list(self.pending_feed.items()):
                        c_idx = pdata["c_idx"]
                        total = pdata["total"]
                        if c_idx < total:
                            self._feed_post(p_key, c_idx + 1)
                        else:
                            mark_payload_posted(self.profile.pages_data[p_key].active_archive)
                            self.profile.pages_data[p_key].current_idx = total + 1
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
                        clean_completed_payloads(self.config.running_dir, max_age_days=retention_days)

                    if self.config.telegram.enabled and self.config.telegram.notify_on_success:
                        round_msg = (
                            f"🏁 <b>[VÒNG {self.round_number} - {self.profile.via_tag}]</b>\n"
                            f"• Thiết bị: <code>{self.serial}</code>\n"
                            f"• Thành công: <b>{len(self.round_succeeded)}</b>\n"
                            f"• Lỗi: <b>{len(self.round_failed)}</b>"
                        )
                        self.notifier.send_message(round_msg)

                    still_has_posts = [
                        p
                        for p, inf in self.profile.pages_data.items()
                        if inf.current_idx <= inf.total_posts
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
                logger.error(f"Lỗi worker máy {self.serial}: {e}")

        self.profile.status = "⏹️ Đã dừng"
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
    ):
        self.config = config
        self.adb = adb_service
        self.notifier = notifier
        self.session_manager = session_manager
        self.on_progress_update = on_progress_update
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
