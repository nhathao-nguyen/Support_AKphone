"""Tab 5: delayed comment by view threshold (settings, login, job table grouped by page)."""

from dataclasses import replace
from datetime import datetime
import threading
from tkinter import messagebox, ttk
from typing import Dict, List, Tuple
import webbrowser

import customtkinter as ctk

from akphone_dispatcher.core.comment_queue import (
    DONE,
    EXPIRED,
    FAILED,
    FED,
    LINKED,
    MARKED,
    POSTED,
    REPOST,
    REPOST_STAGED,
    STATE_LABELS,
)
from akphone_dispatcher.core.fb_profiles import profile_proxy
from akphone_dispatcher.gui.fb_profiles_dialog import FbProfilesDialog
from akphone_dispatcher.services.fb_pool import refresh_logins
from akphone_dispatcher.services.fb_web import check_profile_login, open_login_browser
from akphone_dispatcher.utils.helpers import natural_sort_key
from akphone_dispatcher.utils.logger import logger

SHORT_STATE = {
    FED: "chờ đăng",
    POSTED: "chờ link",
    LINKED: "theo dõi",
    MARKED: "chờ script",
    DONE: "đã comment",
    FAILED: "lỗi",
    EXPIRED: "hết hạn",
    REPOST: "đăng lại",
    "suspect": "nghi đăng lỗi",
}
STATE_ORDER = (FED, POSTED, "suspect", REPOST, LINKED, MARKED, DONE, FAILED, EXPIRED)
HISTORY_DAYS = 7
COLUMNS = (
    ("state", "Trạng Thái", 190),
    ("views", "Lượt Xem", 80),
    ("link", "Link (reel / page)", 250),
    ("comment", "Nội Dung Comment", 230),
    ("updated", "Cập Nhật", 100),
    ("note", "Ghi Chú", 200),
)


def _stamp(job) -> float:
    return job["finished_at"] or job["views_at"] or job["posted_at"] or job["fed_at"] or 0


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "-"


def is_suspect(job, fast_attempts: int) -> bool:
    """Done flag removed, but the reel never showed up after the fast link tries."""
    return job["state"] == POSTED and (job.get("link_attempts") or 0) >= fast_attempts


def job_label(job, fast_attempts: int) -> str:
    if is_suspect(job, fast_attempts):
        return "⚠ Nghi đăng lỗi (không thấy reel)"
    if job["state"] == REPOST:
        if job.get("repost_state") == REPOST_STAGED:
            return "🔁 Đã nạp lại, chờ chạy script"
        return "🔁 Chờ nạp lại"
    return STATE_LABELS.get(job["state"], job["state"])


