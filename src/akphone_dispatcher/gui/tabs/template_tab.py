"""TemplateTabMixin for the main window."""


from tkinter import messagebox

import customtkinter as ctk


ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")




class TemplateTabMixin:
    """Widgets and handlers of this tab (mixed into AKPhoneApp)."""

    def _build_tab_template(self):
        tab = self.tab_template

        note_box = ctk.CTkFrame(tab, corner_radius=6, fg_color="#181b24")
        note_box.pack(fill="x", padx=10, pady=(6, 2))
        ctk.CTkLabel(
            note_box,
            text=(
                "💡 TÙY BIẾN KỊCH BẢN ĐỘNG:\n"
                "• Mẫu kịch bản dùng chung cho toàn bộ dàn máy: template_script.txt & template_config.ini.\n"
                "• Biến thay thế tự động theo từng máy/via: {ID} (vd page1fb1, page1fb2), {PAGE_NAME}, {VIDEO_DIR}, {CAPTION_PATH}, {COMMENT_PATH}.\n"
                "• Biến cho phần comment: {LINK_PATH}, {CMT_MARKER}, {CMT_SCRIPT}, {CMT_MARKER_PATH}."
            ),
            font=ctk.CTkFont(size=12),
            text_color="#38bdf8",
            justify="left",
        ).pack(anchor="w", padx=12, pady=6)

        sub_tab = ctk.CTkTabview(tab)
        sub_tab.pack(fill="both", expand=True, padx=10, pady=4)

        t_txt = sub_tab.add("Kịch Bản .TXT (template_script.txt)")
        t_ini = sub_tab.add("Cấu Hình .INI (template_config.ini)")
        t_cmt = sub_tab.add("Kịch Bản Comment (template_cmt_script.txt)")

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

    def _load_templates_to_ui(self):
        txt_path = self.config.template_txt_path
        ini_path = self.config.template_ini_path
        txt_content = txt_path.read_text(encoding="utf-8") if txt_path.is_file() else ""
        ini_content = ini_path.read_text(encoding="utf-8") if ini_path.is_file() else ""
        self.tpl_txt_edit.delete("1.0", "end")
        self.tpl_txt_edit.insert("1.0", txt_content)
        self.tpl_ini_edit.delete("1.0", "end")
        self.tpl_ini_edit.insert("1.0", ini_content)
        cmt_path = self.config.template_cmt_path
        self.tpl_cmt_edit.delete("1.0", "end")
        self.tpl_cmt_edit.insert("1.0", cmt_path.read_text(encoding="utf-8") if cmt_path.is_file() else "")

    def _save_templates_from_ui(self):
        txt_content = self.tpl_txt_edit.get("1.0", "end-1c")
        ini_content = self.tpl_ini_edit.get("1.0", "end-1c")
        try:
            self.config.template_txt_path.write_text(txt_content, encoding="utf-8")
            self.config.template_ini_path.write_text(ini_content, encoding="utf-8")
            self.config.template_cmt_path.write_text(
                self.tpl_cmt_edit.get("1.0", "end-1c"), encoding="utf-8"
            )
            messagebox.showinfo("Đã Lưu Mẫu", "Đã lưu kịch bản mẫu ra file thành công!")
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không thể lưu file mẫu: {e}")
