"""Main graphical interface supporting Multi-Device Phone Farm orchestration."""

import queue
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from akphone_dispatcher import __version__
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.ledger import Ledger
from akphone_dispatcher.core.models import (
    DeviceInfo,
    DeviceProfile,
    next_free_fb_index,
)
from akphone_dispatcher.gui.tabs.farm_tab import FarmTabMixin
from akphone_dispatcher.gui.tabs.progress_tab import ProgressTabMixin
from akphone_dispatcher.gui.tabs.settings_tab import SettingsTabMixin
from akphone_dispatcher.gui.tabs.template_tab import TemplateTabMixin
from akphone_dispatcher.gui.workers.feeder_worker import FarmFeederManager
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.services.session import SessionManager
from akphone_dispatcher.utils.helpers import sanitize_fb_index
from akphone_dispatcher.services.farm_service import restore_farm_session
from akphone_dispatcher.utils.helpers import get_app_base_dir
from akphone_dispatcher.utils.logger import logger

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class AKPhoneApp(FarmTabMixin, ProgressTabMixin, TemplateTabMixin, SettingsTabMixin, ctk.CTk):
    """Multi-device phone farm dispatcher and feeder application."""

    def __init__(self):
        super().__init__()
        self.title(f"⚡ AKPhone Multi-Device Farm Dispatcher & Feeder v{__version__}")
        self.geometry("1180x860")
        self.minsize(1040, 740)

        # Core services
        self.config = AppConfig()
        self.adb = AdbService(self.config.adb_path)
        self.notifier = TelegramNotifier(self.config.telegram)
        self.session_manager = SessionManager()

        # Multi-device state
        self.connected_devices: List[DeviceInfo] = []
        self.device_profiles: Dict[str, DeviceProfile] = {}
        self.selected_serial: Optional[str] = None
        self._is_updating_selection: bool = False

        # Thread-safe UI dispatch queue
        self.ui_queue: queue.Queue = queue.Queue()
        self._start_queue_poller()

        # Feeder manager
        self.ledger = Ledger(get_app_base_dir() / "configs" / "ledger.db")
        self.feeder_manager = FarmFeederManager(
            ledger=self.ledger,
            config=self.config,
            adb_service=self.adb,
            notifier=self.notifier,
            session_manager=self.session_manager,
            on_progress_update=self._update_progress_display,
        )

        # Build UI layout
        self._create_header()
        self._create_tabs()
        self._create_log_box()

        # Connect logger to GUI
        logger.add_listener(self._on_log_received)

        # Initial ADB scan & session load
        self.after(500, self._refresh_devices_async)
        self._session_checked = False

    def _start_queue_poller(self):
        self._process_ui_queue()

    def _process_ui_queue(self):
        try:
            while True:
                callback = self.ui_queue.get_nowait()
                try:
                    callback()
                except Exception as e:
                    logger.error(f"Lỗi thực thi giao diện: {e}")
        except queue.Empty:
            pass
        finally:
            self.after(40, self._process_ui_queue)

    def dispatch_ui(self, callback: Callable[[], None]):
        """Safely dispatch a callback to be run on the main Tkinter thread."""
        self.ui_queue.put(callback)

    def _create_header(self):
        header = ctk.CTkFrame(self, corner_radius=8, fg_color="#181b24")
        header.pack(fill="x", padx=12, pady=(10, 4))

        title = ctk.CTkLabel(
            header,
            text=f"⚡ AKPHONE FARM DISPATCHER v{__version__}",
            font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
            text_color="#38bdf8",
        )
        title.pack(side="left", padx=14, pady=8)

        # Farm Global Actions on the right
        btn_frame = ctk.CTkFrame(header, fg_color="transparent")
        btn_frame.pack(side="right", padx=10, pady=6)

        self.btn_start_all = ctk.CTkButton(
            btn_frame,
            text="▶️ BẬT TẤT CẢ (START ALL)",
            width=190,
            height=32,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#10b981",
            hover_color="#059669",
            command=self._start_all_devices,
        )
        self.btn_start_all.pack(side="left", padx=(0, 8))

        self.btn_stop_all = ctk.CTkButton(
            btn_frame,
            text="⏹️ DỪNG TẤT CẢ",
            width=130,
            height=32,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#ef4444",
            hover_color="#dc2626",
            command=self._stop_all_devices,
        )
        self.btn_stop_all.pack(side="left", padx=(0, 10))

        btn_refresh_dev = ctk.CTkButton(
            btn_frame,
            text="🔄 Quét ADB",
            width=90,
            height=32,
            fg_color="#374151",
            command=self._refresh_devices_async,
        )
        btn_refresh_dev.pack(side="left", padx=(0, 10))

        self.lbl_global_status = ctk.CTkLabel(
            btn_frame,
            text="Đang quét thiết bị...",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#f59e0b",
        )
        self.lbl_global_status.pack(side="left", padx=(0, 4))

    def _create_tabs(self):
        self.tabview = ctk.CTkTabview(self, corner_radius=8)
        self.tabview.pack(fill="both", expand=True, padx=12, pady=4)

        self.tab_farm = self.tabview.add("📱 1. Quản Lý Dàn Máy (Device Farm)")
        self.tab_progress = self.tabview.add("📊 2. Tiến Độ Thời Gian Thực (Live Farm)")
        self.tab_template = self.tabview.add("📝 3. Kịch Bản Mẫu (Template)")
        self.tab_settings = self.tabview.add("🛡️ 4. Cài Đặt Hệ Thống & Telegram")

        self._build_tab_farm()
        self._build_tab_progress()
        self._build_tab_template()
        self._build_tab_settings()





    def _create_log_box(self):
        log_box = ctk.CTkFrame(self, corner_radius=8, fg_color="#111827")
        log_box.pack(fill="x", padx=12, pady=(4, 10))

        log_head = ctk.CTkFrame(log_box, fg_color="transparent")
        log_head.pack(fill="x", padx=8, pady=(4, 2))

        ctk.CTkLabel(
            log_head,
            text="📋 Nhật ký hoạt động dàn máy:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#94a3b8",
        ).pack(side="left")

        ctk.CTkButton(
            log_head,
            text="Xóa Log",
            width=55,
            height=20,
            font=ctk.CTkFont(size=11),
            fg_color="#374151",
            command=lambda: self.log_text.delete("1.0", "end"),
        ).pack(side="right")

        self.log_text = ctk.CTkTextbox(
            log_box, height=85, font=ctk.CTkFont(family="Consolas", size=11)
        )
        self.log_text.pack(fill="both", padx=8, pady=(2, 6))

    def _on_log_received(self, level: str, message: str):
        def append():
            try:
                line_count = int(self.log_text.index("end-1c").split(".")[0])
                if line_count > 1000:
                    self.log_text.delete("1.0", "200.0")
                self.log_text.insert("end", f"{message}\n")
                self.log_text.see("end")
            except Exception:
                pass

        self.dispatch_ui(append)

    def _refresh_device_row(self, serial: str):
        """Redraw one row of the device table from its profile."""
        prof = self.device_profiles.get(serial)
        if prof is None:
            return
        info = prof.device_info
        model = f"{info.brand} {info.model}" if info else "Android"
        try:
            self.device_tree.item(
                serial,
                values=(serial, model, prof.via_tag, prof.progress_summary, prof.status),
            )
        except tk.TclError:
            pass


    def _refresh_devices_async(self):
        adb_path = (
            self.entry_adb_path.get().strip()
            if hasattr(self, "entry_adb_path")
            else self.config.adb_path
        )
        threading.Thread(
            target=self._worker_refresh_devices, args=(adb_path,), daemon=True
        ).start()

    def _worker_refresh_devices(self, adb_path: str = ""):
        if adb_path:
            self.adb.adb_path = adb_path
        devices = self.adb.get_connected_devices()
        self.connected_devices = devices

        # Update profiles dictionary
        for idx, d in enumerate(devices, start=1):
            if d.serial not in self.device_profiles:
                used = {str(p.fb_index) for p in self.device_profiles.values()}
                used |= {
                    str(c.get("fb_index"))
                    for s_, c in self.config.device_profiles.items()
                    if s_ != d.serial and c.get("fb_index")
                }
                saved_cfg = self.config.get_profile(
                    d.serial, default_fb_idx=next_free_fb_index(used)
                )
                prof = DeviceProfile(
                    serial=d.serial,
                    fb_index=sanitize_fb_index(saved_cfg.get("fb_index"), next_free_fb_index(used)),
                    source_dir=saved_cfg.get("source_dir", self.config.source_dir),
                    running_dir=self.config.running_dir,
                    page_names=saved_cfg.get("page_names", self.config.page_names),
                    enabled=saved_cfg.get("enabled", True),
                    device_info=d,
                )
                self.device_profiles[d.serial] = prof
            else:
                self.device_profiles[d.serial].device_info = d

        def update_ui():
            # Clear and repopulate Treeview
            for item in self.device_tree.get_children():
                self.device_tree.delete(item)

            for d in devices:
                prof = self.device_profiles[d.serial]
                model_str = f"{d.brand} {d.model}"
                self.device_tree.insert(
                    "",
                    "end",
                    iid=d.serial,
                    values=(
                        d.serial,
                        model_str,
                        prof.via_tag,
                        prof.progress_summary,
                        prof.status,
                    ),
                )

            if not self.adb.is_available():
                self.lbl_global_status.configure(
                    text="⚠️ Chưa cấu hình adb.exe (Tab Cài Đặt)", text_color="#ef4444"
                )
            else:
                ready_count = len([d for d in devices if d.is_ready])
                if ready_count > 0:
                    self.lbl_global_status.configure(
                        text=f"🟢 Đã kết nối: {ready_count} điện thoại", text_color="#10b981"
                    )
                else:
                    self.lbl_global_status.configure(
                        text="⚠️ Chưa tìm thấy thiết bị ADB", text_color="#f59e0b"
                    )

            if not self._session_checked:
                self._session_checked = True
                self.after(200, self._check_existing_session)

            # Auto select first item if none selected
            if devices and (not self.selected_serial or self.selected_serial not in [d.serial for d in devices]):
                self._select_device_serial(devices[0].serial)

        self.dispatch_ui(update_ui)


























    def _check_existing_session(self):
        session = self.session_manager.load_session()
        if not (session and session.get("mode") == "farm" and session.get("devices")):
            return
        devs = session["devices"]
        if messagebox.askyesno(
            "Khôi Phục Tiến Độ",
            f"Phát hiện phiên làm việc cũ gồm {len(devs)} máy.\nBạn có muốn khôi phục lại không?",
        ):
            restore_farm_session(session, self.device_profiles, self.config, ledger=self.ledger)
            self._update_progress_display()
            logger.info(f"Đã khôi phục thành công dữ liệu dàn {len(devs)} máy!")