def _fmt_day(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m") if ts else "-"


def _summary(jobs, fast_attempts: int) -> str:
    counts = {s: 0 for s in STATE_ORDER}
    for job in jobs:
        key = "suspect" if is_suspect(job, fast_attempts) else job["state"]
        counts[key] = counts.get(key, 0) + 1
    return " · ".join(f"{n} {SHORT_STATE[s]}" for s, n in counts.items() if n)


def build_comment_rows(
    jobs, via_tags: Dict[str, str], fast_attempts: int = 6
) -> List[Tuple[str, str, str, tuple]]:
    """Rows (iid, parent iid, label, values) for the table: device -> page -> post.

    A page group is (device, page_id, Fanpage name): when a page_id is reused for a new
    page, the old posts stay under the old page name. Order is stable (devices by
    serial, pages by page_id, newest post first) so rows never jump around.
    """
    by_device: Dict[str, Dict[tuple, list]] = {}
    for job in jobs:
        page_key = (job["page_id"], job["fanpage_name"])
        by_device.setdefault(job["serial"], {}).setdefault(page_key, []).append(job)

    rows = []
    for serial in sorted(by_device):
        pages = by_device[serial]
        all_jobs = [j for page_jobs in pages.values() for j in page_jobs]
        dev_iid = f"dev|{serial}"
        rows.append(
            (
                dev_iid,
                "",
                f"📱 {serial} ({via_tags.get(serial, '?')}) · {len(pages)} page",
                (
                    _summary(all_jobs, fast_attempts),
                    "",
                    "",
                    "",
                    _fmt_time(max(map(_stamp, all_jobs))),
                    "",
                ),
            )
        )
        for page_id, name in sorted(pages, key=lambda k: (natural_sort_key(k[0]), k[1])):
            page_jobs = pages[(page_id, name)]
            newest = max(page_jobs, key=lambda j: j["fed_at"] or 0)
            page_url = newest.get("page_url", "")
            views = [j["views"] for j in page_jobs if j["views"] is not None]
            page_iid = f"pg|{serial}|{page_id}|{name}"
            rows.append(
                (
                    page_iid,
                    dev_iid,
                    f"{page_id} · {name}",
                    (
                        _summary(page_jobs, fast_attempts),
                        f"cao nhất {max(views)}" if views else "-",
                        page_url or "-",
                        "",
                        _fmt_time(max(map(_stamp, page_jobs))),
                        "" if page_url else "⚠ Chưa có link Fanpage (Tab 1)",
                    ),
                )
            )
            for job in sorted(page_jobs, key=lambda j: j["fed_at"] or 0, reverse=True):
                comment = job["comment_text"]
                rows.append(
                    (
                        job["uid"],
                        page_iid,
                        f"Bài {job['post_num']} ({job['video_name']}) · nạp {_fmt_day(job['fed_at'])}",
                        (
                            job_label(job, fast_attempts),
                            "-" if job["views"] is None else job["views"],
                            job["post_url"] or "-",
                            comment if len(comment) <= 60 else comment[:57] + "...",
                            _fmt_time(_stamp(job)),
                            job.get("verify_note") or job["note"],
                        ),
                    )
                )
    return rows


def toggle_view(
    running: bool, alive: bool, blocked: bool, checking: bool = False
) -> Tuple[str, str, str]:
    """(text, colour, state) of the tracker on/off button."""
    if checking:
        return "⏳ Đang kiểm tra tài khoản Facebook...", "#4b5563", "disabled"
    if running and blocked:
        return "🔒 TẠM DỪNG QUÉT · bấm để tắt", "#d97706", "normal"
    if running:
        return "🟢 ĐANG THEO DÕI · bấm để tắt", "#059669", "normal"
    if alive:
        return "⏳ Đang dừng...", "#4b5563", "disabled"
    return "▶️ BẬT THEO DÕI", "#374151", "normal"


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
                "Comment_{ID}.txt vào SCRIPTS\\FILES và tạo cờ CMT/{ID}.txt trên máy. Link Fanpage điền ở "
                "Tab 1, cạnh tên page của từng máy. Việc đã xong sau 24h được lưu trữ sang "
                "configs\\comment_archive."
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

        r3 = ctk.CTkFrame(grp, fg_color="transparent")
        r3.pack(fill="x", padx=10, pady=(2, 8))
        self.btn_cmt_toggle = ctk.CTkButton(
            r3, text="", width=230, font=ctk.CTkFont(weight="bold"),
            command=self._toggle_comment_tracker,
        )
        self.btn_cmt_toggle.pack(side="left", padx=(0, 6))
        self._update_cmt_toggle()
        buttons = [
            ("💾 Lưu", "#059669", self._save_comment_settings),
            ("👤 Profile Facebook", "#7c3aed", self._open_fb_profiles),
            ("🔍 Quét ngay", "#0284c7", self._scan_comment_now),
            ("🧹 Xóa bài đã xong", "#d97706", self._clear_finished_comments),
        ]
        for text, color, command in buttons:
            ctk.CTkButton(r3, text=text, width=130, fg_color=color, command=command).pack(
                side="left", padx=(0, 6)
            )
        self.lbl_cmt_status = ctk.CTkLabel(
            grp, text="", text_color="#f59e0b", anchor="w", justify="left", wraplength=1080
        )
        self.lbl_cmt_status.pack(fill="x", padx=10, pady=(0, 6))

        tree_frame = ctk.CTkFrame(tab, fg_color="#181b24")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        r_tree = ctk.CTkFrame(tree_frame, fg_color="transparent")
        r_tree.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkButton(
            r_tree, text="🔁 Đăng lại bài lỗi", width=140, fg_color="#7c3aed",
            command=self._repost_selected,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            r_tree, text="💬 Comment lại", width=120, fg_color="#0e7490",
            command=self._retry_comment_selected,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            r_tree, text="🔗 Nhận reel gợi ý", width=140, fg_color="#475569",
            command=self._accept_suggested_selected,
        ).pack(side="left", padx=(0, 12))
        self.var_cmt_history = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            r_tree, text=f"Hiện lịch sử {HISTORY_DAYS} ngày", variable=self.var_cmt_history,
            command=lambda: self._refresh_comment_tab(reschedule=False),
        ).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(
            r_tree,
            text="Chọn bài / page / máy (giữ Ctrl để chọn nhiều) · nhấp đúp để mở link",
            text_color="#94a3b8",
            anchor="w",
        ).pack(side="left")
        self.comment_tree = ttk.Treeview(
            tree_frame, columns=[c[0] for c in COLUMNS], show="tree headings",
            selectmode="extended",
        )
        self.comment_tree.heading("#0", text="Máy / Page / Bài")
        self.comment_tree.column("#0", width=260, anchor="w")
        for col, title, width in COLUMNS:
            self.comment_tree.heading(col, text=title)
            self.comment_tree.column(col, width=width, anchor="w")
        v_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.comment_tree.yview)
        self.comment_tree.configure(yscrollcommand=v_scroll.set)
        self.comment_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        v_scroll.pack(side="left", fill="y", pady=8)
        self.comment_tree.bind("<Double-1>", self._open_comment_row_link)

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
            )
        except ValueError:
            messagebox.showerror("Lỗi", "Ngưỡng view, phút, giờ và timeout phải là số nguyên!")
            return False
        self.config.save()
        logger.info("💬 [Comment] Đã lưu cài đặt comment.")
        return True

    def _open_fb_profiles(self):
        dialog = getattr(self, "_fb_profiles_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus()
            return
        self._fb_profiles_dialog = FbProfilesDialog(self)

    def run_with_tracker_paused(self, work, done_message: str = ""):
        """Run ``work`` in a thread with the scan browser closed, then resume the tracker
        if it was running (a browser profile folder can only be open once)."""
        tracker = self.comment_tracker
        was_running = tracker.is_running()

        def runner():
            if tracker.is_alive():
                tracker.stop()
                tracker.join(timeout=90)  # the scan thread must release the browser profile
            try:
                work()
                if done_message:
                    logger.info(f"👤 [Comment] {done_message[0].upper()}{done_message[1:]}.")
            except Exception as e:
                logger.error(f"[Comment] Lỗi: {e}")
            finally:
                if was_running:
                    self.dispatch_ui(lambda: self.start_tracker_checked(interactive=False))
                self.dispatch_ui(self._update_cmt_toggle)

        threading.Thread(target=runner, name="FbProfileTask", daemon=True).start()
        self.after(300, self._update_cmt_toggle)

    def login_fb_profile(self, profile_id: str):
        store = self.fb_profiles
        profile = store.get(profile_id)
        if not profile:
            return
        channel = self.entry_cmt_channel.get().strip()

        def work():
            logger.info(f"🔐 [Comment] Mở trình duyệt đăng nhập cho profile '{profile['name']}'...")
            proxy = profile_proxy(profile)
            open_login_browser(store.dir_of(profile), channel=channel, proxy=proxy)
            self._record_login(profile, check_profile_login(store.dir_of(profile), channel, proxy))

        self.run_with_tracker_paused(work)

    def check_fb_profile(self, profile_id: str):
        """Re-check one profile's login (tracker paused while its folder is open)."""
        store = self.fb_profiles
        profile = store.get(profile_id)
        if not profile:
            return
        channel = self.entry_cmt_channel.get().strip()
        self.run_with_tracker_paused(
            lambda: self._record_login(
                profile, check_profile_login(store.dir_of(profile), channel, profile_proxy(profile))
            )
        )

    def _record_login(self, profile: dict, fb_user: str):
        self.fb_profiles.set_account(profile["id"], fb_user)
        if fb_user:
            self.fb_profiles.clear_blocked(profile["id"])
            logger.info(f"👤 [Comment] Profile '{profile['name']}' đã đăng nhập Facebook (tài khoản {fb_user}).")
        else:
            logger.error(
                f"👤 [Comment] Profile '{profile['name']}' CHƯA đăng nhập Facebook: profile này sẽ không được dùng để quét."
            )

    def start_tracker_checked(self, interactive: bool = True):
        """Start the tracker only if at least one profile really has a Facebook login."""
        tracker = self.comment_tracker
        if tracker.is_alive() or getattr(self, "_checking_accounts", False):
            return
        store = self.fb_profiles
        channel = self.config.comment.browser_channel

        def checker(profile):
            return check_profile_login(store.dir_of(profile), channel, profile_proxy(profile))

        def work():
            with self._housekeeping_lock:  # browser caches are not being emptied
                pass
            try:
                ready = refresh_logins(store, checker)
            except Exception as e:
                logger.error(f"[Comment] Không kiểm tra được tài khoản Facebook: {e}")
                ready = []
            self.dispatch_ui(lambda: finish(ready))

        def finish(ready):
            self._checking_accounts = False
            if ready:
                logger.info(
                    f"👤 [Comment] {len(ready)} tài khoản Facebook sẵn sàng quét: "
                    + ", ".join(f"{p['name']} ({p['fb_user']})" for p in ready)
                )
                tracker.start()
            else:
                reason = (
                    "Không có tài khoản Facebook nào đăng nhập (hoặc tất cả đã hết hạn / bị chặn): "
                    "KHÔNG quét. Vào Tab 5 > Profile Facebook > Đăng nhập."
                )
                logger.error(f"🔒 [Comment] {reason}")
                self.notifier.send_message(f"🔒 <b>[Comment] Chưa quét được</b>\n{reason}")
                if interactive:
                    messagebox.showerror("Chưa Có Tài Khoản Facebook", reason)
                    self._open_fb_profiles()
            self._update_cmt_toggle()

        self._checking_accounts = True
        self._update_cmt_toggle()
        threading.Thread(target=work, name="FbLoginCheck", daemon=True).start()

    def _toggle_comment_tracker(self):
        tracker = self.comment_tracker
        if tracker.is_running():
            tracker.stop()
        elif not tracker.is_alive() and self._save_comment_settings():
            self.start_tracker_checked(interactive=True)
        self._update_cmt_toggle()
        self.after(1000, lambda: self._refresh_comment_tab(reschedule=False))

    def _update_cmt_toggle(self):
        tracker = self.comment_tracker
        text, color, state = toggle_view(
            tracker.is_running(), tracker.is_alive(), bool(tracker.blocked_reason),
            checking=getattr(self, "_checking_accounts", False),
        )
        self.btn_cmt_toggle.configure(text=text, fg_color=color, state=state)

    def _scan_comment_now(self):
        if not self.comment_tracker.is_running():
            messagebox.showwarning("Comment", "Theo dõi chưa bật. Bấm 'Bật theo dõi' trước.")
            return
        self.comment_tracker.scan_now()
        self._update_cmt_toggle()

    def _clear_finished_comments(self):
        removed = self.comment_queue.clear_finished()
        logger.info(f"🧹 [Comment] Đã chuyển {removed} bài đã xong/lỗi/hết hạn sang lưu trữ.")
        self._refresh_comment_tab(reschedule=False)

    def _selected_jobs(self) -> List[dict]:
        """Jobs under the selected rows (a page or device row selects all its posts)."""
        picked = set(self.comment_tree.selection())
        jobs = []
        for job in self.comment_queue.snapshot():
            page_iid = f"pg|{job['serial']}|{job['page_id']}|{job['fanpage_name']}"
            if picked & {job["uid"], page_iid, f"dev|{job['serial']}"}:
                jobs.append(job)
        return jobs

    def _repost_selected(self):
        selected = self._selected_jobs()
        fast = self.config.comment.link_fast_attempts
        jobs = [
            j for j in selected
            if self.comment_queue.can_repost(j) and (j["state"] != POSTED or is_suspect(j, fast))
        ]
        if not jobs:
            messagebox.showinfo(
                "Đăng Lại",
                "Không có bài đăng lỗi trong mục đã chọn.\n\nĐăng lại được: bài chưa đăng được, bài "
                "'⚠ Nghi đăng lỗi' (đã xóa cờ Done nhưng không thấy reel) hoặc hết hạn mà chưa có link, "
                "và còn bản lưu video.",
            )
            return
        busy = sorted({j["serial"] for j in jobs if self.feeder_manager.is_running(j["serial"])})
        lines = [f"• {j['page_id']} · {j['fanpage_name']} — bài {j['post_num']} ({j['video_name']})" for j in jobs]
        text = (
            f"Đăng lại {len(jobs)} bài:\n" + "\n".join(lines[:15])
            + ("\n..." if len(lines) > 15 else "")
            + "\n\nTool nạp lại video + caption (mỗi page 1 bài một lúc) và cắm cờ Done; "
            "bạn bấm chạy script page đó trên AKPhoneS. Nội dung comment giữ nguyên."
        )
        if busy:
            text += f"\n\n⚠ Máy {', '.join(busy)} đang chạy auto: tool chờ chạy xong mới nạp lại."
        if not self.comment_tracker.is_running():
            text += "\n\n⚠ Theo dõi đang tắt: bấm 'Bật theo dõi' để tool nạp lại."
        if not messagebox.askyesno("Đăng Lại Bài Lỗi", text):
            return
        queued = self.comment_queue.queue_repost({j["uid"] for j in jobs})
        logger.info(f"🔁 [Comment] Đã xếp {len(queued)} bài để đăng lại.")
        self._refresh_comment_tab(reschedule=False)

    def _retry_comment_selected(self):
        jobs = [j for j in self._selected_jobs() if j["state"] == FAILED and j.get("post_url")]
        if not jobs:
            messagebox.showinfo("Comment Lại", "Không có bài '❌ Comment lỗi' trong mục đã chọn.")
            return
        if not messagebox.askyesno(
            "Comment Lại",
            f"Đẩy lại link + comment cho {len(jobs)} bài comment lỗi?\n"
            "Mỗi page 1 bài một lúc, như lúc đủ view.",
        ):
            return
        retried = self.comment_queue.retry_comment({j["uid"] for j in jobs})
        logger.info(f"💬 [Comment] Đã đưa {len(retried)} bài về hàng comment lại.")
        self._refresh_comment_tab(reschedule=False)

    def _accept_suggested_selected(self):
        jobs = [j for j in self._selected_jobs() if j["state"] == POSTED and j.get("suggested_url")]
        if not jobs:
            messagebox.showinfo(
                "Nhận Reel Gợi Ý",
                "Không có bài nào có reel gợi ý trong mục đã chọn.\n\nReel gợi ý xuất hiện khi tool thấy "
                "reel đúng giờ nhưng khác caption, hoặc cùng caption nhưng lệch giờ (xem cột Ghi Chú).",
            )
            return
        lines = [f"• {j['page_id']} bài {j['post_num']} → {j['suggested_url']}" for j in jobs]
        if not messagebox.askyesno(
            "Nhận Reel Gợi Ý",
            "Gán các link sau cho bài (chỉ làm khi đã mở link và chắc chắn đúng video):\n\n"
            + "\n".join(lines[:15]) + ("\n..." if len(lines) > 15 else ""),
        ):
            return
        accepted = self.comment_queue.accept_suggestions({j["uid"] for j in jobs})
        logger.info(f"🔗 [Comment] Đã gán tay {len(accepted)} link theo reel gợi ý.")
        self._refresh_comment_tab(reschedule=False)

    def _open_comment_row_link(self, event):
        iid = self.comment_tree.identify_row(event.y)
        if not iid:
            return
        link = self.comment_tree.set(iid, "link")
        if link.startswith("http"):
            webbrowser.open(link)

    def _refresh_comment_tab(self, reschedule: bool = True):
        try:
            tracker = self.comment_tracker
            if tracker.blocked_reason:
                status = f"🔒 Tạm dừng quét: {tracker.blocked_reason}"
            elif tracker.is_running():
                st = tracker.status()
                status = (
                    f"chờ link {st['posted']} · reel chờ xác minh {st['verifying']} · "
                    f"theo dõi view {st['linked']} · chờ script {st['marked']} · "
                    f"{st['scans_last_hour']} lượt mở trang/giờ qua"
                    + (f" · profile: {st['profile']}" if st.get("profile") else "")
                    + (f" · ⚠ xác minh: {st['verify_note']}" if st.get("verify_note") else "")
                    + (f" · ⚠ đọc view trễ {st['views_lag_min']} phút" if st.get("views_lag_min") else "")
                    + (f" · ⌛ {st['rate_note']}" if st.get("rate_note") else "")
                )
            else:
                status = ""
            self.lbl_cmt_status.configure(text=status)
            self._update_cmt_toggle()

            via_tags = {serial: prof.via_tag for serial, prof in self.device_profiles.items()}
            jobs = self.comment_queue.snapshot()
            if self.var_cmt_history.get():
                live = {j["uid"] for j in jobs}
                jobs += [j for j in self.comment_queue.load_archived(HISTORY_DAYS) if j["uid"] not in live]
            rows = build_comment_rows(jobs, via_tags, self.config.comment.link_fast_attempts)
            self._apply_comment_rows(rows)
        except Exception as e:
            logger.error(f"Lỗi làm mới tab Comment: {e}")
        if reschedule:
            self.after(5000, self._refresh_comment_tab)

    def _apply_comment_rows(self, rows):
        """Update the tree in place: only changed rows touch the (slow) Treeview."""
        tree = self.comment_tree
        cache = self.__dict__.setdefault("_cmt_row_cache", {})
        wanted = {iid for iid, _, _, _ in rows}
        for iid in list(cache):
            if iid not in wanted:
                if tree.exists(iid):
                    tree.delete(iid)
                cache.pop(iid, None)

        position: Dict[str, int] = {}
        for iid, parent, label, values in rows:
            index = position.get(parent, 0)
            position[parent] = index + 1
            if iid not in cache:
                tree.insert(parent, index, iid=iid, text=label, values=values,
                            open=iid.startswith("dev|"))
            elif cache[iid] != (label, values):
                tree.item(iid, text=label, values=values)
            cache[iid] = (label, values)
