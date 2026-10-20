"""Main graphical interface supporting Multi-Device Phone Farm orchestration."""

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

import customtkinter as ctk

from akphone_dispatcher import __version__
from akphone_dispatcher.core.comment_queue import REPOST_STAGED, CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.fb_profiles import FbProfileStore
from akphone_dispatcher.core.models import (
    DeviceInfo,
    DeviceProfile,
    PageInfo,
    PostItem,
    TelegramConfig,
)
from akphone_dispatcher.core.payload import clean_completed_payloads, transfer_payload
from akphone_dispatcher.core.script_templates import (
    check_cmt_template,
    check_post_template,
    render_post_script,
)
from akphone_dispatcher.core.scanner import (
    check_store,
    page_id_for,
    page_name_for,
    page_number,
    parse_page_links_mapping,
    parse_page_names_mapping,
    scan_source_directory,
)
from akphone_dispatcher.gui.comment_tab import CommentTabMixin
from akphone_dispatcher.gui.update_mixin import UpdateMixin
from akphone_dispatcher.gui.workers.feeder_worker import FarmFeederManager, queue_post_comment
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.comment_tracker import (
    IDLE_CMT_SCRIPT,
    CommentTracker,
    _write_atomic,
    cmt_script_filename,
    comment_filename,
    render_cmt_script,
)
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.services.session import SessionManager, restore_device_profiles
from akphone_dispatcher.utils.bootstrap import ensure_runtime_files
from akphone_dispatcher.utils.helpers import resolve_akphone_layout
from akphone_dispatcher.utils.housekeeping import (
    CleanReport,
    clean_browser_cache,
    clean_stuck_payloads,
    delete_older_than,
)
from akphone_dispatcher.utils.logger import logger

HOUSEKEEPING_EVERY_MS = 6 * 3600 * 1000


