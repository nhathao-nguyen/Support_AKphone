"""Tab 5: view tracking and automatic comments (accounts, page links, thresholds, status)."""

from datetime import datetime
import threading
from tkinter import messagebox, ttk

import customtkinter as ctk

from akphone_dispatcher.tracker.accounts import STATE_DISABLED, STATE_OK
from akphone_dispatcher.tracker.login import login_account
from akphone_dispatcher.utils.logger import logger

_STATE_TEXT = {
    "ok": "🟢 Sẵn sàng",
    "needs_login": "🔑 Cần đăng nhập lại",
    "checkpoint": "⛔ Checkpoint/khóa",
    "disabled": "⏸ Tạm dừng",
}
_POST_STATUS_TEXT = {
    "queued": "Trong kho",
    "fed": "Đã nạp",
    "posted": "Đã đăng",
    "post_failed": "Đăng lỗi",
    "tracking": "Đang theo dõi",
    "threshold_hit": "Đạt ngưỡng",
    "comment_marked": "Chờ comment",
    "commented": "Đã comment",
    "comment_failed": "Comment lỗi",
    "expired": "Hết hạn",
}


class TrackerTabMixin:
    """Widgets and handlers of this tab (mixed into AKPhoneApp)."""

    def _build_tab_tracker(self):
        body = ctk.CTkScrollableFrame(self.tab_tracker, fg_color="transparent")
        body.pack(fill="both", expand=True)

        def card(title):
            frame = ctk.CTkFrame(body, corner_radius=6, fg_color="#1e222d")
            frame.pack(fill="x", padx=8, pady=6)
            ctk.CTkLabel(
                frame, text=title, font=ctk.CTkFont(size=13, weight="bold"), text_color="#38bdf8"
            ).pack(anchor="w", padx=10, pady=(6, 4))
            return frame

        def row(parent):
            frame = ctk.CTkFrame(parent, fg_color="transparent")
            frame.pack(fill="x", padx=10, pady=3)
            return frame

        # ---- control --------------------------------------------------------------------------
        ctrl = card("🔎 THEO DÕI LƯỢT XEM & TỰ ĐỘNG COMMENT")
        r = row(ctrl)
        self.btn_tracker_toggle = ctk.CTkButton(
            r, text="▶️ Bật theo dõi", width=150, fg_color="#10b981", hover_color="#059669",
            command=self._tracker_toggle,
        )
        self.btn_tracker_toggle.pack(side="left", padx=(0, 10))
        self.lbl_tracker_status = ctk.CTkLabel(r, text="Chưa bật", anchor="w", justify="left")
        self.lbl_tracker_status.pack(side="left", fill="x", expand=True)

        # ---- settings -------------------------------------------------------------------------
        st = card("⚙️ NGƯỠNG, HẠN THEO DÕI VÀ COMMENT")
        r1 = row(st)
        ctk.CTkLabel(r1, text="Ngưỡng lượt xem chung:", width=210, anchor="w").pack(side="left")
        self.entry_view_threshold = ctk.CTkEntry(r1, width=100)
        self.entry_view_threshold.pack(side="left")
        self.entry_view_threshold.insert(0, str(self.config.view_threshold))
        r2 = row(st)
        ctk.CTkLabel(r2, text="Hạn theo dõi (ngày):", width=210, anchor="w").pack(side="left")
        self.entry_track_days = ctk.CTkEntry(r2, width=100)
        self.entry_track_days.pack(side="left")
        self.entry_track_days.insert(0, f"{self.config.track_expire_days:g}")
        r3 = row(st)
        ctk.CTkLabel(r3, text="Tối đa comment / page / ngày:", width=210, anchor="w").pack(side="left")
        self.entry_comment_cap = ctk.CTkEntry(r3, width=100)
        self.entry_comment_cap.pack(side="left")
        self.entry_comment_cap.insert(0, str(self.config.comment_daily_cap))
        r4 = row(st)
        ctk.CTkLabel(r4, text="Thư mục mục báo việc (ĐT):", width=210, anchor="w").pack(side="left")
        self.entry_marker_dir = ctk.CTkEntry(r4)
        self.entry_marker_dir.pack(side="left", fill="x", expand=True)
        self.entry_marker_dir.insert(0, self.config.comment_marker_dir)
        r5 = row(st)
        ctk.CTkLabel(r5, text="Thư mục SCRIPTS riêng:", width=210, anchor="w").pack(side="left")
        self.entry_scripts_dir = ctk.CTkEntry(r5, placeholder_text="(để trống = <AKPhone>\\SCRIPTS, vd AKPhoneS: ...\\assets\\SCRIPTS)")
        self.entry_scripts_dir.pack(side="left", fill="x", expand=True)
        self.entry_scripts_dir.insert(0, self.config.scripts_dir)
        r6 = row(st)
        self.var_write_offers = ctk.BooleanVar(value=self.config.write_offers)
        ctk.CTkCheckBox(
            r6, text="Ghi file OFFERS (.ini) — tắt nếu bản AKPhone của bạn không dùng", variable=self.var_write_offers
        ).pack(side="left")
        r7 = row(st)
        ctk.CTkButton(r7, text="💾 Lưu cài đặt", width=140, command=self._tracker_save_settings).pack(side="left")

        # ---- accounts -------------------------------------------------------------------------
        ac = card("👤 TÀI KHOẢN QUÉT (đăng nhập tay, mỗi tài khoản một proxy cố định)")
        self.tree_accounts = ttk.Treeview(
            ac, columns=("name", "proxy", "state", "note"), show="headings", height=4, selectmode="browse"
        )
        for col, text, width in (("name", "Tên", 110), ("proxy", "Proxy", 190), ("state", "Trạng thái", 160), ("note", "Ghi chú", 360)):
            self.tree_accounts.heading(col, text=text)
            self.tree_accounts.column(col, width=width, anchor="w")
        self.tree_accounts.pack(fill="x", padx=10, pady=4)
        r = row(ac)
        ctk.CTkLabel(r, text="Tên:", width=40, anchor="w").pack(side="left")
        self.entry_acc_name = ctk.CTkEntry(r, width=110, placeholder_text="acc1")
        self.entry_acc_name.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(r, text="Proxy:", width=50, anchor="w").pack(side="left")
        self.entry_acc_proxy = ctk.CTkEntry(r, placeholder_text="socks5 host:port:user:pass  hoặc  host:port  (proxy NGOÀI Việt Nam nếu page chặn VN)")
        self.entry_acc_proxy.pack(side="left", fill="x", expand=True)
        r = row(ac)
        for text, cmd, color in (
            ("🔑 Đăng nhập / đăng nhập lại", self._tracker_login_account, "#0284c7"),
            ("⏯ Bật/Tạm dừng", self._tracker_toggle_account, "#475569"),
            ("🗑 Xóa khỏi danh sách", self._tracker_remove_account, "#7f1d1d"),
        ):
            ctk.CTkButton(r, text=text, width=190, fg_color=color, command=cmd).pack(side="left", padx=(0, 8))
        self.tree_accounts.bind("<<TreeviewSelect>>", self._tracker_on_account_select)

        # ---- pages ----------------------------------------------------------------------------
        pg = card("📄 PAGE: LINK FACEBOOK VÀ NGƯỠNG RIÊNG")
        self.tree_pages = ttk.Treeview(
            pg, columns=("serial", "page", "name", "url", "thr"), show="headings", height=6, selectmode="browse"
        )
        for col, text, width in (("serial", "Máy", 110), ("page", "Mã page", 100), ("name", "Tên page", 170), ("url", "Link Facebook", 330), ("thr", "Ngưỡng", 110)):
            self.tree_pages.heading(col, text=text)
            self.tree_pages.column(col, width=width, anchor="w")
        self.tree_pages.pack(fill="x", padx=10, pady=4)
        self.tree_pages.bind("<<TreeviewSelect>>", self._tracker_on_page_select)
        r = row(pg)
        ctk.CTkLabel(r, text="Link:", width=40, anchor="w").pack(side="left")
        self.entry_page_url = ctk.CTkEntry(r, placeholder_text="https://www.facebook.com/profile.php?id=...")
        self.entry_page_url.pack(side="left", fill="x", expand=True)
        r = row(pg)
        self.var_thr_mode = ctk.StringVar(value="global")
        ctk.CTkRadioButton(r, text="Dùng ngưỡng chung", variable=self.var_thr_mode, value="global").pack(side="left", padx=(0, 14))
        ctk.CTkRadioButton(r, text="Ngưỡng riêng:", variable=self.var_thr_mode, value="custom").pack(side="left")
        self.entry_page_thr = ctk.CTkEntry(r, width=100)
        self.entry_page_thr.pack(side="left", padx=(6, 14))
        ctk.CTkButton(r, text="💾 Lưu page", width=110, command=self._tracker_save_page).pack(side="left", padx=(0, 8))
        ctk.CTkButton(r, text="🔍 Đọc thử page này", width=150, fg_color="#475569", command=self._tracker_probe_page).pack(side="left")

        # ---- ledger ---------------------------------------------------------------------------
        lg = card("📒 SỔ CÁI BÀI ĐĂNG (mới cập nhật nhất ở trên)")
        self.lbl_ledger_counts = ctk.CTkLabel(lg, text="", anchor="w", justify="left")
        self.lbl_ledger_counts.pack(fill="x", padx=10)
        self.tree_ledger = ttk.Treeview(
            lg, columns=("uid", "page", "status", "views", "updated"), show="headings", height=9, selectmode="browse"
        )
        for col, text, width in (("uid", "Mã bài", 210), ("page", "Page", 110), ("status", "Trạng thái", 130), ("views", "Lượt xem", 90), ("updated", "Cập nhật", 140)):
            self.tree_ledger.heading(col, text=text)
            self.tree_ledger.column(col, width=width, anchor="w")
        self.tree_ledger.pack(fill="x", padx=10, pady=(4, 10))

        self._tracker_refresh()
        self.after(8000, self._tracker_auto_refresh)

    # ---- refresh --------------------------------------------------------------------------------
    def _tracker_auto_refresh(self):
        try:
            self._tracker_refresh()
        finally:
            self.after(8000, self._tracker_auto_refresh)

    def _tracker_refresh(self):
        rt = self.tracker_runtime
        status = rt.status()
        by = status["by_status"]
        self.btn_tracker_toggle.configure(
            text="⏹ Tắt theo dõi" if status["running"] else "▶️ Bật theo dõi",
            fg_color="#ef4444" if status["running"] else "#10b981",
        )
        text = (
            f"{'🟢 Đang chạy' if status['running'] else '⚪ Đang tắt'} | tài khoản sẵn sàng "
            f"{status['accounts_usable']}/{status['accounts_total']} | lượt vừa rồi: đọc "
            f"{status['polled']} page, khớp {status['matched']} bài, đạt ngưỡng {status['hits']}, lỗi {status['failed']}"
        )
        if status["errors"]:
            text += f"\n⚠️ {status['errors'][0][:140]}"
        self.lbl_tracker_status.configure(text=text)
        self.lbl_ledger_counts.configure(
            text=" | ".join(f"{_POST_STATUS_TEXT.get(k, k)}: {v}" for k, v in sorted(by.items())) or "Chưa có bài nào."
        )

        self._fill(self.tree_accounts, [
            (a.name, a.proxy or "(không proxy)", _STATE_TEXT.get(a.state, a.state), a.note) for a in rt.store.all()
        ], key=lambda v: v[0])

        urls = {k.lower(): v for k, v in rt.page_urls().items()}
        rows = []
        for serial, prof in self.device_profiles.items():
            for pid, info in prof.pages_data.items():
                url = urls.get(pid.lower()) or urls.get(info.fanpage_name.lower(), "")
                cfg = rt.ledger.get_page_threshold(serial, pid)
                thr = f"riêng {cfg['custom_threshold']}" if cfg and cfg["mode"] == "custom" else f"chung ({self.config.view_threshold})"
                rows.append((serial, pid, info.fanpage_name, url or "(chưa có link)", thr))
        self._fill(self.tree_pages, rows, key=lambda v: f"{v[0]}|{v[1]}")

        ledger_rows = []
        for post in rt.ledger.list_recent(150):
            when = datetime.fromtimestamp(post["updated_at"]).strftime("%d/%m %H:%M") if post["updated_at"] else ""
            ledger_rows.append((
                post["post_uid"], post["page_id"], _POST_STATUS_TEXT.get(post["status"], post["status"]),
                "" if post["views"] is None else f"{post['views']:,}", when,
            ))
        self._fill(self.tree_ledger, ledger_rows, key=lambda v: v[0])

    @staticmethod
    def _fill(tree, rows, key):
        """Replace the rows of a table but keep the user's selection."""
        selected = tree.selection()
        for item in tree.get_children():
            tree.delete(item)
        for values in rows:
            tree.insert("", "end", iid=str(key(values)), values=values)
        for item in selected:
            if tree.exists(item):
                tree.selection_set(item)

    # ---- tracker control ------------------------------------------------------------------------
    def _tracker_toggle(self):
        rt = self.tracker_runtime
        if rt.is_running():
            rt.stop()
        else:
            if not rt.store.usable():
                messagebox.showwarning(
                    "Chưa có tài khoản", "Hãy đăng nhập ít nhất một tài khoản quét (mục Tài khoản) trước khi bật theo dõi."
                )
                return
            rt.start()
        self._tracker_refresh()

    def _tracker_save_settings(self):
        try:
            threshold = int(self.entry_view_threshold.get().strip())
            days = float(self.entry_track_days.get().strip().replace(",", "."))
            cap = int(self.entry_comment_cap.get().strip())
            if threshold < 1 or days <= 0 or cap < 1:
                raise ValueError("Ngưỡng, hạn theo dõi và giới hạn comment phải lớn hơn 0.")
        except ValueError as e:
            messagebox.showerror("Giá Trị Không Hợp Lệ", f"Vui lòng nhập số hợp lệ.\n{e}")
            return
        marker_dir = self.entry_marker_dir.get().strip() or "/storage/emulated/0/Cmt"
        self.config.view_threshold, self.config.track_expire_days = threshold, days
        self.config.comment_daily_cap, self.config.comment_marker_dir = cap, marker_dir
        self.config.scripts_dir = self.entry_scripts_dir.get().strip()
        self.config.write_offers = bool(self.var_write_offers.get())
        self.config.save()
        self.tracker_runtime.apply_settings()
        self._tracker_refresh()
        messagebox.showinfo("Đã Lưu", "Đã lưu cài đặt theo dõi và comment.")

    # ---- accounts -------------------------------------------------------------------------------
    def _tracker_on_account_select(self, _event=None):
        sel = self.tree_accounts.selection()
        if not sel:
            return
        account = self.tracker_runtime.store.get(sel[0])
        if account:
            self.entry_acc_name.delete(0, "end")
            self.entry_acc_name.insert(0, account.name)
            self.entry_acc_proxy.delete(0, "end")
            self.entry_acc_proxy.insert(0, account.proxy)

    def _tracker_login_account(self):
        name = "".join(c for c in self.entry_acc_name.get().strip() if c.isalnum() or c in "-_")
        if not name:
            messagebox.showwarning("Thiếu tên", "Nhập tên tài khoản (chữ và số, vd acc1).")
            return
        proxy = self.entry_acc_proxy.get().strip()
        if not messagebox.askokcancel(
            "Đăng nhập tay",
            f"Một cửa sổ Chrome sẽ mở cho tài khoản '{name}'.\n\n"
            "• Tự đăng nhập Facebook (mật khẩu, mã 2 lớp...). Tool không thấy và không lưu mật khẩu.\n"
            "• Nếu có proxy có mật khẩu, Chrome sẽ hỏi — bạn nhập vào đó.\n"
            "• Đăng nhập xong thì ĐÓNG cửa sổ Chrome để lưu phiên.",
        ):
            return

        def work():
            try:
                login_account(name, proxy, store=self.tracker_runtime.store, notify=logger.info)
            except Exception as e:
                logger.error(f"Đăng nhập tài khoản {name} lỗi: {e}")
            self.dispatch_ui(self._tracker_refresh)

        threading.Thread(target=work, name=f"login-{name}", daemon=True).start()

    def _selected_account(self):
        sel = self.tree_accounts.selection()
        return self.tracker_runtime.store.get(sel[0]) if sel else None

    def _tracker_toggle_account(self):
        account = self._selected_account()
        if account is None:
            messagebox.showwarning("Chưa chọn", "Chọn một tài khoản trong bảng.")
            return
        store = self.tracker_runtime.store
        if account.state == STATE_DISABLED:
            store.mark(account.name, STATE_OK, "bật lại thủ công")
        else:
            store.mark(account.name, STATE_DISABLED, "tạm dừng thủ công")
        self._tracker_refresh()

    def _tracker_remove_account(self):
        account = self._selected_account()
        if account is None:
            messagebox.showwarning("Chưa chọn", "Chọn một tài khoản trong bảng.")
            return
        if messagebox.askyesno("Xóa tài khoản", f"Bỏ '{account.name}' khỏi danh sách quét? (Phiên đăng nhập trong thư mục hồ sơ không bị xóa.)"):
            self.tracker_runtime.store.remove(account.name)
            self._tracker_refresh()

    # ---- pages ----------------------------------------------------------------------------------
    def _tracker_selected_page(self):
        sel = self.tree_pages.selection()
        if not sel:
            return None
        serial, page_id = sel[0].split("|", 1)
        info = self.device_profiles.get(serial).pages_data.get(page_id) if serial in self.device_profiles else None
        return (serial, page_id, info.fanpage_name if info else "") if info else None

    def _tracker_on_page_select(self, _event=None):
        picked = self._tracker_selected_page()
        if not picked:
            return
        serial, page_id, name = picked
        urls = {k.lower(): v for k, v in self.tracker_runtime.page_urls().items()}
        self.entry_page_url.delete(0, "end")
        self.entry_page_url.insert(0, urls.get(page_id.lower()) or urls.get(name.lower(), ""))
        cfg = self.tracker_runtime.ledger.get_page_threshold(serial, page_id)
        custom = bool(cfg and cfg["mode"] == "custom")
        self.var_thr_mode.set("custom" if custom else "global")
        self.entry_page_thr.delete(0, "end")
        if custom:
            self.entry_page_thr.insert(0, str(cfg["custom_threshold"]))

    def _tracker_save_page(self):
        picked = self._tracker_selected_page()
        if not picked:
            messagebox.showwarning("Chưa chọn", "Chọn một page trong bảng.")
            return
        serial, page_id, name = picked
        try:
            self.tracker_runtime.set_page_url(name or page_id, self.entry_page_url.get())
            if self.var_thr_mode.get() == "custom":
                self.tracker_runtime.ledger.set_page_threshold(serial, page_id, "custom", int(self.entry_page_thr.get().strip()))
            else:
                self.tracker_runtime.ledger.set_page_threshold(serial, page_id, "global")
        except ValueError as e:
            messagebox.showerror("Giá Trị Không Hợp Lệ", str(e))
            return
        self._tracker_refresh()

    def _tracker_probe_page(self):
        url = self.entry_page_url.get().strip()
        if not url:
            messagebox.showwarning("Thiếu link", "Nhập link Facebook của page trước.")
            return
        account = self._selected_account()

        def work():
            try:
                posts = self.tracker_runtime.probe(url, account.name if account else None)
                lines = [
                    f"{datetime.fromtimestamp(p.posted_at).strftime('%d/%m %H:%M') if p.posted_at else '?':>12}  "
                    f"{p.views:>10,} lượt xem  {p.url}"
                    for p in sorted(posts, key=lambda p: p.posted_at or 0, reverse=True)
                ]
                text = f"Thấy {len(posts)} reel:\n\n" + "\n".join(lines[:15])
                self.dispatch_ui(lambda: messagebox.showinfo("Kết quả đọc thử", text))
            except Exception as e:
                msg = str(e)
                self.dispatch_ui(lambda: messagebox.showerror("Đọc thử lỗi", msg))
            self.dispatch_ui(self._tracker_refresh)

        threading.Thread(target=work, name="probe-page", daemon=True).start()
