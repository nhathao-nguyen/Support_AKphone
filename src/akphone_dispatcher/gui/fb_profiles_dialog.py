"""Popup: manage the Facebook profiles (login + optional proxy) used to scan the Reels tabs."""

import shutil
import threading
import time
from tkinter import messagebox, ttk
from typing import Optional

import customtkinter as ctk

from akphone_dispatcher.core.fb_profiles import parse_proxy, profile_proxy
from akphone_dispatcher.services.socks_relay import check_proxy

PROXY_LABELS = {"none": "Không dùng", "socks5": "SOCKS5", "http": "HTTP"}
PROXY_KINDS = {label: kind for kind, label in PROXY_LABELS.items()}


def profile_state(profile: dict, cooldown_until: float, now: float, current_name: str) -> str:
    if not profile.get("enabled", True):
        return "⏸️ Tắt"
    if profile.get("blocked_reason"):
        return f"🔒 Bị chặn: {profile['blocked_reason'][:60]}"
    if cooldown_until > now:
        return f"⏳ Proxy lỗi, nghỉ {int((cooldown_until - now) // 60) + 1} phút"
    if current_name and current_name == profile["name"]:
        return "🟢 Đang quét"
    return "✅ Sẵn sàng"


def account_label(profile: dict) -> str:
    if profile.get("fb_user"):
        return f"✅ {profile['fb_user']}"
    return "chưa đăng nhập" if profile.get("checked_at") else "chưa kiểm tra"


def proxy_label(profile: dict) -> str:
    try:
        spec = profile_proxy(profile)
    except ValueError:
        return "⚠ proxy sai"
    return spec.label() if spec else "Không proxy"


