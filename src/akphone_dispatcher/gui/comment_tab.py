"""Tab 5: delayed comment by view threshold (settings, login, job table)."""

import threading
from dataclasses import replace
from datetime import datetime
from tkinter import messagebox, ttk

import customtkinter as ctk

from akphone_dispatcher.core.comment_queue import STATE_LABELS
from akphone_dispatcher.services.fb_web import open_login_browser, parse_page_urls
from akphone_dispatcher.utils.logger import logger


class CommentTabMixin:
    """Mixed into AKPhoneApp; needs self.config, self.comment_queue, self.comment_tracker."""

    def _build_tab_comment(self):
        tab = self.tab_comment
        cfg = self.config.comment

        grp = ctk.CTkFrame(tab, corner_radius=6, fg_color="#1e222d")
        grp.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(
            grp,
            text=(
                "💬 COMMENT KHI ĐỦ LƯỢT XEM — bài vừa đăng: đọc tab Reels lấy link; bài đã có link: đọc view "
                "theo chu kỳ (1 lần mở trang đọc mọi bài của page). Đạt ngưỡng thì ghi linkcmt{ID}.txt + "
                "Comment_{ID}.txt vào SCRIPTS\\FILES và tạo cờ CMT/{ID}.txt trên máy. Việc đã xong sau 24h "
                "được lưu trữ sang configs\\comment_archive."
            ),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8",
            justify="left",
            wraplength=1080,
        ).pack(anchor="w", padx=10, pady=(6, 4))

        r1 = ctk.CTkFrame(grp, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=3)
        self.var_cmt_enabled = ctk.BooleanVar(value=cfg.enabled)
        ctk.CTkCheckBox(
            r1, text="Tự bật theo dõi khi mở tool", variable=self.var_cmt_enabled
        ).pack(side="left", padx=(0, 16))
        self.entry_cmt_threshold = self._cmt_number_field(r1, "Ngưỡng view:", cfg.view_threshold)
        self.entry_cmt_interval = self._cmt_number_field(
            r1, "Đọc view mỗi (phút):", cfg.views_interval_min
        )
        self.entry_cmt_expire = self._cmt_number_field(r1, "Hết hạn sau (giờ):", cfg.expire_hours)
        self.entry_cmt_timeout = self._cmt_number_field(
            r1, "Timeout cờ CMT (giây):", cfg.cmt_timeout_s
        )

        r2 = ctk.CTkFrame(grp, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(r2, text="Thư mục cờ CMT:", width=110, anchor="w").pack(side="left")
        self.entry_cmt_flag_dir = ctk.CTkEntry(r2, width=300)
        self.entry_cmt_flag_dir.pack(side="left", padx=(0, 16))
        self.entry_cmt_flag_dir.insert(0, cfg.flag_dir)
        ctk.CTkLabel(r2, text="Trình duyệt:", anchor="w").pack(side="left")
        self.entry_cmt_channel = ctk.CTkEntry(r2, width=90)
        self.entry_cmt_channel.pack(side="left", padx=(4, 16))
        self.entry_cmt_channel.insert(0, cfg.browser_channel)
        self.var_cmt_headless = ctk.BooleanVar(value=cfg.headless)
        ctk.CTkCheckBox(r2, text="Chạy ẩn khi quét", variable=self.var_cmt_headless).pack(
            side="left"
        )

        ctk.CTkLabel(
            grp,
            text="Link Fanpage (mỗi dòng: Tên Fanpage: https://www.facebook.com/...) — tên phải trùng tên trong dàn máy:",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(6, 0))
        self.txt_cmt_page_urls = ctk.CTkTextbox(
            grp, height=90, font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.txt_cmt_page_urls.pack(fill="x", padx=10, pady=(2, 4))
        self.txt_cmt_page_urls.insert("1.0", cfg.page_urls)

        r3 = ctk.CTkFrame(grp, fg_color="transparent")
        r3.pack(fill="x", padx=10, pady=(2, 8))
        buttons = [
            ("💾 Lưu", "#059669", self._save_comment_settings),
            ("📋 Điền tên Fanpage", "#475569", self._fill_comment_page_names),
            ("🔐 Đăng nhập Facebook", "#7c3aed", self._open_comment_login),
            ("▶️ Bật theo dõi", "#10b981", self._start_comment_tracker),
            ("⏹️ Dừng", "#ef4444", self._stop_comment_tracker),
            ("🔍 Quét ngay", "#0284c7", self._scan_comment_now),
            ("🧹 Xóa bài đã xong", "#d97706", self._clear_finished_comments),
        ]
        for text, color, command in buttons:
            ctk.CTkButton(r3, text=text, width=130, fg_color=color, command=command).pack(
                side="left", padx=(0, 6)
            )
        self.lbl_cmt_status = ctk.CTkLabel(r3, text="", text_color="#f59e0b")
        self.lbl_cmt_status.pack(side="left", padx=8)

        tree_frame = ctk.CTkFrame(tab, fg_color="#181b24")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        columns = ("page_id", "fanpage", "post", "state", "views", "url", "updated", "note")
        self.comment_tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        for col, title, width in (
            ("page_id", "Mã Page", 100),
            ("fanpage", "Fanpage", 160),
            ("post", "Bài", 90),
            ("state", "Trạng Thái", 170),
            ("views", "Lượt Xem", 80),
            ("url", "Link Reel", 260),
            ("updated", "Cập Nhật", 110),
            ("note", "Ghi Chú", 220),
        ):
            self.comment_tree.heading(col, text=title)
            self.comment_tree.column(col, width=width, anchor="w")
        v_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.comment_tree.yview)
        self.comment_tree.configure(yscrollcommand=v_scroll.set)
        self.comment_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        v_scroll.pack(side="left", fill="y", pady=8)

        self.after(1500, self._refresh_comment_tab)

    def _cmt_number_field(self, parent, label: str, value: int):
        ctk.CTkLabel(parent, text=label, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(parent, width=70)
        entry.pack(side="left", padx=(4, 14))
        entry.insert(0, str(value))
        return entry

    def _save_comment_settings(self) -> bool:
        try:
            self.config.comment = replace(
                self.config.comment,
                enabled=bool(self.var_cmt_enabled.get()),
                view_threshold=int(self.entry_cmt_threshold.get().strip()),
                views_interval_min=max(5, int(self.entry_cmt_interval.get().strip())),
                expire_hours=int(self.entry_cmt_expire.get().strip()),
                cmt_timeout_s=int(self.entry_cmt_timeout.get().strip()),
                flag_dir=self.entry_cmt_flag_dir.get().strip().rstrip("/"),
                headless=bool(self.var_cmt_headless.get()),
                browser_channel=self.entry_cmt_channel.get().strip(),
                page_urls=self.txt_cmt_page_urls.get("1.0", "end-1c").strip(),
            )
        except ValueError:
            messagebox.showerror("Lỗi", "Ngưỡng view, phút, giờ và timeout phải là số nguyên!")
            return False
        self.config.save()
        logger.info("💬 [Comment] Đã lưu cài đặt comment.")
        return True

    def _fill_comment_page_names(self):
        """Append a 'Name: ' line for every Fanpage of the farm that has no link yet."""
        known = parse_page_urls(self.txt_cmt_page_urls.get("1.0", "end-1c"))
        names = []
        for prof in self.device_profiles.values():
            for info in prof.pages_data.values():
                if info.fanpage_name not in known and info.fanpage_name not in names:
                    names.append(info.fanpage_name)
        if not names:
            messagebox.showinfo("Comment", "Mọi Fanpage đã khởi tạo đều có link (hoặc chưa khởi tạo máy nào).")
            return
        current = self.txt_cmt_page_urls.get("1.0", "end-1c").rstrip()
        lines = ([current] if current else []) + [f"{n}: " for n in names]
        self.txt_cmt_page_urls.delete("1.0", "end")
        self.txt_cmt_page_urls.insert("1.0", "\n".join(lines))

    def _open_comment_login(self):
        if not messagebox.askyesno(
            "Đăng Nhập Facebook",
            "Tool sẽ dừng theo dõi (nếu đang chạy) và mở trình duyệt.\n"
            "Đăng nhập Facebook bằng tài khoản xem được các Fanpage, rồi ĐÓNG cửa sổ trình duyệt.\n\nTiếp tục?",
        ):
            return
        tracker = self.comment_tracker
        channel = self.entry_cmt_channel.get().strip()

        def work():
            if tracker.is_alive():
                tracker.stop()
                tracker.join(timeout=90)  # the scan thread must release the browser profile
            try:
                logger.info("🔐 [Comment] Đang mở trình duyệt đăng nhập Facebook...")
                open_login_browser(self.config.browser_profile_dir, channel=channel)
                logger.info("🔐 [Comment] Đã đóng trình duyệt đăng nhập. Bấm 'Bật theo dõi' để chạy.")
            except Exception as e:
                logger.error(f"[Comment] Không mở được trình duyệt đăng nhập: {e}")

        threading.Thread(target=work, name="FbLogin", daemon=True).start()

    def _start_comment_tracker(self):
        if self._save_comment_settings():
            self.comment_tracker.start()

    def _stop_comment_tracker(self):
        self.comment_tracker.stop()

    def _scan_comment_now(self):
        if not self.comment_tracker.is_running():
            messagebox.showwarning("Comment", "Theo dõi chưa bật. Bấm 'Bật theo dõi' trước.")
            return
        self.comment_tracker.scan_now()

    def _clear_finished_comments(self):
        removed = self.comment_queue.clear_finished()
        logger.info(f"🧹 [Comment] Đã xóa {removed} bài đã xong/lỗi/hết hạn khỏi danh sách.")
        self._refresh_comment_tab(reschedule=False)

    def _refresh_comment_tab(self, reschedule: bool = True):
        try:
            tracker = self.comment_tracker
            if tracker.blocked_reason:
                status = f"🔒 Tạm dừng quét: {tracker.blocked_reason}"
            elif tracker.is_running():
                st = tracker.status()
                status = (
                    f"🟢 Đang theo dõi · chờ link {st['posted']} · theo dõi view {st['linked']} · "
                    f"chờ script {st['marked']} · {st['scans_last_hour']} lượt mở trang/giờ qua"
                )
            else:
                status = "⏹️ Chưa bật"
            self.lbl_cmt_status.configure(text=status)

            jobs = self.comment_queue.snapshot()
            wanted = {job["uid"] for job in jobs}
            cache = self.__dict__.setdefault("_cmt_row_cache", {})
            for iid in self.comment_tree.get_children():
                if iid not in wanted:
                    self.comment_tree.delete(iid)
                    cache.pop(iid, None)
            for job in jobs:  # oldest first, each new row inserted on top
                stamp = job["finished_at"] or job["views_at"] or job["posted_at"] or job["fed_at"]
                values = (
                    job["page_id"],
                    job["fanpage_name"],
                    f"{job['post_num']} ({job['video_name']})",
                    STATE_LABELS.get(job["state"], job["state"]),
                    "-" if job["views"] is None else job["views"],
                    job["post_url"] or "-",
                    datetime.fromtimestamp(stamp).strftime("%d/%m %H:%M") if stamp else "-",
                    job["note"],
                )
                if cache.get(job["uid"]) == values:
                    continue  # unchanged row: skip the (slow) Treeview call
                if job["uid"] in cache:
                    self.comment_tree.item(job["uid"], values=values)
                else:
                    self.comment_tree.insert("", 0, iid=job["uid"], values=values)
                cache[job["uid"]] = values
        except Exception as e:
            logger.error(f"Lỗi làm mới tab Comment: {e}")
        if reschedule:
            self.after(5000, self._refresh_comment_tab)
