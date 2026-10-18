"""FarmTabMixin for the main window."""


import os
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from akphone_dispatcher.core.models import (
    find_duplicate_fb_indexes,
)
from akphone_dispatcher.core.scanner import (
    scan_source_directory,
)
from akphone_dispatcher.utils.helpers import sanitize_fb_index
from akphone_dispatcher.services.farm_service import ProvisionError, provision_device
from akphone_dispatcher.utils.logger import logger

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")




class FarmTabMixin:
    """Widgets and handlers of this tab (mixed into AKPhoneApp)."""

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
            text="Quét Kho & Điền Mẫu",
            width=130,
            height=22,
            fg_color="#475569",
            command=self._scan_and_fill_profile_pages,
        ).pack(side="right")

        self.text_profile_pages = ctk.CTkTextbox(
            self.right_box, height=85, font=ctk.CTkFont(family="Consolas", size=11)
        )
        self.text_profile_pages.pack(fill="both", expand=True, padx=12, pady=3)
        self.text_profile_pages.bind("<KeyRelease>", lambda e: self._on_detail_field_changed())

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
        raw_via = self.entry_profile_m.get()
        prof.fb_index = sanitize_fb_index(raw_via)
        if raw_via != prof.fb_index and raw_via.strip():
            # Strip spaces/special characters as the user types (e.g. "a b/c" -> "abc")
            self.entry_profile_m.delete(0, "end")
            self.entry_profile_m.insert(0, prof.fb_index)
        prof.source_dir = self.entry_profile_src.get().strip()
        prof.page_names = self.text_profile_pages.get("1.0", "end-1c").strip()

        # Update config profile
        self.config.set_profile(
            self.selected_serial,
            {
                "fb_index": prof.fb_index,
                "source_dir": prof.source_dir,
                "page_names": prof.page_names,
                "enabled": prof.enabled,
            },
        )
        # Update Treeview row
        try:
            self._refresh_device_row(self.selected_serial)
        except Exception:
            pass

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
                    "enabled": prof.enabled,
                },
            )
            try:
                self._refresh_device_row(d.serial)
            except Exception:
                pass

        if self.selected_serial:
            self._select_device_serial(self.selected_serial)

        messagebox.showinfo(
            "Thành Công",
            f"Đã tự động gán thứ tự fb1..fb{len(self.connected_devices)} cho toàn bộ {len(self.connected_devices)} máy!",
        )

    def _warn_duplicate_fb_index(self) -> bool:
        """Show an error and return True if enabled devices share an fb index."""
        dups = find_duplicate_fb_indexes(self.device_profiles)
        if not dups:
            return False
        detail = "\n".join(f"fb{idx}: {', '.join(sers)}" for idx, sers in dups.items())
        messagebox.showerror(
            "Trùng Chỉ Số Via {m}",
            "Nhiều máy đang dùng cùng chỉ số Via, script/thư mục đệm sẽ đè lên nhau:\n\n"
            f"{detail}\n\nHãy đổi chỉ số hoặc tắt bớt máy.",
        )
        return True

    def _init_scripts_for_device(self, serial: str) -> bool:
        """Compile and seed 1 Page = 1 Script files for a single Phone/Via."""
        prof = self.device_profiles.get(serial)
        if not prof:
            return False
        if prof.enabled and self._warn_duplicate_fb_index():
            return False
        try:
            provision_device(prof, self.config, serial)
        except ProvisionError as err:
            messagebox.showerror(err.title, err.message)
            return False
        return True

    def _init_scripts_for_selected_device(self):
        if not self.selected_serial:
            messagebox.showwarning("Cảnh báo", "Vui lòng chọn 1 thiết bị từ bảng bên trái!")
            return
        if self._init_scripts_for_device(self.selected_serial):
            prof = self.device_profiles[self.selected_serial]
            self._refresh_device_row(self.selected_serial)
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
        success_count = 0
        for serial, prof in self.device_profiles.items():
            if prof.enabled:
                if self._init_scripts_for_device(serial):
                    success_count += 1
                    try:
                        self._refresh_device_row(serial)
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
        if self._warn_duplicate_fb_index():
            return
        if self.feeder_manager.start_device(self.selected_serial, prof):
            self._refresh_device_row(self.selected_serial)

    def _stop_selected_device(self):
        if not self.selected_serial:
            return
        self.feeder_manager.stop_device(self.selected_serial)
        prof = self.device_profiles.get(self.selected_serial)
        if prof:
            self._refresh_device_row(self.selected_serial)

    def _resync_flags_selected_device(self):
        if not self.selected_serial:
            return
        prof = self.device_profiles.get(self.selected_serial)
        if not prof or not prof.pages_data:
            messagebox.showwarning("Cảnh báo", "Máy này chưa được khởi tạo kịch bản!")
            return
        active_pages = [
            pid for pid, inf in prof.pages_data.items() if inf.current_idx <= inf.total_posts
        ]
        if self.adb.init_round_flags(self.selected_serial, active_pages):
            messagebox.showinfo(
                "Thành Công", f"Đã nạp lại {len(active_pages)} cờ Done lên máy {self.selected_serial}!"
            )
        else:
            messagebox.showerror("Lỗi", "Không thể nạp cờ Done lên điện thoại qua ADB!")

    def _start_all_devices(self):
        if self._warn_duplicate_fb_index():
            return
        started = self.feeder_manager.start_all(self.device_profiles)
        if started == 0:
            messagebox.showwarning(
                "Chưa Khởi Tạo",
                "Chưa có máy nào sẵn sàng để chạy! Vui lòng bấm 'Khởi tạo Scripts' trước.",
            )
        else:
            for serial, prof in self.device_profiles.items():
                try:
                    self._refresh_device_row(serial)
                except Exception:
                    pass
            messagebox.showinfo("Bắt Đầu Chạy Dàn", f"Đã kích hoạt giám sát đồng thời cho {started} máy điện thoại!")

    def _stop_all_devices(self):
        self.feeder_manager.stop_all()
        for serial, prof in self.device_profiles.items():
            try:
                self._refresh_device_row(serial)
            except Exception:
                pass
        self._update_progress_display()

    def _open_running_dir(self):
        d = self.config.running_dir
        if not d:
            messagebox.showwarning("Cảnh Báo", "Chưa cấu hình Thư mục đệm (Running Dir)!")
            return
        os.makedirs(d, exist_ok=True)
        os.startfile(d)