class FbProfilesDialog(ctk.CTkToplevel):
    """One window per app; ``app`` is the AKPhoneApp (store, tracker, helpers)."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.store = app.fb_profiles
        self.title("👤 Profile Facebook dùng để quét")
        self.geometry("1020x560")
        self.minsize(900, 500)
        self.transient(app)
        self.selected_id: Optional[str] = None

        ctk.CTkLabel(
            self,
            text=(
                "Mỗi profile = 1 tài khoản Facebook đăng nhập sẵn (+ proxy riêng nếu có). Chỉ profile ĐÃ ĐĂNG "
                "NHẬP mới được quét; không có profile nào đăng nhập thì tool không quét. Phiên hết hạn / bị "
                "chặn thì tự bỏ qua và báo để đăng nhập lại; proxy lỗi thì nghỉ 10 phút rồi thử lại. "
                "Đăng nhập bằng nút 🔐 để dùng đúng proxy của profile."
            ),
            wraplength=860,
            justify="left",
            text_color="#94a3b8",
        ).pack(anchor="w", padx=12, pady=(10, 4))

        columns = ("name", "account", "proxy", "state", "scans")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=9, selectmode="browse")
        for col, text, width in (
            ("name", "Tên profile", 150),
            ("account", "Tài khoản FB", 140),
            ("proxy", "Proxy", 200),
            ("state", "Trạng thái", 330),
            ("scans", "Lượt quét", 70),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=12, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        form = ctk.CTkFrame(self, fg_color="#1e222d")
        form.pack(fill="x", padx=12, pady=6)
        ctk.CTkLabel(form, text="Tên:").grid(row=0, column=0, padx=(10, 4), pady=6, sticky="w")
        self.entry_name = ctk.CTkEntry(form, width=170)
        self.entry_name.grid(row=0, column=1, padx=4, pady=6)
        ctk.CTkLabel(form, text="Loại proxy:").grid(row=0, column=2, padx=(12, 4), sticky="w")
        self.opt_kind = ctk.CTkOptionMenu(form, values=list(PROXY_KINDS), width=120)
        self.opt_kind.grid(row=0, column=3, padx=4)
        ctk.CTkLabel(form, text="Proxy:").grid(row=0, column=4, padx=(12, 4), sticky="w")
        self.entry_proxy = ctk.CTkEntry(form, width=260, placeholder_text="ip:port:user:pass")
        self.entry_proxy.grid(row=0, column=5, padx=4)
        self.var_enabled = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(form, text="Dùng để quét", variable=self.var_enabled).grid(
            row=0, column=6, padx=(12, 10)
        )

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=(0, 4))
        for text, color, command in (
            ("➕ Thêm mới", "#059669", self._add),
            ("💾 Lưu sửa", "#2563eb", self._save),
            ("🗑️ Xóa", "#b91c1c", self._delete),
            ("🧪 Kiểm tra proxy", "#0e7490", self._test_proxy),
            ("🔐 Đăng nhập", "#7c3aed", self._login),
            ("🔎 Kiểm tra đăng nhập", "#475569", self._check_login),
            ("✅ Bỏ chặn", "#d97706", self._unblock),
        ):
            ctk.CTkButton(buttons, text=text, width=118, fg_color=color, command=command).pack(
                side="left", padx=(0, 6)
            )

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=12, pady=(4, 10))
        ctk.CTkLabel(bottom, text="Đổi sang profile kế tiếp sau mỗi").pack(side="left")
        self.entry_rotate = ctk.CTkEntry(bottom, width=50)
        self.entry_rotate.pack(side="left", padx=4)
        self.entry_rotate.insert(0, str(self.store.rotate_every))
        ctk.CTkLabel(bottom, text="lượt mở trang (1 = mỗi lượt 1 profile)").pack(side="left")
        ctk.CTkButton(bottom, text="Lưu", width=60, command=self._save_rotate).pack(
            side="left", padx=8
        )
        ctk.CTkButton(bottom, text="Đóng", width=80, fg_color="#374151", command=self.destroy).pack(
            side="right"
        )

        self._refresh()
        self.after(100, self.lift)

    # ---------- table ----------
    def _pool(self):
        source = self.app.comment_tracker.active_source
        return source if hasattr(source, "cooldown") else None

    def _refresh(self, reschedule: bool = True):
        if not self.winfo_exists():
            return
        pool, now = self._pool(), time.time()
        profiles = self.store.list()
        wanted = {p["id"] for p in profiles}
        for iid in self.tree.get_children():
            if iid not in wanted:
                self.tree.delete(iid)
        for index, p in enumerate(profiles):
            values = (
                p["name"],
                account_label(p),
                proxy_label(p),
                profile_state(
                    p,
                    pool.cooldown.get(p["id"], 0) if pool else 0,
                    now,
                    pool.current_name if pool else "",
                ),
                (pool.scans.get(p["id"], 0) if pool else 0),
            )
            if self.tree.exists(p["id"]):
                self.tree.item(p["id"], values=values)
                self.tree.move(p["id"], "", index)
            else:
                self.tree.insert("", index, iid=p["id"], values=values)
        if reschedule:
            self.after(3000, self._refresh)

    def _on_select(self, _event=None):
        picked = self.tree.selection()
        if not picked:
            return
        profile = self.store.get(picked[0])
        if not profile:
            return
        self.selected_id = profile["id"]
        self.entry_name.delete(0, "end")
        self.entry_name.insert(0, profile["name"])
        self.opt_kind.set(PROXY_LABELS.get(profile.get("proxy_type", "none"), "Không dùng"))
        self.entry_proxy.delete(0, "end")
        self.entry_proxy.insert(0, profile.get("proxy", ""))
        self.var_enabled.set(bool(profile.get("enabled", True)))

    def _form(self):
        return {
            "name": self.entry_name.get().strip(),
            "proxy_type": PROXY_KINDS[self.opt_kind.get()],
            "proxy": self.entry_proxy.get().strip(),
            "enabled": bool(self.var_enabled.get()),
        }

    def _need_selection(self) -> Optional[dict]:
        profile = self.store.get(self.selected_id) if self.selected_id else None
        if not profile:
            messagebox.showwarning("Profile", "Chọn 1 profile trong bảng trước.", parent=self)
        return profile

    # ---------- actions ----------
    def _add(self):
        try:
            profile = self.store.add(**self._form())
        except ValueError as e:
            messagebox.showerror("Proxy không hợp lệ", str(e), parent=self)
            return
        self.selected_id = profile["id"]
        self._refresh(reschedule=False)
        self.tree.selection_set(profile["id"])
        messagebox.showinfo(
            "Đã thêm",
            f"Đã thêm profile '{profile['name']}'.\nBấm 🔐 Đăng nhập để đăng nhập Facebook cho profile này.",
            parent=self,
        )

    def _save(self):
        profile = self._need_selection()
        if not profile:
            return
        try:
            self.store.update(profile["id"], **self._form())
        except ValueError as e:
            messagebox.showerror("Proxy không hợp lệ", str(e), parent=self)
            return
        self._refresh(reschedule=False)

    def _delete(self):
        profile = self._need_selection()
        if not profile:
            return
        if not messagebox.askyesno(
            "Xóa profile", f"Xóa profile '{profile['name']}' khỏi danh sách quét?", parent=self
        ):
            return
        wipe = messagebox.askyesno(
            "Xóa dữ liệu đăng nhập",
            "Xóa luôn thư mục dữ liệu trình duyệt (phiên đăng nhập Facebook) của profile này?\n"
            "Chọn 'No' để giữ thư mục.",
            parent=self,
        )
        self.store.remove(profile["id"])
        self.selected_id = None
        if wipe:
            folder = self.store.dir_of(profile)
            self.app.run_with_tracker_paused(
                lambda: shutil.rmtree(folder, ignore_errors=True),
                f"đã xóa dữ liệu trình duyệt của profile '{profile['name']}'",
            )
        self._refresh(reschedule=False)

    def _test_proxy(self):
        try:
            spec = parse_proxy(PROXY_KINDS[self.opt_kind.get()], self.entry_proxy.get())
        except ValueError as e:
            messagebox.showerror("Proxy không hợp lệ", str(e), parent=self)
            return

        def work():
            try:
                ip = check_proxy(spec)
                text, ok = f"Proxy chạy tốt.\nIP ra ngoài: {ip}", True
            except Exception as e:
                text, ok = f"Không đi qua được proxy:\n{e}", False
            self.app.dispatch_ui(
                lambda: (messagebox.showinfo if ok else messagebox.showerror)(
                    "Kiểm tra proxy", text, parent=self
                )
            )

        threading.Thread(target=work, name="ProxyCheck", daemon=True).start()

    def _login(self):
        profile = self._need_selection()
        if not profile:
            return
        try:
            proxy = profile_proxy(profile)
        except ValueError as e:
            messagebox.showerror("Proxy không hợp lệ", str(e), parent=self)
            return
        if not messagebox.askyesno(
            "Đăng nhập Facebook",
            f"Mở trình duyệt cho profile '{profile['name']}'"
            + (f" qua {proxy.label()}" if proxy else " (không proxy)")
            + ".\nTheo dõi comment tạm dừng trong lúc này và tự bật lại sau khi bạn ĐÓNG trình duyệt.\n\n"
            "Đăng nhập tài khoản Facebook của profile này rồi đóng cửa sổ. Tiếp tục?",
            parent=self,
        ):
            return
        self.app.login_fb_profile(profile["id"])

    def _check_login(self):
        profile = self._need_selection()
        if profile:
            self.app.check_fb_profile(profile["id"])

    def _unblock(self):
        profile = self._need_selection()
        if not profile:
            return
        self.store.clear_blocked(profile["id"])
        source = self._pool()
        if source is not None:
            source.cooldown.pop(profile["id"], None)
        self._refresh(reschedule=False)

    def _save_rotate(self):
        try:
            value = int(self.entry_rotate.get().strip())
        except ValueError:
            messagebox.showerror("Lỗi", "Số lượt phải là số nguyên ≥ 1.", parent=self)
            return
        self.store.set_rotate_every(value)
        self.entry_rotate.delete(0, "end")
        self.entry_rotate.insert(0, str(self.store.rotate_every))
