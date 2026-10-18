"""SettingsTabMixin for the main window."""


from datetime import datetime
import os
from tkinter import filedialog, messagebox

import customtkinter as ctk

from akphone_dispatcher.core.models import (
    TelegramConfig,
)
from akphone_dispatcher.core.payload import (
    clean_completed_payloads,
)
from akphone_dispatcher.services.notifier import TelegramNotifier

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")




class SettingsTabMixin:
    """Widgets and handlers of this tab (mixed into AKPhoneApp)."""

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
        ctk.CTkLabel(r1, text="Thư mục AKPhone:", width=170, anchor="w").pack(side="left")
        self.entry_akphone_dir = ctk.CTkEntry(r1)
        self.entry_akphone_dir.pack(side="left", fill="x", expand=True, padx=6)
        self.entry_akphone_dir.insert(0, self.config.akphone_dir)
        ctk.CTkButton(r1, text="Chọn...", width=70, command=self._browse_akphone_dir).pack(
            side="left"
        )

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
            text="🧹 Dọn Dẹp Payload Store Ngay",
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
        cleaned = clean_completed_payloads(r_dir, max_age_days=0)
        messagebox.showinfo(
            "Đã Dọn Dẹp",
            f"Đã dọn dẹp {cleaned} bản lưu trữ payload đã hoàn tất khỏi .payload-store!",
        )

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

    def _save_system_settings(self):
        # Validate every numeric field first so a bad value never leaves a half-applied config
        try:
            watchdog_timeout = int(self.entry_watchdog_timeout.get().strip())
            retention_days = int(self.entry_retention_days.get().strip())
            if watchdog_timeout < 1 or retention_days < 0:
                raise ValueError("Watchdog phải >= 1 giây và số ngày lưu vết phải >= 0.")
        except ValueError as e:
            messagebox.showerror("Giá Trị Không Hợp Lệ", f"Vui lòng nhập số nguyên hợp lệ.\n{e}")
            return
        try:
            self.config.akphone_dir = self.entry_akphone_dir.get().strip()
            self.config.adb_path = self.entry_adb_path.get().strip()
            self.config.running_dir = self.entry_running_dir.get().strip()
            self.config.watchdog_timeout = watchdog_timeout
            self.config.auto_screencap = bool(self.var_screencap.get())
            self.config.auto_clean_storage = bool(self.var_clean_storage.get())
            self.config.auto_clean_payloads = bool(self.var_clean_payloads.get())
            self.config.payload_retention_days = retention_days

            current_tg = self.config.telegram
            self.config.telegram = TelegramConfig(
                bot_token=self.entry_tg_token.get().strip(),
                chat_id=self.entry_tg_chat_id.get().strip(),
                enabled=bool(self.var_telegram_enabled.get()),
                notify_on_success=current_tg.notify_on_success,
                notify_on_error=current_tg.notify_on_error,
                send_screenshots=current_tg.send_screenshots,
            )
            self.notifier.update_config(self.config.telegram)
            self.adb.adb_path = self.config.adb_path
            self.adb.socket_client.adb_path = self.config.adb_path
            self.config.save()
            messagebox.showinfo("Thành Công", "Đã lưu cấu hình hệ thống!")
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
        test_notifier.send_message(
            f"🔔 <b>[AKPhone Farm Test Alert]</b>\n"
            f"Thời gian: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            f"Đã cấu hình Telegram thành công!"
        )
        messagebox.showinfo("Đã Gửi", "Đã gửi thử nghiệm đến Telegram!")
