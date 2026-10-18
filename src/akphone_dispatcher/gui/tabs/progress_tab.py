"""ProgressTabMixin for the main window."""


from datetime import datetime
from tkinter import ttk

import customtkinter as ctk


ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")




class ProgressTabMixin:
    """Widgets and handlers of this tab (mixed into AKPhoneApp)."""

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

        table_box = ctk.CTkFrame(tab, corner_radius=6, fg_color="#181b24")
        table_box.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # Modern multi-column Treeview
        tree_frame = ctk.CTkFrame(table_box, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=8)

        columns = ("serial", "via", "page_id", "fanpage_name", "progress", "status", "updated_at")
        self.progress_tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="browse"
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

    def _update_progress_display(self):
        def update():
            # Update Tab 1 device treeview statuses
            for serial, prof in self.device_profiles.items():
                try:
                    self._refresh_device_row(serial)
                except Exception:
                    pass

            # Persist progress so a crash/restart resumes from the real position
            if any(p.pages_data for p in self.device_profiles.values()):
                self.session_manager.save_farm_session(self.device_profiles)

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
                        info.last_status,
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
