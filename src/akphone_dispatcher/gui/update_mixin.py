"""Header button + dialogs for self-update from GitHub Releases."""

import threading
from tkinter import messagebox
from typing import Optional

import customtkinter as ctk

from akphone_dispatcher import __version__
from akphone_dispatcher.services.updater import (
    ReleaseInfo,
    can_self_update,
    current_exe,
    download,
    fetch_latest,
    is_newer,
    launch_swap,
)
from akphone_dispatcher.utils.logger import logger

UPDATE_FIRST_CHECK_MS = 8000
UPDATE_CHECK_EVERY_MS = 6 * 3600 * 1000
IDLE_TEXT = "🔔 Kiểm tra cập nhật"


class UpdateMixin:
    """Checks for a new release at startup and every few hours; one click installs it."""

    def _create_update_button(self, parent):
        self._update_info: Optional[ReleaseInfo] = None
        self._update_prompted = set()
        self._update_busy = False
        self.btn_update = ctk.CTkButton(
            parent,
            text=IDLE_TEXT,
            width=150,
            height=32,
            fg_color="#374151",
            command=self._update_button_clicked,
        )
        self.btn_update.pack(side="left", padx=(0, 10))
        self.after(UPDATE_FIRST_CHECK_MS, self._periodic_update_check)

    def _periodic_update_check(self):
        self._check_update_async(manual=False)
        self.after(UPDATE_CHECK_EVERY_MS, self._periodic_update_check)

    def _update_button_clicked(self):
        if self._update_busy:
            return
        if self._update_info is not None:
            self._prompt_update(self._update_info)
        else:
            self._check_update_async(manual=True)

    def _check_update_async(self, manual: bool):
        if self._update_busy:
            return
        self._update_busy = True
        if manual:
            self.btn_update.configure(text="⏳ Đang kiểm tra...")

        def work():
            info, error = None, None
            try:
                info = fetch_latest()
            except Exception as e:
                error = e
            self.dispatch_ui(lambda: self._on_update_checked(info, error, manual))

        threading.Thread(target=work, name="UpdateCheck", daemon=True).start()

    def _on_update_checked(self, info: Optional[ReleaseInfo], error, manual: bool):
        self._update_busy = False
        if error is not None:
            logger.warning(f"Không kiểm tra được bản cập nhật: {error}")
            self._reset_update_button()
            if manual:
                messagebox.showwarning("Cập Nhật", f"Không kiểm tra được bản cập nhật:\n{error}")
            return
        if info is None or not is_newer(info.version, __version__):
            self._update_info = None
            self._reset_update_button()
            if manual:
                messagebox.showinfo("Cập Nhật", f"Bạn đang dùng bản mới nhất (v{__version__}).")
            return
        self._update_info = info
        self._reset_update_button()
        if info.version not in self._update_prompted:
            logger.info(f"⬆️ Có bản cập nhật v{info.version} (đang dùng v{__version__}).")
        if manual or info.version not in self._update_prompted:
            self._prompt_update(info)

    def _reset_update_button(self):
        if self._update_info is not None:
            self.btn_update.configure(
                text=f"⬆️ Cập nhật v{self._update_info.version}",
                fg_color="#f59e0b",
                hover_color="#d97706",
                state="normal",
            )
        else:
            self.btn_update.configure(text=IDLE_TEXT, fg_color="#374151", state="normal")

    def _prompt_update(self, info: ReleaseInfo):
        self._update_prompted.add(info.version)
        notes = info.notes if len(info.notes) <= 1200 else info.notes[:1200] + "\n..."
        head = f"Có bản mới v{info.version} (đang dùng v{__version__})."
        if notes:
            head += f"\n\nThay đổi:\n{notes}"
        if not can_self_update():
            messagebox.showinfo(
                "Có Bản Cập Nhật",
                head + "\n\nĐang chạy từ mã nguồn: cập nhật bằng git pull.",
            )
            return
        busy = self.feeder_manager.is_running() or self.comment_tracker.is_alive()
        tail = (
            "\n\nTool sẽ tải bản mới (vẫn chạy bình thường trong lúc tải), sau đó tự dừng, "
            "lưu tiến độ, thay file và mở lại. Cài đặt, profile và tiến độ được giữ nguyên."
        )
        if busy:
            tail += "\n⚠️ Tool đang chạy: sau khi mở lại, khôi phục tiến độ rồi bấm chạy lại."
        if messagebox.askyesno("Có Bản Cập Nhật", head + tail + "\n\nCập nhật ngay?"):
            self._start_update_download(info)

    def _start_update_download(self, info: ReleaseInfo):
        self._update_busy = True
        self.btn_update.configure(text="⬇️ Đang tải 0%", state="disabled")
        exe = current_exe()
        dest = exe.with_name(exe.name + ".new")
        last_pct = [-1]

        def progress(done: int, total: int):
            pct = int(done * 100 / total) if total else 0
            if pct != last_pct[0]:
                last_pct[0] = pct
                self.dispatch_ui(lambda: self.btn_update.configure(text=f"⬇️ Đang tải {pct}%"))

        def work():
            try:
                download(info, dest, on_progress=progress)
            except Exception as e:
                logger.error(f"Tải bản cập nhật v{info.version} lỗi: {e}")
                self.dispatch_ui(lambda err=e: self._on_update_failed(err))
                return
            logger.info(f"⬆️ Đã tải xong bản v{info.version}; đang dừng tool để cài.")
            self.dispatch_ui(lambda: self._install_update(dest))

        threading.Thread(target=work, name="UpdateDownload", daemon=True).start()

    def _on_update_failed(self, error):
        self._update_busy = False
        self._reset_update_button()
        messagebox.showerror("Cập Nhật", f"Tải bản cập nhật lỗi:\n{error}\n\nThử lại sau.")

    def _install_update(self, new_exe):
        """Stop and save like the window X, then hand over to the swap script."""
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self.btn_update.configure(text="⏳ Đang cài...")
        try:
            self.lbl_global_status.configure(
                text="⏳ Đang dừng để cập nhật...", text_color="#f59e0b"
            )
        except Exception:
            pass
        threading.Thread(
            target=self._shutdown,
            kwargs={"before_exit": lambda: launch_swap(new_exe)},
            name="Shutdown",
            daemon=True,
        ).start()