def template_reports(txt: str, cmt: str, flag_dir: str):
    """(post template report, comment template report or None when there is none)."""
    return check_post_template(txt), (check_cmt_template(cmt, flag_dir) if cmt.strip() else None)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class AKPhoneApp(CommentTabMixin, UpdateMixin, ctk.CTk):
    """Multi-device phone farm dispatcher and feeder application."""

    def __init__(self):
        super().__init__()
        self.title(f"⚡ AKPhone Multi-Device Farm Dispatcher & Feeder v{__version__}")
        self.geometry("1180x860")
        self.minsize(1040, 740)

        # A bare exe writes its templates, default settings and guide on first run
        created = ensure_runtime_files()
        if created:
            logger.info(f"🆕 Đã tạo file mặc định: {', '.join(created)}")

        # Core services
        self.config = AppConfig()
        self.adb = AdbService(self.config.adb_path)
        self.notifier = TelegramNotifier(self.config.telegram)
        self.session_manager = SessionManager()
        self.comment_queue = CommentQueue(self.config.comment_queue_path)
        self.fb_profiles = FbProfileStore(
            self.config.fb_profiles_path, self.config.browser_profile_dir
        )
        self.comment_tracker = CommentTracker(
            self.config,
            self.adb,
            self.notifier,
            self.comment_queue,
            is_feeding=lambda serial: self.feeder_manager.is_running(serial),
            profile_store=self.fb_profiles,
            is_page_off=self._is_page_off,
        )

        # Multi-device state
        self.connected_devices: List[DeviceInfo] = []
        self.device_profiles: Dict[str, DeviceProfile] = {}
        self.selected_serial: Optional[str] = None
        self._is_updating_selection: bool = False

        self._housekeeping_lock = threading.Lock()

        # Thread-safe UI dispatch queue
        self.ui_queue: queue.Queue = queue.Queue()
        self._start_queue_poller()

        # Feeder manager
        self.feeder_manager = FarmFeederManager(
            config=self.config,
            adb_service=self.adb,
            notifier=self.notifier,
            session_manager=self.session_manager,
            on_progress_update=self._update_progress_display,
            comment_queue=self.comment_queue,
        )

        # Build UI layout
        self._create_header()
        self._create_tabs()
        self._create_log_box()

        # Connect logger to GUI
        logger.add_listener(self._on_log_received)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Initial ADB scan & session load
        self.after(500, self._refresh_devices_async)
        self.after(1200, self._check_existing_session)
        if self.config.comment.enabled:
            self.after(1500, self._startup_housekeeping)
        else:
            self.after(1500, lambda: self._startup_housekeeping(start_tracker=False))
        self.after(HOUSEKEEPING_EVERY_MS, self._periodic_housekeeping)

    # ---------- closing ----------
    def _on_close(self):
        """Window X: stop every thread, close the scan browsers, flush data, then quit."""
        if getattr(self, "_closing", False):
            return
        busy = self.feeder_manager.is_running() or self.comment_tracker.is_alive()
        if busy and not messagebox.askyesno(
            "Đóng Tool",
            "Tool đang chạy (đăng bài và/hoặc theo dõi comment).\n"
            "Đóng sẽ dừng hết, đóng trình duyệt quét và lưu tiến độ. Đóng tool?",
        ):
            return
        self._closing = True
        try:
            self.lbl_global_status.configure(
                text="⏳ Đang dừng các luồng, đóng trình duyệt...", text_color="#f59e0b"
            )
        except Exception:
            pass
        threading.Thread(target=self._shutdown, name="Shutdown", daemon=True).start()

    def _shutdown(self, timeout_s: float = 40, before_exit: Optional[Callable[[], Any]] = None):
        deadline = time.time() + timeout_s
        try:
            self.feeder_manager.stop_all()
            self.comment_tracker.stop()
            for worker in list(self.feeder_manager.workers.values()):
                if worker.thread is not None:
                    worker.thread.join(max(0.1, min(10, deadline - time.time())))
            self.comment_tracker.join(timeout=max(0.1, deadline - time.time()))
            for serial, prof in self.device_profiles.items():
                if prof.pages_data:  # never overwrite a saved session with an empty profile
                    self.session_manager.save_device(serial, prof)
            self.comment_queue.save()
            while self.notifier.pending() and time.time() < deadline and self.notifier.flush_one():
                pass
            logger.info("👋 Đã dừng mọi luồng và lưu dữ liệu; đóng tool.")
        except Exception as e:
            logger.error(f"Lỗi khi đóng tool: {e}")
        finally:
            if before_exit is not None:
                try:
                    before_exit()
                except Exception as e:
                    logger.error(f"Lỗi trước khi thoát: {e}")
            self.dispatch_ui(self.destroy)

    # ---------- disk housekeeping ----------
    def housekeeping(self, include_browser: bool) -> CleanReport:
        """Delete what only takes disk space; browser caches only while no scan browser is open."""
        report = CleanReport()
        with self._housekeeping_lock:
            cfg = self.config
            delete_older_than(cfg.base_dir / "logs" / "screenshots", cfg.screenshot_keep_days, report)
            delete_older_than(
                cfg.comment_queue_path.parent / "comment_archive", cfg.archive_keep_days, report, "*.jsonl"
            )
            if cfg.running_dir and os.path.isdir(cfg.running_dir):
                keep = self.comment_queue.protected_archives()
                clean_stuck_payloads(cfg.running_dir, report, keep=keep)
            if include_browser and not self.comment_tracker.is_alive():
                for profile in self.fb_profiles.list():
                    clean_browser_cache(
                        self.fb_profiles.dir_of(profile), report, min_total_mb=cfg.browser_cache_max_mb
                    )
        if report.removed:
            logger.info(f"🧹 Dọn dẹp ổ đĩa: đã xóa {report.summary()}.")
        return report

    def _startup_housekeeping(self, start_tracker: bool = True):
        def work():
            try:
                self.housekeeping(include_browser=True)
            except Exception as e:
                logger.error(f"Lỗi dọn dẹp ổ đĩa: {e}")
            if start_tracker:
                self.dispatch_ui(lambda: self.start_tracker_checked(interactive=False))

        threading.Thread(target=work, name="Housekeeping", daemon=True).start()

    def _periodic_housekeeping(self):
        def work():
            try:
                self.housekeeping(include_browser=False)
            except Exception as e:
                logger.error(f"Lỗi dọn dẹp ổ đĩa: {e}")

        threading.Thread(target=work, name="Housekeeping", daemon=True).start()
        self.after(HOUSEKEEPING_EVERY_MS, self._periodic_housekeeping)

    def _start_queue_poller(self):
        self._process_ui_queue()

    def _process_ui_queue(self):
        try:
            while not getattr(self, "_destroyed", False):
                callback = self.ui_queue.get_nowait()
                try:
                    callback()
                except Exception as e:
                    logger.error(f"Lỗi thực thi giao diện: {e}")
        except queue.Empty:
            pass
        finally:
            if not getattr(self, "_destroyed", False):
                self.after(40, self._process_ui_queue)

    def destroy(self):
        self._destroyed = True
        logger.remove_listener(self._on_log_received)
        super().destroy()

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

        self._create_update_button(btn_frame)

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
        self.tab_comment = self.tabview.add("💬 5. Comment Theo Lượt Xem")

        self._build_tab_farm()
        self._build_tab_progress()
        self._build_tab_template()
        self._build_tab_settings()
        self._build_tab_comment()

    def _build_tab_farm(self):
        tab = self.tab_farm

        # Top quick actions
        top_bar = ctk.CTkFrame(tab, corner_radius=6, fg_color="#1e222d")
        top_bar.pack(fill="x", padx=8, pady=(4, 6))

        ctk.CTkButton(
            top_bar,
            text="⚡ Tự Động Gán Nhanh fb1..fbN Cho Cả Dàn",
            width=260,
            height=30,
            fg_color="#475569",
            hover_color="#334155",
            command=self._auto_assign_all_profiles,
        ).pack(side="left", padx=10, pady=6)

        ctk.CTkButton(
            top_bar,
            text="🛠️ Khởi Tạo Scripts Cho TẤT CẢ Máy",
            width=240,
            height=30,
            font=ctk.CTkFont(weight="bold"),
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self._init_scripts_for_all_devices,
        ).pack(side="left", padx=(0, 10), pady=6)

        btn_open_running = ctk.CTkButton(
            top_bar,
            text="📂 Mở Thư Mục Đệm",
            width=140,
            height=30,
            fg_color="#374151",
            command=self._open_running_dir,
        )
        btn_open_running.pack(side="right", padx=10, pady=6)

        # Split Master-Detail layout
        split_frame = ctk.CTkFrame(tab, fg_color="transparent")
        split_frame.pack(fill="both", expand=True, padx=8, pady=4)

        # LEFT: Device List (Treeview / Table)
        left_box = ctk.CTkFrame(split_frame, corner_radius=6, fg_color="#181b24", width=460)
        left_box.pack(side="left", fill="both", expand=True, padx=(0, 6))

        ctk.CTkLabel(
            left_box,
            text="📋 DANH SÁCH THIẾT BỊ KẾT NỐI (ADB)",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
        ).pack(anchor="w", padx=10, pady=(6, 4))

        # Using ttk.Treeview with Dark Styling
        tree_frame = ctk.CTkFrame(left_box, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=6, pady=4)

        columns = ("serial", "model", "via", "pages", "status")
        self.device_tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="browse", height=12
        )
        self.device_tree.heading("serial", text="Serial ID")
        self.device_tree.heading("model", text="Tên Máy / Model")
        self.device_tree.heading("via", text="Via Tag")
        self.device_tree.heading("pages", text="Tiến Độ")
        self.device_tree.heading("status", text="Trạng Thái")

        self.device_tree.column("serial", width=110, anchor="w")
        self.device_tree.column("model", width=120, anchor="w")
        self.device_tree.column("via", width=65, anchor="center")
        self.device_tree.column("pages", width=75, anchor="center")
        self.device_tree.column("status", width=95, anchor="center")

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.device_tree.yview)
        self.device_tree.configure(yscrollcommand=scrollbar.set)
        self.device_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.device_tree.bind("<<TreeviewSelect>>", self._on_device_tree_select)

        # RIGHT: Detail Profile Configuration
        self.right_box = ctk.CTkFrame(
            split_frame, corner_radius=6, fg_color="#1e222d", width=520
        )
        self.right_box.pack(side="right", fill="both", expand=True, padx=(6, 0))

        self.lbl_detail_title = ctk.CTkLabel(
            self.right_box,
            text="⚙️ CẤU HÌNH THIẾT BỊ: (Chọn máy bên trái)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
        )
        self.lbl_detail_title.pack(anchor="w", padx=12, pady=(8, 4))

        # Checkbox Enabled
        self.var_profile_enabled = ctk.BooleanVar(value=True)
        self.chk_profile_enabled = ctk.CTkCheckBox(
            self.right_box,
            text="Kích hoạt máy này trong dàn chạy tự động",
            variable=self.var_profile_enabled,
            command=self._on_detail_field_changed,
        )
        self.chk_profile_enabled.pack(anchor="w", padx=12, pady=4)

        # Via Index {m}
        r_via = ctk.CTkFrame(self.right_box, fg_color="transparent")
        r_via.pack(fill="x", padx=12, pady=3)
        ctk.CTkLabel(r_via, text="Chỉ số Via {m} (fb1, fb2...):", width=170, anchor="w").pack(
            side="left"
        )
        self.entry_profile_m = ctk.CTkEntry(r_via, width=70)
        self.entry_profile_m.pack(side="left")
        self.entry_profile_m.bind("<KeyRelease>", lambda e: self._on_detail_field_changed())

        # Source dir for this device
        r_src = ctk.CTkFrame(self.right_box, fg_color="transparent")
        r_src.pack(fill="x", padx=12, pady=3)
        ctk.CTkLabel(r_src, text="Kho bài viết gốc (Source):", width=170, anchor="w").pack(
            side="left"
        )
        self.entry_profile_src = ctk.CTkEntry(r_src)
        self.entry_profile_src.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(
            r_src, text="Chọn...", width=65, command=self._browse_profile_source_dir
        ).pack(side="left")

        # Page mappings
        p_top = ctk.CTkFrame(self.right_box, fg_color="transparent")
        p_top.pack(fill="x", padx=12, pady=(6, 2))
        ctk.CTkLabel(
            p_top, text="Danh sách Fanpage của máy này:", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(side="left")
        ctk.CTkButton(
            p_top,
            text="💾 Lưu Tên + Link",
            width=120,
            height=22,
            fg_color="#059669",
            command=self._save_page_links_selected,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            p_top,
            text="Quét Kho & Điền Mẫu",
            width=130,
            height=22,
            fg_color="#475569",
            command=self._scan_and_fill_profile_pages,
        ).pack(side="right")

        p_cols = ctk.CTkFrame(self.right_box, fg_color="transparent")
        p_cols.pack(fill="both", expand=True, padx=12, pady=3)
        p_cols.grid_columnconfigure(0, weight=2)
        p_cols.grid_columnconfigure(1, weight=3)
        p_cols.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(p_cols, text="Tên Fanpage (mỗi dòng 1 page)", anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(p_cols, text="Link Fanpage (cùng dòng với tên)", anchor="w").grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        self.text_profile_pages = ctk.CTkTextbox(
            p_cols, height=85, wrap="none", font=ctk.CTkFont(family="Consolas", size=11)
        )
        self.text_profile_pages.grid(row=1, column=0, sticky="nsew")
        self.text_profile_pages.bind("<KeyRelease>", lambda e: self._on_detail_field_changed())
        self.text_profile_links = ctk.CTkTextbox(
            p_cols, height=85, wrap="none", font=ctk.CTkFont(family="Consolas", size=11)
        )
        self.text_profile_links.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        self.text_profile_links.bind("<KeyRelease>", lambda e: self._on_detail_field_changed())

        # Device action buttons
        d_actions = ctk.CTkFrame(self.right_box, fg_color="transparent")
        d_actions.pack(fill="x", padx=12, pady=(6, 10))

        self.btn_init_one = ctk.CTkButton(
            d_actions,
            text="🛠️ Khởi Tạo Cho Máy Này",
            width=170,
            fg_color="#0284c7",
            command=self._init_scripts_for_selected_device,
        )
        self.btn_init_one.pack(side="left", padx=(0, 6))

        self.btn_start_one = ctk.CTkButton(
            d_actions,
            text="▶️ Chạy Riêng Máy Này",
            width=150,
            fg_color="#10b981",
            command=self._start_selected_device,
        )
        self.btn_start_one.pack(side="left", padx=(0, 6))

        self.btn_stop_one = ctk.CTkButton(
            d_actions,
            text="⏹️ Dừng",
            width=70,
            fg_color="#ef4444",
            command=self._stop_selected_device,
        )
        self.btn_stop_one.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            d_actions,
            text="🚩 Nạp Cờ ĐT",
            width=90,
            fg_color="#d97706",
            command=self._resync_flags_selected_device,
        ).pack(side="left")

    def _build_tab_progress(self):
        tab = self.tab_progress

        top_ctrl = ctk.CTkFrame(tab, corner_radius=6, fg_color="#1e222d")
        top_ctrl.pack(fill="x", padx=10, pady=6)

        ctk.CTkLabel(
            top_ctrl,
            text="📊 MA TRẬN TIẾN ĐỘ THỜI GIAN THỰC (LIVE FARM PROGRESS)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            top_ctrl,
            text="Làm Mới Hiển Thị",
            width=120,
            height=28,
            fg_color="#475569",
            command=self._update_progress_display,
        ).pack(side="right", padx=12, pady=6)
        ctk.CTkButton(
            top_ctrl,
            text="✅ Bật lại page",
            width=120,
            height=28,
            fg_color="#059669",
            command=lambda: self._toggle_selected_pages(off=False),
        ).pack(side="right", padx=(0, 6), pady=6)
        ctk.CTkButton(
            top_ctrl,
            text="⛔ Tắt page",
            width=110,
            height=28,
            fg_color="#b91c1c",
            command=lambda: self._toggle_selected_pages(off=True),
        ).pack(side="right", padx=(0, 6), pady=6)

        table_box = ctk.CTkFrame(tab, corner_radius=6, fg_color="#181b24")
        table_box.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # Modern multi-column Treeview
        tree_frame = ctk.CTkFrame(table_box, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=8)

        columns = ("serial", "via", "page_id", "fanpage_name", "progress", "status", "updated_at")
        self.progress_tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended"
        )
        self.progress_tree.heading("serial", text="Thiết Bị (Serial)")
        self.progress_tree.heading("via", text="Via")
        self.progress_tree.heading("page_id", text="Mã Page")
        self.progress_tree.heading("fanpage_name", text="Tên Fanpage")
        self.progress_tree.heading("progress", text="Tiến Độ")
        self.progress_tree.heading("status", text="Trạng Thái")
        self.progress_tree.heading("updated_at", text="Cập Nhật")

        self.progress_tree.column("serial", width=120, anchor="w")
        self.progress_tree.column("via", width=65, anchor="center")
        self.progress_tree.column("page_id", width=110, anchor="center")
        self.progress_tree.column("fanpage_name", width=220, anchor="w")
        self.progress_tree.column("progress", width=85, anchor="center")
        self.progress_tree.column("status", width=200, anchor="w")
        self.progress_tree.column("updated_at", width=95, anchor="center")

        v_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.progress_tree.yview)
        h_scroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.progress_tree.xview)
        self.progress_tree.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)

        self.progress_tree.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")

        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

    def _build_tab_template(self):
        tab = self.tab_template

        note_box = ctk.CTkFrame(tab, corner_radius=6, fg_color="#181b24")
        note_box.pack(fill="x", padx=10, pady=(6, 2))
        ctk.CTkLabel(
            note_box,
            text=(
                "💡 TÙY BIẾN KỊCH BẢN ĐỘNG:\n"
                "• Mẫu kịch bản dùng chung cho toàn bộ dàn máy: template_script.txt & template_config.ini.\n"
                "• Biến thay thế tự động theo từng máy/via: {ID} (vd page1fb1, page1fb2), {PAGE_NAME}, {VIDEO_DIR}, {CAPTION_PATH}, {COMMENT_PATH}, {LINK_PATH}.\n"
                "• Kịch bản comment (template_cmt.txt) được tạo riêng thành SCRIPTS\\cmt{ID}.txt; tool chỉ đẩy link + comment khi bài đủ lượt xem (tab 5)."
            ),
            font=ctk.CTkFont(size=12),
            text_color="#38bdf8",
            justify="left",
        ).pack(anchor="w", padx=12, pady=6)

        sub_tab = ctk.CTkTabview(tab)
        sub_tab.pack(fill="both", expand=True, padx=10, pady=4)

        t_txt = sub_tab.add("Kịch Bản .TXT (template_script.txt)")
        t_ini = sub_tab.add("Cấu Hình .INI (template_config.ini)")
        t_cmt = sub_tab.add("Kịch Bản Comment (template_cmt.txt)")

        self.tpl_txt_edit = ctk.CTkTextbox(t_txt, font=ctk.CTkFont(family="Consolas", size=12))
        self.tpl_txt_edit.pack(fill="both", expand=True, padx=6, pady=6)

        self.tpl_ini_edit = ctk.CTkTextbox(t_ini, font=ctk.CTkFont(family="Consolas", size=12))
        self.tpl_ini_edit.pack(fill="both", expand=True, padx=6, pady=6)

        self.tpl_cmt_edit = ctk.CTkTextbox(t_cmt, font=ctk.CTkFont(family="Consolas", size=12))
        self.tpl_cmt_edit.pack(fill="both", expand=True, padx=6, pady=6)

        self._load_templates_to_ui()

        r = ctk.CTkFrame(tab, fg_color="transparent")
        r.pack(fill="x", padx=10, pady=(2, 6))

        ctk.CTkButton(
            r,
            text="💾 Lưu Mẫu Ra File .txt / .ini",
            width=210,
            fg_color="#0284c7",
            hover_color="#0369a1",
            command=self._save_templates_from_ui,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            r,
            text="📂 Tải Lại Từ File Mẫu",
            width=160,
            fg_color="#475569",
            hover_color="#334155",
            command=self._load_templates_to_ui,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            r,
            text="🔄 Áp Dụng Mẫu Cho Script Đang Dùng",
            width=250,
            fg_color="#059669",
            hover_color="#047857",
            command=self._apply_templates_clicked,
        ).pack(side="left", padx=(0, 10))

    def _build_tab_settings(self):
        tab = self.tab_settings

        # Global Paths
        grp_paths = ctk.CTkFrame(tab, corner_radius=6, fg_color="#1e222d")
        grp_paths.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(
            grp_paths,
            text="📁 ĐƯỜNG DẪN HỆ THỐNG DÙNG CHUNG",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
        ).pack(anchor="w", padx=10, pady=(6, 4))

        # AKPhone dir
        r1 = ctk.CTkFrame(grp_paths, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(r1, text="Thư mục AKPhone(S):", width=170, anchor="w").pack(side="left")
        self.entry_akphone_dir = ctk.CTkEntry(r1)
        self.entry_akphone_dir.pack(side="left", fill="x", expand=True, padx=6)
        self.entry_akphone_dir.insert(0, self.config.akphone_dir)
        ctk.CTkButton(r1, text="Chọn...", width=70, command=self._browse_akphone_dir).pack(
            side="left"
        )

        r1b = ctk.CTkFrame(grp_paths, fg_color="transparent")
        r1b.pack(fill="x", padx=10, pady=3)
        self.var_write_offers = ctk.BooleanVar(value=self.config.write_offers)
        ctk.CTkCheckBox(
            r1b,
            text="Tạo file cấu hình OFFERS\\{ID}.ini (chỉ AKPhone bản cũ cần; AKPhoneS không dùng)",
            variable=self.var_write_offers,
        ).pack(side="left", padx=(170, 0))

        # ADB path
        r2 = ctk.CTkFrame(grp_paths, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(r2, text="Đường dẫn adb.exe:", width=170, anchor="w").pack(side="left")
        self.entry_adb_path = ctk.CTkEntry(r2)
        self.entry_adb_path.pack(side="left", fill="x", expand=True, padx=6)
        self.entry_adb_path.insert(0, self.config.adb_path)
        ctk.CTkButton(
            r2,
            text="Kiểm Tra ADB",
            width=100,
            fg_color="#059669",
            command=self._test_adb_button,
        ).pack(side="left")

        # Running dir
        r3 = ctk.CTkFrame(grp_paths, fg_color="transparent")
        r3.pack(fill="x", padx=10, pady=(3, 8))
        ctk.CTkLabel(r3, text="Thư mục đệm (Running Dir):", width=170, anchor="w").pack(side="left")
        self.entry_running_dir = ctk.CTkEntry(r3)
        self.entry_running_dir.pack(side="left", fill="x", expand=True, padx=6)
        self.entry_running_dir.insert(0, self.config.running_dir)
        ctk.CTkButton(r3, text="Chọn...", width=70, command=self._browse_running_dir).pack(
            side="left"
        )

        # Watchdog & Storage
        grp_wd = ctk.CTkFrame(tab, corner_radius=6, fg_color="#1e222d")
        grp_wd.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(
            grp_wd,
            text="🛡️ TỰ ĐỘNG PHỤC HỒI & BẢO TRÌ ĐIỆN THOẠI (WATCHDOG)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
        ).pack(anchor="w", padx=10, pady=(6, 4))

        r_wd1 = ctk.CTkFrame(grp_wd, fg_color="transparent")
        r_wd1.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(r_wd1, text="Watchdog Timeout (giây):", width=220, anchor="w").pack(side="left")
        self.entry_watchdog_timeout = ctk.CTkEntry(r_wd1, width=80)
        self.entry_watchdog_timeout.pack(side="left")
        self.entry_watchdog_timeout.insert(0, str(self.config.watchdog_timeout))

        r_wd2 = ctk.CTkFrame(grp_wd, fg_color="transparent")
        r_wd2.pack(fill="x", padx=10, pady=3)
        self.var_screencap = ctk.BooleanVar(value=self.config.auto_screencap)
        ctk.CTkCheckBox(
            r_wd2,
            text="Tự động chụp ảnh màn hình điện thoại khi gặp lỗi hoặc treo",
            variable=self.var_screencap,
        ).pack(side="left")

        r_wd3 = ctk.CTkFrame(grp_wd, fg_color="transparent")
        r_wd3.pack(fill="x", padx=10, pady=(3, 8))
        self.var_clean_storage = ctk.BooleanVar(value=self.config.auto_clean_storage)
        ctk.CTkCheckBox(
            r_wd3,
            text="Tự động dọn dẹp rác, thumbnail và cache Facebook trên các máy sau mỗi vòng",
            variable=self.var_clean_storage,
        ).pack(side="left")

        # Payload Store Retention Policy
        grp_retention = ctk.CTkFrame(tab, corner_radius=6, fg_color="#1e222d")
        grp_retention.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(
            grp_retention,
            text="🧹 QUẢN LÝ DUNG LƯỢNG Ổ ĐĨA & BẢN LƯU (.PAYLOAD-STORE)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
        ).pack(anchor="w", padx=10, pady=(6, 4))

        r_ret1 = ctk.CTkFrame(grp_retention, fg_color="transparent")
        r_ret1.pack(fill="x", padx=10, pady=3)
        self.var_clean_payloads = ctk.BooleanVar(value=self.config.auto_clean_payloads)
        ctk.CTkCheckBox(
            r_ret1,
            text="Tự động dọn dẹp các thư mục lưu trữ payload (.payload-store) đã hoàn tất sau mỗi vòng",
            variable=self.var_clean_payloads,
        ).pack(side="left")

        r_ret2 = ctk.CTkFrame(grp_retention, fg_color="transparent")
        r_ret2.pack(fill="x", padx=10, pady=(3, 8))
        ctk.CTkLabel(
            r_ret2, text="Số ngày lưu vết (0 = xóa ngay khi xong vòng):", width=280, anchor="w"
        ).pack(side="left")
        self.entry_retention_days = ctk.CTkEntry(r_ret2, width=60)
        self.entry_retention_days.pack(side="left")
        self.entry_retention_days.insert(0, str(self.config.payload_retention_days))

        ctk.CTkButton(
            r_ret2,
            text="🧹 Dọn Dẹp Ổ Đĩa Ngay",
            width=210,
            fg_color="#d97706",
            hover_color="#b45309",
            command=self._clean_payload_store_now,
        ).pack(side="left", padx=(20, 0))

        # Telegram
        grp_tg = ctk.CTkFrame(tab, corner_radius=6, fg_color="#1e222d")
        grp_tg.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(
            grp_tg,
            text="📲 THÔNG BÁO TỪ XA QUA TELEGRAM BOT",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#38bdf8",
        ).pack(anchor="w", padx=10, pady=(6, 4))

        r_tg1 = ctk.CTkFrame(grp_tg, fg_color="transparent")
        r_tg1.pack(fill="x", padx=10, pady=3)
        self.var_telegram_enabled = ctk.BooleanVar(value=self.config.telegram.enabled)
        ctk.CTkCheckBox(
            r_tg1,
            text="Bật gửi thông báo tiến độ & ảnh lỗi về Telegram",
            variable=self.var_telegram_enabled,
        ).pack(side="left")

        r_tg2 = ctk.CTkFrame(grp_tg, fg_color="transparent")
        r_tg2.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(r_tg2, text="Telegram Bot Token:", width=180, anchor="w").pack(side="left")
        self.entry_tg_token = ctk.CTkEntry(r_tg2)
        self.entry_tg_token.pack(side="left", fill="x", expand=True, padx=6)
        self.entry_tg_token.insert(0, self.config.telegram.bot_token)

        r_tg3 = ctk.CTkFrame(grp_tg, fg_color="transparent")
        r_tg3.pack(fill="x", padx=10, pady=(3, 8))
        ctk.CTkLabel(r_tg3, text="Telegram Chat ID:", width=180, anchor="w").pack(side="left")
        self.entry_tg_chat_id = ctk.CTkEntry(r_tg3)
        self.entry_tg_chat_id.pack(side="left", fill="x", expand=True, padx=6)
        self.entry_tg_chat_id.insert(0, self.config.telegram.chat_id)
        ctk.CTkButton(
            r_tg3, text="Gửi Thử", width=100, fg_color="#0284c7", command=self._test_telegram
        ).pack(side="left")

        # Save Settings Button
        btn_save = ctk.CTkButton(
            tab,
            text="💾 LƯU CẤU HÌNH HỆ THỐNG",
            height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#059669",
            command=self._save_system_settings,
        )
        btn_save.pack(fill="x", padx=10, pady=10)

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
                saved_cfg = self.config.get_profile(d.serial, default_fb_idx=str(idx))
                prof = DeviceProfile(
                    serial=d.serial,
                    fb_index=saved_cfg.get("fb_index", str(idx)),
                    source_dir=saved_cfg.get("source_dir", self.config.source_dir),
                    running_dir=self.config.running_dir,
                    page_names=saved_cfg.get("page_names", self.config.page_names),
                    page_links=saved_cfg.get("page_links", ""),
                    enabled=saved_cfg.get("enabled", True),
                    device_info=d,
                    disabled_pages=list(saved_cfg.get("disabled_pages") or []),
                )
                # setdefault: a session restored meanwhile keeps its profile
                self.device_profiles.setdefault(d.serial, prof).device_info = d
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

            # Auto select first item if none selected
            if devices and (not self.selected_serial or self.selected_serial not in [d.serial for d in devices]):
                self._select_device_serial(devices[0].serial)

        self.dispatch_ui(update_ui)

    def _select_device_serial(self, serial: str):
        if getattr(self, "_is_updating_selection", False):
            return
        self._is_updating_selection = True
        try:
            self.selected_serial = serial
            current_sel = self.device_tree.selection()
            if current_sel != (serial,):
                try:
                    self.device_tree.selection_set(serial)
                    self.device_tree.focus(serial)
                except Exception:
                    pass

            prof = self.device_profiles.get(serial)
            if not prof:
                return

            info_text = f"⚙️ CẤU HÌNH THIẾT BỊ: [{serial}] {prof.device_info.brand if prof.device_info else ''} {prof.device_info.model if prof.device_info else ''}"
            self.lbl_detail_title.configure(text=info_text)

            self.var_profile_enabled.set(prof.enabled)
            self.entry_profile_m.delete(0, "end")
            self.entry_profile_m.insert(0, prof.fb_index)

            self.entry_profile_src.delete(0, "end")
            self.entry_profile_src.insert(0, prof.source_dir)

            self.text_profile_pages.delete("1.0", "end")
            self.text_profile_pages.insert("1.0", prof.page_names)
            self.text_profile_links.delete("1.0", "end")
            self.text_profile_links.insert("1.0", prof.page_links)
        finally:
            self._is_updating_selection = False

    def _on_device_tree_select(self, event):
        if getattr(self, "_is_updating_selection", False):
            return
        selected = self.device_tree.selection()
        if selected and selected[0] != self.selected_serial:
            self._select_device_serial(selected[0])

    def _on_detail_field_changed(self):
        if getattr(self, "_is_updating_selection", False) or not self.selected_serial:
            return
        prof = self.device_profiles.get(self.selected_serial)
        if not prof:
            return

        prof.enabled = bool(self.var_profile_enabled.get())
        prof.fb_index = self.entry_profile_m.get().strip() or "1"
        prof.source_dir = self.entry_profile_src.get().strip()
        prof.page_names = self.text_profile_pages.get("1.0", "end-1c").strip()
        # keep empty lines: a link belongs to the name on the same line number
        prof.page_links = self.text_profile_links.get("1.0", "end-1c").rstrip()

        # Update config profile
        self.config.set_profile(
            self.selected_serial,
            {
                "fb_index": prof.fb_index,
                "source_dir": prof.source_dir,
                "page_names": prof.page_names,
                "page_links": prof.page_links,
                "enabled": prof.enabled,
            },
        )
        # Update Treeview row
        try:
            self.device_tree.item(
                self.selected_serial,
                values=(
                    self.selected_serial,
                    f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                    prof.via_tag,
                    prof.progress_summary,
                    prof.status,
                ),
            )
        except Exception:
            pass

    # ---------- pages turned off by hand (Tab 2) ----------
    def _is_page_off(self, serial: str, page_id: str) -> bool:
        prof = self.device_profiles.get(serial)
        return prof is not None and prof.page_off(page_id)

    def set_pages_off(self, pages: List[Tuple[str, str]], off: bool) -> List[Tuple[str, str]]:
        """Turn (serial, page_id) pages off / back on, saved in each phone's config.

        Returns the pages that changed. The list is replaced, never edited in place:
        the feeder and comment threads read it at any time.
        """
        changed = []
        for serial, pid in pages:
            prof = self.device_profiles.get(serial)
            if prof is None or prof.page_off(pid) == off:
                continue
            others = [p for p in prof.disabled_pages if p != pid]
            prof.disabled_pages = others + [pid] if off else others
            self.config.set_profile(serial, {"disabled_pages": list(prof.disabled_pages)})
            changed.append((serial, pid))
            logger.info(
                f"{'⛔' if off else '✅'} [MÁY {serial} | {prof.via_tag}] [{pid}] "
                + ("đã TẮT page: không nạp bài, không cắm cờ, ngừng quét comment."
                   if off else "đã BẬT LẠI page: chạy lại từ vòng sau.")
            )
        return changed

    def _selected_progress_pages(self) -> List[Tuple[str, str]]:
        picked = set(self.progress_tree.selection())
        return [
            (serial, pid)
            for serial, prof in self.device_profiles.items()
            for pid in prof.pages_data
            if f"{serial}_{pid}" in picked
        ]

    def _toggle_selected_pages(self, off: bool):
        title = "Tắt Page" if off else "Bật Lại Page"
        todo = [(s, p) for s, p in self._selected_progress_pages() if self._is_page_off(s, p) != off]
        if not todo:
            messagebox.showinfo(
                title,
                "Chọn page trong bảng (giữ Ctrl để chọn nhiều)."
                if not self._selected_progress_pages()
                else f"Các page đã chọn đều đã {'tắt' if off else 'bật'} sẵn.",
            )
            return
        lines = [
            f"• {s} · {p} · {self.device_profiles[s].pages_data[p].fanpage_name}" for s, p in todo
        ]
        text = f"{title} {len(todo)} page:\n" + "\n".join(lines[:15]) + ("\n..." if len(lines) > 15 else "")
        if off:
            text += (
                "\n\nPage tắt: không nạp bài mới, không cắm cờ Done, Khởi Tạo bỏ qua; luồng comment "
                "ngừng quét page và không đẩy comment. Bài đang theo dõi được giữ nguyên, bật lại thì "
                "chạy tiếp.\nNên bỏ page...txt và cmtpage...txt của page này khỏi vòng lặp AKPhoneS."
            )
            in_round = [
                p for s, p in todo
                if p in getattr(self.feeder_manager.workers.get(s), "current_round_flags", set())
            ]
            if in_round:
                text += (
                    f"\n\n⚠ {', '.join(in_round)} đã có cờ Done trong vòng đang chạy: vòng này vẫn chạy "
                    "nốt, từ vòng sau mới bỏ qua."
                )
        else:
            text += (
                "\n\nTừ vòng sau page chạy lại (chưa có bài nạp sẵn thì tool nạp bài kế tiếp trước); "
                "luồng comment quét lại page. Máy đã dừng hoặc đã báo hoàn tất thì bấm Chạy lại máy đó."
            )
        if not messagebox.askyesno(title, text):
            return
        self.set_pages_off(todo, off)
        self._update_progress_display()

    def _page_identity(self, prof, page: PageInfo):
        """(Fanpage name, Fanpage link) currently written for a page in the device profile."""
        names = parse_page_names_mapping(prof.page_names or self.config.page_names)
        links = parse_page_links_mapping(prof.page_names or self.config.page_names, prof.page_links)
        m_num = re.search(r"\d+", page.folder_name)
        n_num = m_num.group(0) if m_num else page.folder_name
        name = names.get(page.folder_name) or names.get(f"page-{n_num}") or ""
        link = links.get(page.folder_name) or links.get(f"page-{n_num}") or ""
        return name, link

    def _apply_page_links(self, serial: str) -> List[str]:
        """Push the profile's Fanpage links to its pages and to their running comment jobs.

        Same name as now -> same page: the link is filled in / corrected, also for posts
        already queued. New name -> new page: it takes effect after 'Khởi Tạo' (new
        scripts), and the old posts keep tracking the old page with the old link.
        """
        prof = self.device_profiles.get(serial)
        report: List[str] = []
        if not prof:
            return report
        for pid, page in sorted(prof.pages_data.items()):
            name, link = self._page_identity(prof, page)
            if name and name != page.fanpage_name:
                report.append(
                    f"• {pid}: đổi page '{page.fanpage_name}' → '{name}' — bấm 'Khởi Tạo' để áp dụng; "
                    "bài cũ vẫn theo dõi page cũ."
                )
                continue
            page.page_url = link
            changed = self.comment_queue.set_page_url(serial, pid, page.fanpage_name, link)
            state = link or "⚠ CHƯA CÓ LINK"
            extra = f" (cập nhật {changed} bài đang theo dõi)" if changed else ""
            report.append(f"• {pid} · {page.fanpage_name}: {state}{extra}")
        return report

    def _save_page_links_selected(self):
        if not self.selected_serial:
            messagebox.showwarning("Cảnh báo", "Vui lòng chọn 1 thiết bị từ bảng bên trái!")
            return
        self._on_detail_field_changed()
        report = self._apply_page_links(self.selected_serial)
        self.session_manager.save_farm_session(self.device_profiles)
        if not report:
            messagebox.showinfo(
                "Đã Lưu",
                "Đã lưu tên + link Fanpage. Máy này chưa khởi tạo page nào: link sẽ được dùng khi "
                "bấm 'Khởi Tạo'.",
            )
            return
        logger.info(f"🔗 Đã lưu link Fanpage cho máy {self.selected_serial}:\n" + "\n".join(report))
        messagebox.showinfo("Đã Lưu Tên + Link Fanpage", "\n".join(report))

    def _browse_profile_source_dir(self):
        d = filedialog.askdirectory(initialdir=self.entry_profile_src.get())
        if d:
            self.entry_profile_src.delete(0, "end")
            self.entry_profile_src.insert(0, d)
            self._on_detail_field_changed()

    def _scan_and_fill_profile_pages(self):
        src = self.entry_profile_src.get().strip()
        scanned = scan_source_directory(src, self.config.running_dir)
        if not scanned:
            messagebox.showwarning(
                "Thông Báo", f"Không tìm thấy video nào trong kho nguồn của máy này:\n{src}"
            )
            return
        lines = [f"{p_name}: Fanpage {p_name}" for p_name in scanned.keys()]
        self.text_profile_pages.delete("1.0", "end")
        self.text_profile_pages.insert("1.0", "\n".join(lines))
        self._on_detail_field_changed()
        logger.info(
            f"Đã quét thấy {len(scanned)} page cho máy {self.selected_serial}: {', '.join(scanned.keys())}"
        )

    def _auto_assign_all_profiles(self):
        """Sequentially assign fb1, fb2... fbN across all detected phones."""
        if not self.connected_devices:
            messagebox.showwarning("Thông Báo", "Chưa có thiết bị nào kết nối qua ADB!")
            return

        for idx, d in enumerate(self.connected_devices, start=1):
            prof = self.device_profiles[d.serial]
            prof.fb_index = str(idx)
            self.config.set_profile(
                d.serial,
                {
                    "fb_index": prof.fb_index,
                    "source_dir": prof.source_dir,
                    "page_names": prof.page_names,
                    "page_links": prof.page_links,
                    "enabled": prof.enabled,
                },
            )
            try:
                self.device_tree.item(
                    d.serial,
                    values=(
                        d.serial,
                        f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                        prof.via_tag,
                        prof.progress_summary,
                        prof.status,
                    ),
                )
            except Exception:
                pass

        if self.selected_serial:
            self._select_device_serial(self.selected_serial)

        messagebox.showinfo(
            "Thành Công",
            f"Đã tự động gán thứ tự fb1..fb{len(self.connected_devices)} cho toàn bộ {len(self.connected_devices)} máy!",
        )

    def _init_scripts_for_device(self, serial: str) -> bool:
        """Compile and seed 1 Page = 1 Script files for a single Phone/Via."""
        prof = self.device_profiles.get(serial)
        if not prof:
            return False

        src_dir = prof.source_dir or self.config.source_dir
        ak_dir = self.config.akphone_dir
        running_base = self.config.running_dir
        if not ak_dir or not os.path.isdir(ak_dir):
            logger.error(f"Thư mục AKPhone chưa được cấu hình hoặc không tồn tại: {ak_dir}")
            messagebox.showerror(
                "Chưa Cài Đặt AKPhone",
                f"Thư mục AKPhone không hợp lệ hoặc chưa được chọn: '{ak_dir}'\n"
                "Vui lòng vào Tab '4. Cài Đặt Hệ Thống' để cài đặt và chọn thư mục AKPhone!",
            )
            return False

        if not running_base:
            logger.error("Thư mục đệm (Running Dir) chưa được cấu hình!")
            messagebox.showerror(
                "Chưa Chọn Thư Mục Đệm",
                "Thư mục đệm (Running Dir) chưa được cấu hình!\n"
                "Vui lòng vào Tab '4. Cài Đặt Hệ Thống' để chọn thư mục đệm.",
            )
            return False

        if not src_dir or not os.path.exists(src_dir):
            logger.error(f"Kho nguồn không tồn tại cho máy {serial}: {src_dir}")
            messagebox.showerror(
                "Kho Nguồn Không Hợp Lệ",
                f"Kho nguồn không tồn tại cho máy {serial}:\n'{src_dir}'\n"
                "Vui lòng chọn thư mục kho bài nguồn hợp lệ!",
            )
            return False

        scripts_dest, offers_dest = resolve_akphone_layout(ak_dir)
        scripts_files_dest = scripts_dest / "FILES"
        write_offers = self.config.write_offers and offers_dest is not None
        if self.config.write_offers and offers_dest is None:
            logger.warning(
                f"Đã tích tạo OFFERS nhưng '{ak_dir}' không có thư mục OFFERS (AKPhoneS); bỏ qua file .ini."
            )

        scripts_dest.mkdir(parents=True, exist_ok=True)
        scripts_files_dest.mkdir(parents=True, exist_ok=True)
        Path(running_base).mkdir(parents=True, exist_ok=True)

        scanned = scan_source_directory(src_dir, running_base)
        if not scanned:
            logger.error(f"Không tìm thấy video nào trong kho {src_dir} của máy {serial}")
            return False

        # Checked before anything is written or cut out of the store
        report = check_store(src_dir, prof.page_names or self.config.page_names, prof.fb_index)
        for warning in report.warnings:
            logger.warning(f"[MÁY {serial} | {prof.via_tag}] Kho bài: {warning}")
        if report.errors:
            for error in report.errors:
                logger.error(f"[MÁY {serial} | {prof.via_tag}] Kho bài: {error}")
            shown = report.errors[:12]
            messagebox.showerror(
                "Kho Bài Chưa Đúng - Chưa Khởi Tạo",
                f"Máy {serial} ({prof.via_tag}) chưa được khởi tạo, chưa có file nào bị chuyển.\n"
                f"Kho: {src_dir}\n\n"
                + "\n".join(f"• {e}" for e in shown)
                + (f"\n... và {len(report.errors) - len(shown)} lỗi khác (xem nhật ký)"
                   if len(report.errors) > len(shown) else "")
                + "\n\nSửa kho / tên page rồi bấm Khởi Tạo lại."
                "\nVideo ghép với caption và comment theo SỐ trong tên file "
                "(vd10...mp4 ↔ cap10...txt ↔ cmt10...txt).",
            )
            return False

        page_names_map = parse_page_names_mapping(prof.page_names or self.config.page_names)
        page_links_map = parse_page_links_mapping(
            prof.page_names or self.config.page_names, prof.page_links
        )
        txt_tpl = self.config.template_txt_path.read_text(encoding="utf-8")
        ini_tpl = self.config.template_ini_path.read_text(encoding="utf-8") if write_offers else ""
        cmt_path = self.config.template_cmt_path
        cmt_tpl = cmt_path.read_text(encoding="utf-8") if cmt_path.is_file() else ""
        post_report, cmt_report = template_reports(txt_tpl, cmt_tpl, self.config.comment.flag_dir)
        template_errors = post_report.errors + (cmt_report.errors if cmt_report else [])
        if template_errors:
            for error in template_errors:
                logger.error(f"[MÁY {serial}] Mẫu script: {error}")
            messagebox.showerror(
                "Mẫu Script Có Lỗi - Chưa Khởi Tạo",
                "Chưa có file nào bị chuyển.\n\n" + "\n".join(f"• {e}" for e in template_errors)
                + "\n\nSửa ở Tab 3 rồi Khởi Tạo lại.",
            )
            return False
        for warning in post_report.warnings + (cmt_report.warnings if cmt_report else []):
            logger.warning(f"[MÁY {serial}] Mẫu script: {warning}")

        # The comment tracker stages re-posts under this lock: holding it from the staged
        # check until every page has its new FED job means no re-post Done flag can land
        # in between (it would be wiped as "old", then read as posted).
        failure = None
        with self.comment_tracker.repost_lock:
            # A re-post already put back (video + Done flag) would be replaced by the new
            # store's post 1 and its flag wiped: wait until AKPhoneS has posted it.
            staged = [
                j for j in self.comment_queue.active_jobs()
                if j["serial"] == serial and j.get("repost_state") == REPOST_STAGED
            ]
            if staged:
                lines = [f"• {j['page_id']} bài {j['post_num']} ({j['video_name']})" for j in staged]
                logger.error(
                    f"[MÁY {serial} | {prof.via_tag}] Chưa khởi tạo: còn {len(staged)} bài đang chờ AKPhoneS đăng lại."
                )
                failure = (
                    "Đang Chờ Đăng Lại - Chưa Khởi Tạo",
                    f"Máy {serial} ({prof.via_tag}) chưa được khởi tạo, chưa có file nào bị chuyển.\n"
                    "Các bài sau đã được nạp lại video + cờ Done để đăng lại:\n"
                    + "\n".join(lines)
                    + "\n\nĐợi vòng lặp AKPhoneS đăng xong (Tab 5 chuyển 'Đã đăng lại') rồi Khởi Tạo lại.",
                )
            # A Done flag left from the previous store (e.g. stopped mid-round) would make
            # AKPhoneS post the new post 1 while no feeder watches, then again on Start.
            elif not self.adb.clear_done_flags(serial):
                logger.error(f"[MÁY {serial} | {prof.via_tag}] Chưa khởi tạo: không xóa được cờ Done cũ trên máy.")
                failure = (
                    "Chưa Xóa Được Cờ Done - Chưa Khởi Tạo",
                    f"Máy {serial} ({prof.via_tag}) chưa được khởi tạo, chưa có file nào bị chuyển.\n"
                    "Không xóa được cờ Done cũ trên điện thoại (máy chưa kết nối ADB?).\n\n"
                    "Cắm lại cáp, bấm 'Quét ADB' rồi Khởi Tạo lại.",
                )
            else:
                prof.pages_data = {}
                # flags just wiped belong to the old store: not "posted while away"
                prof.round_flags, prof.round_flags_seen_at = [], 0.0
                m_val = prof.fb_index or "1"

                try:
                    for folder_name, posts in scanned.items():
                        n_num = page_number(folder_name)
                        page_id = page_id_for(folder_name, m_val)
                        fanpage_name = page_name_for(folder_name, page_names_map)
                        page_url = page_links_map.get(folder_name) or page_links_map.get(f"page-{n_num}") or ""
                        if not page_url:
                            logger.warning(
                                f"[{page_id}] {fanpage_name} chưa có link Fanpage: bài vẫn đăng, nhưng chưa "
                                "theo dõi view/comment được cho tới khi điền link."
                            )

                        page_video_dir = Path(running_base) / page_id
                        page_video_dir.mkdir(parents=True, exist_ok=True)

                        page_caption_filename = f"Caption_{page_id}.txt"
                        ak_caption_path = str(scripts_files_dest / page_caption_filename)

                        final_txt = render_post_script(txt_tpl, page_id, fanpage_name, str(page_video_dir))
                        (scripts_dest / f"{page_id}.txt").write_text(final_txt, encoding="utf-8")

                        if cmt_tpl:
                            # The real comment script is written only when a post reaches the
                            # threshold; until then cmt{ID}.txt does nothing (no double comment).
                            final_cmt = (
                                render_cmt_script(cmt_tpl, page_id, fanpage_name, self.config.comment.flag_dir)
                                if self.comment_queue.has_marked(serial, page_id)
                                else IDLE_CMT_SCRIPT
                            )
                            (scripts_dest / cmt_script_filename(page_id)).write_text(
                                final_cmt, encoding="utf-8"
                            )

                        if write_offers:
                            final_ini = (
                                ini_tpl.replace("{ID}", page_id)
                                .replace("{PAGE_NAME}", fanpage_name)
                                .replace("{VIDEO_DIR}", str(page_video_dir))
                            )
                            (offers_dest / f"{page_id}.ini").write_text(final_ini, encoding="utf-8")

                        page_obj = PageInfo(
                            page_id=page_id,
                            folder_name=folder_name,
                            fanpage_name=fanpage_name,
                            total_posts=len(posts),
                            current_idx=1,
                            posts=posts,
                            video_dir=str(page_video_dir),
                            caption_destinations=[ak_caption_path],
                            last_status="Ready",
                            page_url=page_url,
                        )
                        prof.pages_data[page_id] = page_obj
                        if prof.page_off(page_id):
                            # turned off by hand: post 1 stays in the store; once the page is
                            # turned back on the feeder loads it like any pending feed
                            page_obj.current_idx = 0
                            logger.warning(
                                f"[MÁY {serial} | {prof.via_tag}] [{page_id}] {fanpage_name}: page đã tắt, không nạp bài."
                            )
                            continue

                        # Feed post 1
                        target_post = posts[0]
                        target_dict = {
                            "post_num": target_post.post_num,
                            "video_file": target_post.video_file,
                            "caption_file": target_post.caption_file,
                            "comment_file": target_post.comment_file,
                        }
                        transfer_payload(
                            target_dict,
                            str(page_video_dir),
                            [ak_caption_path],
                            [],  # comment is pushed later, when the post reaches the view threshold
                            fanpage_name,
                            1,
                        )
                        queue_post_comment(self.comment_queue, serial, page_obj, 1, target_dict)
                        if "_payload_archive" in target_dict:
                            target_post.archive_dir = target_dict["_payload_archive"]
                except Exception as e:
                    prof.status = "❌ Lỗi tạo kịch bản"
                    logger.error(f"Lỗi khi khởi tạo kịch bản cho máy {serial}: {e}", exc_info=True)
                    failure = (
                        "Lỗi Khởi Tạo",
                        f"Đã xảy ra lỗi khi tạo kịch bản cho máy {serial}:\n{e}",
                    )

        # Shown once the lock is free: an open dialog must not stall the tracker's flags thread.
        if failure:
            messagebox.showerror(*failure)
            return False
        prof.status = "✅ Sẵn sàng"
        logger.info(
            f"🎉 [MÁY {serial} | {prof.via_tag}] Đã tạo hoàn tất {len(scanned)} kịch bản (1 Page = 1 Script)!"
        )
        return True

    def _warn_if_offers_missing(self, ak_dir: str) -> bool:
        """Tell the user when 'create OFFERS' is ticked but the AKPhone(S) folder has none."""
        if not self.config.write_offers or not ak_dir or not os.path.isdir(ak_dir):
            return False
        if resolve_akphone_layout(ak_dir)[1] is not None:
            return False
        messagebox.showwarning(
            "Không Có Thư Mục OFFERS",
            f"Bạn đã tích 'Tạo file cấu hình OFFERS' nhưng '{ak_dir}' không có thư mục OFFERS.\n"
            "AKPhoneS không dùng OFFERS nên tool sẽ bỏ qua file .ini (script vẫn được tạo bình thường).\n\n"
            "Nếu đang dùng AKPhoneS, hãy bỏ tích ở Tab 4.",
        )
        return True

    def _init_scripts_for_selected_device(self):
        if not self.selected_serial:
            messagebox.showwarning("Cảnh báo", "Vui lòng chọn 1 thiết bị từ bảng bên trái!")
            return
        if self.feeder_manager.is_running(self.selected_serial):
            messagebox.showwarning(
                "Máy Đang Chạy Auto",
                f"Máy [{self.selected_serial}] đang chạy auto. Bấm 'Dừng' (hoặc đợi chạy xong) rồi mới "
                "Khởi Tạo lại, để không làm rối vòng đang chạy.",
            )
            return
        self._warn_if_offers_missing(self.config.akphone_dir)
        if self._init_scripts_for_device(self.selected_serial):
            prof = self.device_profiles[self.selected_serial]
            self.device_tree.item(
                self.selected_serial,
                values=(
                    self.selected_serial,
                    f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                    prof.via_tag,
                    prof.progress_summary,
                    prof.status,
                ),
            )
            self._update_progress_display()
            self.session_manager.save_farm_session(self.device_profiles)
            messagebox.showinfo(
                "Khởi Tạo Thành Công",
                f"Đã tạo kịch bản xong cho máy [{self.selected_serial}] ({prof.via_tag}) với {prof.total_pages} Page!",
            )

    def _init_scripts_for_all_devices(self):
        if not self.device_profiles:
            messagebox.showwarning("Cảnh báo", "Chưa có thiết bị nào kết nối!")
            return
        self._warn_if_offers_missing(self.config.akphone_dir)
        success_count = 0
        busy = [s for s, p in self.device_profiles.items() if p.enabled and self.feeder_manager.is_running(s)]
        if busy:
            messagebox.showwarning(
                "Máy Đang Chạy Auto",
                f"Bỏ qua {len(busy)} máy đang chạy auto: {', '.join(busy)}.\n"
                "Dừng (hoặc đợi chạy xong) rồi Khởi Tạo lại các máy này.",
            )
        for serial, prof in self.device_profiles.items():
            if prof.enabled and serial not in busy:
                if self._init_scripts_for_device(serial):
                    success_count += 1
                    try:
                        self.device_tree.item(
                            serial,
                            values=(
                                serial,
                                f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                                prof.via_tag,
                                prof.progress_summary,
                                prof.status,
                            ),
                        )
                    except Exception:
                        pass

        self._update_progress_display()
        self.session_manager.save_farm_session(self.device_profiles)
        messagebox.showinfo(
            "Khởi Tạo Toàn Dàn",
            f"Đã khởi tạo thành công kịch bản cho {success_count}/{len(self.device_profiles)} thiết bị!",
        )

    def _start_selected_device(self):
        if not self.selected_serial:
            return
        prof = self.device_profiles.get(self.selected_serial)
        if not prof:
            return
        if self.feeder_manager.start_device(self.selected_serial, prof):
            self.device_tree.item(
                self.selected_serial,
                values=(
                    self.selected_serial,
                    f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                    prof.via_tag,
                    prof.progress_summary,
                    prof.status,
                ),
            )

    def _stop_selected_device(self):
        if not self.selected_serial:
            return
        self.feeder_manager.stop_device(self.selected_serial)
        prof = self.device_profiles.get(self.selected_serial)
        if prof:
            self.device_tree.item(
                self.selected_serial,
                values=(
                    self.selected_serial,
                    f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                    prof.via_tag,
                    prof.progress_summary,
                    prof.status,
                ),
            )

    def _resync_flags_selected_device(self):
        if not self.selected_serial:
            return
        prof = self.device_profiles.get(self.selected_serial)
        if not prof or not prof.pages_data:
            messagebox.showwarning("Cảnh báo", "Máy này chưa được khởi tạo kịch bản!")
            return
        # pages whose post is already live are left out: flagging them would post it twice
        active_pages = [
            pid for pid, inf in prof.pages_data.items() if inf.ready_to_post and not prof.page_off(pid)
        ]
        if self.adb.init_round_flags(self.selected_serial, active_pages):
            if not self.feeder_manager.is_running(self.selected_serial):
                # no worker watches these flags: the next Chạy checks which ones AKPhoneS used
                prof.round_flags, prof.round_flags_seen_at = sorted(active_pages), time.time()
                self.session_manager.save_device(self.selected_serial, prof)
            messagebox.showinfo(
                "Thành Công", f"Đã nạp lại {len(active_pages)} cờ Done lên máy {self.selected_serial}!"
            )
        else:
            messagebox.showerror("Lỗi", "Không thể nạp cờ Done lên điện thoại qua ADB!")

    def _start_all_devices(self):
        started = self.feeder_manager.start_all(self.device_profiles)
        self._update_progress_display()
        if started == 0:
            messagebox.showwarning(
                "Chưa Khởi Tạo",
                "Chưa có máy nào sẵn sàng để chạy! Vui lòng bấm 'Khởi tạo Scripts' trước.",
            )
        else:
            for serial, prof in self.device_profiles.items():
                try:
                    self.device_tree.item(
                        serial,
                        values=(
                            serial,
                            f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                            prof.via_tag,
                            prof.progress_summary,
                            prof.status,
                        ),
                    )
                except Exception:
                    pass
            messagebox.showinfo("Bắt Đầu Chạy Dàn", f"Đã kích hoạt giám sát đồng thời cho {started} máy điện thoại!")

    def _stop_all_devices(self):
        self.feeder_manager.stop_all()
        for serial, prof in self.device_profiles.items():
            try:
                self.device_tree.item(
                    serial,
                    values=(
                        serial,
                        f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                        prof.via_tag,
                        prof.progress_summary,
                        prof.status,
                    ),
                )
            except Exception:
                pass
        self._update_progress_display()

    def _update_progress_display(self):
        def update():
            running = sum(1 for w in self.feeder_manager.workers.values() if w.is_running())
            if running:
                self.btn_start_all.configure(
                    text=f"🟢 ĐANG CHẠY {running} MÁY", fg_color="#047857"
                )
            else:
                self.btn_start_all.configure(text="▶️ BẬT TẤT CẢ (START ALL)", fg_color="#10b981")

            # Update Tab 1 device treeview statuses
            for serial, prof in self.device_profiles.items():
                try:
                    self.device_tree.item(
                        serial,
                        values=(
                            serial,
                            f"{prof.device_info.brand} {prof.device_info.model}" if prof.device_info else "Android",
                            prof.via_tag,
                            prof.progress_summary,
                            prof.status,
                        ),
                    )
                except Exception:
                    pass

            # Update Tab 2 multi-column Live Progress Treeview
            if not hasattr(self, "progress_tree"):
                return

            now_str = datetime.now().strftime("%H:%M:%S")
            current_iids = set()

            for serial, prof in self.device_profiles.items():
                dev_short = serial[:12]
                if not prof.pages_data:
                    row_id = f"{serial}_empty"
                    current_iids.add(row_id)
                    vals = (
                        dev_short,
                        prof.via_tag,
                        "-",
                        "(Chưa khởi tạo kịch bản)",
                        "-",
                        prof.status,
                        now_str,
                    )
                    if self.progress_tree.exists(row_id):
                        self.progress_tree.item(row_id, values=vals)
                    else:
                        self.progress_tree.insert("", "end", iid=row_id, values=vals)
                    continue

                for pid, info in prof.pages_data.items():
                    row_id = f"{serial}_{pid}"
                    current_iids.add(row_id)
                    c_idx = info.current_idx
                    total = info.total_posts
                    prog_str = f"{c_idx}/{total}" if c_idx <= total else f"{total}/{total}"
                    vals = (
                        dev_short,
                        prof.via_tag,
                        pid,
                        info.fanpage_name,
                        prog_str,
                        "⛔ Đã tắt (bấm Bật lại page)" if prof.page_off(pid) else info.last_status,
                        now_str,
                    )
                    if self.progress_tree.exists(row_id):
                        self.progress_tree.item(row_id, values=vals)
                    else:
                        self.progress_tree.insert("", "end", iid=row_id, values=vals)

            # Cleanup rows that no longer exist
            for item in list(self.progress_tree.get_children()):
                if item not in current_iids:
                    try:
                        self.progress_tree.delete(item)
                    except Exception:
                        pass

        self.dispatch_ui(update)

    def _open_running_dir(self):
        d = self.config.running_dir
        if not d:
            messagebox.showwarning("Cảnh Báo", "Chưa cấu hình Thư mục đệm (Running Dir)!")
            return
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def _browse_running_dir(self):
        cur = self.entry_running_dir.get().strip() or self.config.running_dir
        d = filedialog.askdirectory(initialdir=cur if os.path.isdir(cur) else None)
        if d:
            self.entry_running_dir.delete(0, "end")
            self.entry_running_dir.insert(0, d)

    def _clean_payload_store_now(self):
        r_dir = self.entry_running_dir.get().strip() or self.config.running_dir
        if not r_dir:
            messagebox.showwarning("Cảnh Báo", "Chưa cấu hình Thư mục đệm (Running Dir)!")
            return
        cleaned = clean_completed_payloads(
            r_dir, max_age_days=0, keep=self.comment_queue.protected_archives()
        )

        def work():
            report = self.housekeeping(include_browser=True)
            browser_note = (
                "" if not self.comment_tracker.is_alive()
                else "\n(Cache trình duyệt chưa dọn vì đang theo dõi comment: tắt theo dõi rồi bấm lại.)"
            )
            self.dispatch_ui(lambda: messagebox.showinfo(
                "Đã Dọn Dẹp",
                f"Đã dọn {cleaned} bản lưu payload đã hoàn tất, và {report.summary()} "
                f"(ảnh lỗi cũ, lịch sử cũ, bản lưu kẹt, cache trình duyệt).{browser_note}",
            ))

        threading.Thread(target=work, name="Housekeeping", daemon=True).start()

    def _browse_akphone_dir(self):
        cur = self.entry_akphone_dir.get().strip() or self.config.akphone_dir
        d = filedialog.askdirectory(initialdir=cur if os.path.isdir(cur) else None)
        if d:
            self.entry_akphone_dir.delete(0, "end")
            self.entry_akphone_dir.insert(0, d)

    def _test_adb_button(self):
        self.adb.adb_path = self.entry_adb_path.get().strip()
        self.adb.socket_client.adb_path = self.adb.adb_path
        devices = self.adb.get_connected_devices()
        if devices:
            dev_list_str = "\n".join([d.display_name for d in devices])
            messagebox.showinfo(
                "ADB Kết Nối Tốt",
                f"Tìm thấy {len(devices)} thiết bị Android:\n\n{dev_list_str}",
            )
        else:
            messagebox.showwarning(
                "Cảnh Báo ADB", "Không tìm thấy thiết bị Android nào đang kết nối ở chế độ 'device'!"
            )
        self._refresh_devices_async()

    def _load_templates_to_ui(self):
        txt_path = self.config.template_txt_path
        ini_path = self.config.template_ini_path
        txt_content = txt_path.read_text(encoding="utf-8") if txt_path.is_file() else ""
        ini_content = ini_path.read_text(encoding="utf-8") if ini_path.is_file() else ""
        cmt_path = self.config.template_cmt_path
        cmt_content = cmt_path.read_text(encoding="utf-8") if cmt_path.is_file() else ""
        self.tpl_cmt_edit.delete("1.0", "end")
        self.tpl_cmt_edit.insert("1.0", cmt_content)
        self.tpl_txt_edit.delete("1.0", "end")
        self.tpl_txt_edit.insert("1.0", txt_content)
        self.tpl_ini_edit.delete("1.0", "end")
        self.tpl_ini_edit.insert("1.0", ini_content)

    def _template_reports(self, txt: str, cmt: str):
        return template_reports(txt, cmt, self.config.comment.flag_dir)

    @staticmethod
    def _report_text(post, cmt) -> str:
        parts = []
        for label, report in (("Mẫu đăng bài", post), ("Mẫu comment", cmt)):
            if report is None:
                continue
            parts += [f"❌ {label}: {e}" for e in report.errors]
            parts += [f"⚠ {label}: {w}" for w in report.warnings]
        return "\n".join(parts)

    def _save_templates_from_ui(self):
        txt_content = self.tpl_txt_edit.get("1.0", "end-1c")
        ini_content = self.tpl_ini_edit.get("1.0", "end-1c")
        cmt_content = self.tpl_cmt_edit.get("1.0", "end-1c")
        post, cmt = self._template_reports(txt_content, cmt_content)
        has_errors = bool(post.errors or (cmt and cmt.errors))
        if has_errors and not messagebox.askyesno(
            "Mẫu Có Lỗi",
            self._report_text(post, cmt)
            + "\n\nMẫu có lỗi sẽ làm sai luồng của tool. Khởi Tạo / Áp Dụng sẽ bị chặn tới khi sửa."
            "\nVẫn lưu bản nháp này?",
        ):
            return
        try:
            self.config.template_txt_path.write_text(txt_content, encoding="utf-8")
            self.config.template_ini_path.write_text(ini_content, encoding="utf-8")
            self.config.template_cmt_path.write_text(cmt_content, encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không thể lưu file mẫu: {e}")
            return
        if has_errors:
            return
        warnings = self._report_text(post, cmt)
        initialized = any(p.pages_data for p in self.device_profiles.values())
        text = "Đã lưu mẫu." + (f"\n\nLưu ý:\n{warnings}" if warnings else "")
        if not initialized:
            messagebox.showinfo("Đã Lưu Mẫu", text + "\n\nMẫu sẽ dùng cho lần Khởi Tạo tới.")
            return
        if messagebox.askyesno(
            "Đã Lưu Mẫu",
            text + "\n\nÁp dụng mẫu mới cho script của các máy đã khởi tạo ngay bây giờ?"
            "\n(Chỉ viết lại file script; không đụng tới bài, kho, tiến độ.)",
        ):
            self._apply_templates_clicked(confirm=False)

    def apply_templates(self):
        """Rewrite every initialized page's scripts from the current templates.

        Only the script files change: running dir, store, progress and the comment queue
        stay as they are. Returns (pages rewritten, errors); refuses a broken template.
        """
        txt = self.config.template_txt_path.read_text(encoding="utf-8")
        cmt_path = self.config.template_cmt_path
        cmt = cmt_path.read_text(encoding="utf-8") if cmt_path.is_file() else ""
        post_report, cmt_report = self._template_reports(txt, cmt)
        errors = post_report.errors + (cmt_report.errors if cmt_report else [])
        if errors:
            return 0, errors
        scripts_dest, offers_dest = resolve_akphone_layout(self.config.akphone_dir)
        write_offers = self.config.write_offers and offers_dest is not None
        ini_tpl = self.config.template_ini_path.read_text(encoding="utf-8") if write_offers else ""
        flag_dir = self.config.comment.flag_dir
        count = 0
        for serial, prof in self.device_profiles.items():
            for pid, info in prof.pages_data.items():
                _write_atomic(
                    scripts_dest / f"{pid}.txt",
                    render_post_script(txt, pid, info.fanpage_name, info.video_dir),
                )
                if cmt:
                    _write_atomic(
                        scripts_dest / cmt_script_filename(pid),
                        render_cmt_script(cmt, pid, info.fanpage_name, flag_dir)
                        if self.comment_queue.has_marked(serial, pid)
                        else IDLE_CMT_SCRIPT,
                    )
                if write_offers:
                    _write_atomic(
                        offers_dest / f"{pid}.ini",
                        ini_tpl.replace("{ID}", pid)
                        .replace("{PAGE_NAME}", info.fanpage_name)
                        .replace("{VIDEO_DIR}", str(info.video_dir)),
                    )
                count += 1
        logger.info(f"🔄 Đã áp dụng mẫu mới cho {count} script page (không đụng tới bài/kho/tiến độ).")
        return count, []

    def _apply_templates_clicked(self, confirm: bool = True):
        if not any(p.pages_data for p in self.device_profiles.values()):
            messagebox.showinfo("Áp Dụng Mẫu", "Chưa có máy nào được Khởi Tạo: mẫu sẽ dùng cho lần Khởi Tạo tới.")
            return
        if confirm and not messagebox.askyesno(
            "Áp Dụng Mẫu",
            "Viết lại script đăng bài + script comment của mọi page đã khởi tạo theo mẫu hiện tại?\n"
            "Không đụng tới bài, kho, tiến độ. Nên làm lúc AKPhoneS không chạy script.",
        ):
            return
        try:
            count, errors = self.apply_templates()
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không áp dụng được mẫu: {e}")
            return
        if errors:
            messagebox.showerror(
                "Mẫu Có Lỗi - Chưa Áp Dụng", "\n".join(f"❌ {e}" for e in errors)
            )
            return
        messagebox.showinfo("Đã Áp Dụng Mẫu", f"Đã viết lại script cho {count} page.")

    def _save_system_settings(self):
        try:
            self.config.akphone_dir = self.entry_akphone_dir.get().strip()
            self.config.adb_path = self.entry_adb_path.get().strip()
            self.config.running_dir = self.entry_running_dir.get().strip()
            self.config.watchdog_timeout = int(self.entry_watchdog_timeout.get().strip())
            self.config.auto_screencap = bool(self.var_screencap.get())
            self.config.auto_clean_storage = bool(self.var_clean_storage.get())
            self.config.auto_clean_payloads = bool(self.var_clean_payloads.get())
            self.config.payload_retention_days = int(self.entry_retention_days.get().strip())
            self.config.write_offers = bool(self.var_write_offers.get())

            self.config.telegram = TelegramConfig(
                bot_token=self.entry_tg_token.get().strip(),
                chat_id=self.entry_tg_chat_id.get().strip(),
                enabled=bool(self.var_telegram_enabled.get()),
            )
            self.notifier.update_config(self.config.telegram)
            self.adb.adb_path = self.config.adb_path
            self.adb.socket_client.adb_path = self.config.adb_path
            self.config.save()
            messagebox.showinfo("Thành Công", "Đã lưu cấu hình hệ thống!")
            self._warn_if_offers_missing(self.config.akphone_dir)
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không thể lưu cài đặt: {e}")

    def _test_telegram(self):
        token = self.entry_tg_token.get().strip()
        chat_id = self.entry_tg_chat_id.get().strip()
        if not token or not chat_id:
            messagebox.showwarning(
                "Thiếu Thông Tin", "Vui lòng nhập Bot Token và Chat ID trước khi gửi thử!"
            )
            return
        cfg = TelegramConfig(bot_token=token, chat_id=chat_id, enabled=True)
        test_notifier = TelegramNotifier(cfg)
        text = (
            f"🔔 <b>[AKPhone Farm Test Alert]</b>\n"
            f"Thời gian: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            f"Đã cấu hình Telegram thành công!"
        )

        def work():  # sent right away so the result is known (no queue for a test)
            try:
                test_notifier._post_text(text)
                self.dispatch_ui(lambda: messagebox.showinfo("Đã Gửi", "Telegram đã nhận tin thử nghiệm!"))
            except Exception as e:
                self.dispatch_ui(lambda err=e: messagebox.showerror("Gửi Thất Bại", f"Không gửi được Telegram:\n{err}"))

        threading.Thread(target=work, name="TelegramTest", daemon=True).start()

    def _check_existing_session(self):
        session = self.session_manager.load_session()
        if session and session.get("mode") == "farm" and session.get("devices"):
            devs = session["devices"]
            if messagebox.askyesno(
                "Khôi Phục Tiến Độ",
                f"Phát hiện phiên làm việc cũ gồm {len(devs)} máy.\nBạn có muốn khôi phục lại không?",
            ):
                caption_dir = resolve_akphone_layout(self.config.akphone_dir)[0] / "FILES"
                restored = restore_device_profiles(
                    session, self.device_profiles, self.config.running_dir, caption_dir
                )
                for serial, prof in self.device_profiles.items():  # the setting lives in the config
                    prof.disabled_pages = list(
                        self.config.device_profiles.get(serial, {}).get("disabled_pages") or []
                    )
                self._update_progress_display()
                logger.info(
                    f"Đã khôi phục tiến độ {restored} máy (vị trí bài, bài đã đăng, lỗi nạp bài). "
                    "Bấm Chạy để tiếp tục."
                )
