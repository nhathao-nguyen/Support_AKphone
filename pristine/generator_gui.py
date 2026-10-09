# -*- coding: utf-8 -*-
# Recovered from reverse/AKPhone_Dispatcher.exe; see ../REPORT.md.
"""
AKPhone Auto Script & Live Feeder System
Hệ thống Tự Động Hóa Kịch Bản & Bơm Bài Thông Minh Cho AKPhone
Hỗ trợ:
1. 1 Page = Đúng 1 Script duy nhất (không sinh rác file).
2. Tự động bơm bài tiếp theo qua giám sát ADB trong thời gian thực.
3. Toàn bộ thông số, đường dẫn, mẫu kịch bản đều có thể tùy chỉnh (KHÔNG HARDCODE).
"""
import os, sys, re, json, time, shutil, threading, subprocess
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
ctk.set_appearance_mode('Dark')
ctk.set_default_color_theme('blue')
DEFAULT_TXT_TEMPLATE = '''WAIT=1
CLOSE_PACKAGE=com.speedsoftware.rootexplorer
WAIT=1
OPEN_PACKAGE=com.speedsoftware.rootexplorer
WAIT=5
SENDBUTTON=HOME
WAIT=1
CLICKXY=410|1680
IF_SEARCHTEXT_CONTAINS={ID}|30|chodoi.txt|CONTINUE
ELSE=xoaanhmoi.txt|EXIT
PUSH_FILE={VIDEO_DIR}|/storage/emulated/0/DCIM/Camera
WAIT=4
COPY=/storage/emulated/0/DCIM/Camera|/storage/emulated/0/
WAIT=1
SENDBUTTON=HOME
WAIT=3
CLICKXY=666|1691
WAIT=1
IF_SEARCHTEXT_CONTAINS={ID}|30|chodoi.txt|CONTINUE
ELSE=xoaanhmoi.txt|EXIT
WAIT=1
CLICKTEXT_CONTAINS={ID}|14
WAIT=1
CLICKXY=69|576
WAIT=4
OPEN_PACKAGE=com.android.gallery3d
WAIT=8
OPEN_PACKAGE=com.facebook.katana
IF_SEARCHTEXT_CONTAINS=Bạn đang nghĩ gì?|32|chodoi.txt|CONTINUE
ELSE=molai.txt|CONTINUE
WAIT=8
CLICKXY=984|254
CLICKTEXT_CONTAINS=Hãy mở công cụ chuyển trang cá nhân|30
WAIT=5
SWIPE=1050|1872|1065|690|100
CLICKTEXT_CONTAINS=Xem tất cả|10
CLICKTEXT_CONTAINS=Biểu tượng tìm kiếm trên thanh điều hướng|10
CLICKTEXT_CONTAINS=Tìm kiếm trang cá nhân và Trang|10
SENDTEXT={PAGE_NAME}
CLICKTEXT_CONTAINS=Trang|30
IF_SEARCHTEXT_CONTAINS=Bạn đang nghĩ gì?|32|chodoi.txt|CONTINUE
ELSE=molai.txt|CONTINUE
WAIT=5
CLICKTEXT_CONTAINS=Bạn đang nghĩ gì?|9
IF_SEARCHTEXT_CONTAINS=Thư viện|32|chodoi.txt|CONTINUE
ELSE=molai.txt|CONTINUE
CLICKTEXT_CONTAINS=Thư viện|9
WAIT=5
CLICKXY=186|612
IF_SEARCHTEXT_CONTAINS=Bạn đang nghĩ gì?|32|chodoi.txt|CONTINUE
ELSE=molai.txt|CONTINUE
WAIT=5
CLICKTEXT_CONTAINS=Bạn đang nghĩ gì?|9
WAIT=5
GETDATA_AZ={CAPTION_PATH}
WAIT=5
SENDDATA=VALUE_0
WAIT=5
CLICKXY=948|103
WAIT=5
CLICKTEXT_CONTAINS=Tiếp|9
WAIT=5
CLICKTEXT_CONTAINS=Chia sẻ ngay|9
IF_SEARCHTEXT_CONTAINS=Vừa xong|300|chodoi.txt|CONTINUE
ELSE=molai.txt|CONTINUE
CLICKTEXT_CONTAINS=Vừa xong|9
CLICKTEXT_CONTAINS=phút|9
IF_SEARCHTEXT_CONTAINS={PAGE_NAME}|10|chodoi.txt|CONTINUE
ELSE=xoaanhmoi.txt|CONTINUE
CLICKTEXT_CONTAINS=ình luận|9
GETDATA_AZ={COMMENT_PATH}
WAIT=5
SENDDATA=VALUE_0
WAIT=5
CLICKXY=1021|1063
WAIT=5
CLOSE_PACKAGE=com.facebook.katana
WAIT=5
OPEN_PACKAGE=com.facebook.katana
WAIT=8
CLICKXY=984|254
WAIT=3
CLICKXY=805|454
CLICKTEXT_CONTAINS=hãy chuyển sang trang cá nhân của bạn|10
WAIT=5
OPEN_PACKAGE=com.android.gallery3d
WAIT=5
SWIPE=190|568|190|568|3000
CLICKXY=567|513
CLICKXY=860|535
CLICKXY=164|1000
CLICKXY=523|974
CLICKXY=893|989
CLICKXY=164|1318
CLICKXY=545|1259
CLICKXY=904|1240
CLICKXY=164|1595
CLICKXY=483|1595
CLICKTEXT_CONTAINS=Xóa|10
CLICKTEXT_CONTAINS=OK|10
WAIT=5
CLOSE_PACKAGE=com.android.gallery3d
WAIT=1
REMOVE=/storage/emulated/0/Movies
WAIT=1
REMOVE=/storage/emulated/0/Pictures
WAIT=1
REMOVE=/storage/emulated/0/DCIM/Camera
WAIT=1
REMOVE=/storage/emulated/0/DCIM/.thumbnails
WAIT=1
REMOVE=/storage/emulated/0/Camera
WAIT=2
REMOVE=/storage/emulated/0/Android/Done/{ID}.txt'''
DEFAULT_INI_TEMPLATE = '''[OFFER_CONFIG]
CFOfferType=1
CFUrl=
CFPackage=com.facebook.katana
CFTyleRRS=100
CFTimeoutLoadLink=150
CFTimeoutDownload=300
CFScript={ID}.txt
CFScript_RRS={ID}.txt
CFPathAPK=/data/local/tmp/AK_APK
CFLimitLead=0
'''
if getattr(sys, 'frozen', False):
    APP_BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE_PATH = os.path.join(APP_BASE_DIR, 'app_settings.json')
TEMPLATE_TXT_PATH = os.path.join(APP_BASE_DIR, 'template_script.txt')
TEMPLATE_INI_PATH = os.path.join(APP_BASE_DIR, 'template_config.ini')

def natural_sort_key(s):
    """Sắp xếp tự nhiên số trong chuỗi: vd vd1, vd2, ..., vd10"""
    return [int(text) if text.isdigit() else text.lower() for text in re.split('(\\d+)', str(s))]

class AKPhoneFeederApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title('AKPhone Smart Dispatcher & Script Generator (1 Page = 1 Script)')
        self.geometry('1000 x 760')
        self.minsize(920, 680)
        self.base_dir = APP_BASE_DIR
        self.feeder_running = False
        self.feeder_thread = None
        self.stop_event = threading.Event()
        self.pages_data = {}
        self.device_state = {}
        self._load_saved_settings()
        self._create_layout()
        self._check_adb_status_async()

    def _load_saved_settings(self):
        """Nạp cài đặt từ file JSON nếu có, tránh phải nhập lại mỗi lần mở"""
        defaults = {'source_dir': os.path.join(self.base_dir, 'test-folder'), 'akphone_dir': 'C:\\AKPhone', 'adb_path': 'C:\\AKPhone\\ADB\\adb.exe', 'running_dir': 'C:\\AK_Running', 'm_index': '1', 'check_interval': '2.0', 'page_names': 'page-1: Reel of Life\npage-2: Behind Closed Doors\npage-3: The Turning Point\npage-4: Unmasked Truth\npage-5: Cold Truth'}
        self.settings = defaults
        if os.path.exists(CONFIG_FILE_PATH):
            try:
                with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                    self.settings.update(saved)
            except Exception:
                pass

    def _save_settings(self):
        """Lưu lại cài đặt hiện tại"""
        try:
            self.settings['source_dir'] = self.entry_source_dir.get().strip()
            self.settings['akphone_dir'] = self.entry_akphone_dir.get().strip()
            self.settings['adb_path'] = self.entry_adb_path.get().strip()
            self.settings['running_dir'] = self.entry_running_dir.get().strip()
            self.settings['m_index'] = self.entry_m_index.get().strip()
            self.settings['check_interval'] = self.entry_interval.get().strip()
            self.settings['page_names'] = self.text_page_names.get('1.0', 'end-1c').strip()
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def _create_layout(self):
        header = ctk.CTkFrame(self, corner_radius=8, fg_color='#181b24')
        header.pack(fill='x', padx=12, pady=(10, 4))
        title = ctk.CTkLabel(header, text='⚡ AKPHONE DISPATCHER & AUTO FEEDER', font=ctk.CTkFont(family='Segoe UI', size=18, weight='bold'), text_color='#38bdf8')
        title.pack(side='left', padx=14, pady=8)
        self.lbl_device_status = ctk.CTkLabel(header, text='📱 ADB: Đang kiểm tra...', font=ctk.CTkFont(size=13, weight='bold'), text_color='#f59e0b')
        self.lbl_device_status.pack(side='right', padx=14, pady=8)
        self.tabview = ctk.CTkTabview(self, corner_radius=8)
        self.tabview.pack(fill='both', expand=True, padx=12, pady=4)
        self.tab_setup = self.tabview.add('⚙️ 1. Cấu Hình & Tạo Kịch Bản Cố Định')
        self.tab_feeder = self.tabview.add('🤖 2. Tự Động Bơm Bài (Live Feeder)')
        self.tab_template = self.tabview.add('📝 3. Kịch Bản Mẫu (Template)')
        self._build_tab_setup()
        self._build_tab_feeder()
        self._build_tab_template()
        log_box = ctk.CTkFrame(self, corner_radius=8, fg_color='#111827')
        log_box.pack(fill='x', padx=12, pady=(4, 10))
        log_head = ctk.CTkFrame(log_box, fg_color='transparent')
        log_head.pack(fill='x', padx=8, pady=(4, 2))
        ctk.CTkLabel(log_head, text='📋 Nhật ký hoạt động:', font=ctk.CTkFont(size=12, weight='bold'), text_color='#94a3b8').pack(side='left')
        ctk.CTkButton(log_head, text='Xóa Log', width=55, height=20, font=ctk.CTkFont(size=11), fg_color='#374151', command=lambda: self.log_text.delete('1.0', 'end')).pack(side='right')
        self.log_text = ctk.CTkTextbox(log_box, height=85, font=ctk.CTkFont(family='Consolas', size=11))
        self.log_text.pack(fill='both', padx=8, pady=(2, 6))

    def _build_tab_setup(self):
        tab = self.tab_setup
        grp_paths = ctk.CTkFrame(tab, corner_radius=6, fg_color='#1e222d')
        grp_paths.pack(fill='x', padx=10, pady=6)
        ctk.CTkLabel(grp_paths, text='📁 CẤU HÌNH ĐƯỜNG DẪN HỆ THỐNG (TÙY CHỈNH)', font=ctk.CTkFont(size=13, weight='bold'), text_color='#38bdf8').pack(anchor='w', padx=10, pady=(6, 4))
        r1 = ctk.CTkFrame(grp_paths, fg_color='transparent')
        r1.pack(fill='x', padx=10, pady=3)
        ctk.CTkLabel(r1, text='Kho bài viết gốc (Source):', width=170, anchor='w').pack(side='left')
        self.entry_source_dir = ctk.CTkEntry(r1)
        self.entry_source_dir.pack(side='left', fill='x', expand=True, padx=6)
        self.entry_source_dir.insert(0, self.settings['source_dir'])
        ctk.CTkButton(r1, text='Chọn...', width=70, command=self._browse_source_dir).pack(side='left')
        r2 = ctk.CTkFrame(grp_paths, fg_color='transparent')
        r2.pack(fill='x', padx=10, pady=3)
        ctk.CTkLabel(r2, text='Thư mục AKPhone:', width=170, anchor='w').pack(side='left')
        self.entry_akphone_dir = ctk.CTkEntry(r2)
        self.entry_akphone_dir.pack(side='left', fill='x', expand=True, padx=6)
        self.entry_akphone_dir.insert(0, self.settings['akphone_dir'])
        ctk.CTkButton(r2, text='Chọn...', width=70, command=self._browse_akphone_dir).pack(side='left')
        r3 = ctk.CTkFrame(grp_paths, fg_color='transparent')
        r3.pack(fill='x', padx=10, pady=3)
        ctk.CTkLabel(r3, text='Đường dẫn adb.exe:', width=170, anchor='w').pack(side='left')
        self.entry_adb_path = ctk.CTkEntry(r3)
        self.entry_adb_path.pack(side='left', fill='x', expand=True, padx=6)
        self.entry_adb_path.insert(0, self.settings['adb_path'])
        ctk.CTkButton(r3, text='Kiểm Tra ADB', width=100, fg_color='#059669', hover_color='#047857', command=self._test_adb_button).pack(side='left')
        r4 = ctk.CTkFrame(grp_paths, fg_color='transparent')
        r4.pack(fill='x', padx=10, pady=(3, 8))
        ctk.CTkLabel(r4, text='Thư mục đệm (Running Dir):', width=170, anchor='w').pack(side='left')
        self.entry_running_dir = ctk.CTkEntry(r4)
        self.entry_running_dir.pack(side='left', fill='x', expand=True, padx=6)
        self.entry_running_dir.insert(0, self.settings['running_dir'])
        ctk.CTkLabel(r4, text='Chỉ số FB {m}:', width=85, anchor='w').pack(side='left', padx=(10, 2))
        self.entry_m_index = ctk.CTkEntry(r4, width=50)
        self.entry_m_index.pack(side='left')
        self.entry_m_index.insert(0, self.settings['m_index'])
        grp_pages = ctk.CTkFrame(tab, corner_radius=6, fg_color='#1e222d')
        grp_pages.pack(fill='both', expand=True, padx=10, pady=6)
        page_top = ctk.CTkFrame(grp_pages, fg_color='transparent')
        page_top.pack(fill='x', padx=10, pady=(6, 2))
        ctk.CTkLabel(page_top, text='📝 ÁNH XẠ TÊN FANPAGE FACEBOOK', font=ctk.CTkFont(size=13, weight='bold'), text_color='#38bdf8').pack(side='left')
        ctk.CTkButton(page_top, text='Quét Kho & Điền Mẫu', width=130, height=24, fg_color='#475569', command=self._scan_and_fill_pages).pack(side='right')
        self.text_page_names = ctk.CTkTextbox(grp_pages, height=90, font=ctk.CTkFont(family='Consolas', size=12))
        self.text_page_names.pack(fill='both', expand=True, padx=10, pady=(2, 6))
        self.text_page_names.insert('1.0', self.settings['page_names'])
        note_lbl = ctk.CTkLabel(grp_pages, text="* Cú pháp: Mỗi dòng ghi 'page-1: Tên Fanpage'. Hoặc chỉ cần gõ tên Fanpage lần lượt theo thứ tự từng dòng.", font=ctk.CTkFont(size=11), text_color='#94a3b8')
        note_lbl.pack(anchor='w', padx=10, pady=(0, 6))
        action_bar = ctk.CTkFrame(tab, fg_color='transparent')
        action_bar.pack(fill='x', padx=10, pady=(4, 6))
        self.btn_init = ctk.CTkButton(action_bar, text='🛠️ 1. KHỞI TẠO SCRIPTS CỐ ĐỊNH & NẠP BÀI SỐ 1 VÀO CHẠY', height=38, font=ctk.CTkFont(size=14, weight='bold'), fg_color='#0284c7', hover_color='#0369a1', command=self._initialize_scripts_and_first_post)
        self.btn_init.pack(fill='x')

    def _build_tab_feeder(self):
        tab = self.tab_feeder
        ctrl_frame = ctk.CTkFrame(tab, corner_radius=6, fg_color='#1e222d')
        ctrl_frame.pack(fill='x', padx=10, pady=8)
        top_r = ctk.CTkFrame(ctrl_frame, fg_color='transparent')
        top_r.pack(fill='x', padx=10, pady=(8, 4))
        ctk.CTkLabel(top_r, text='Chu kỳ quét (giây):', font=ctk.CTkFont(size=12)).pack(side='left', padx=(0, 4))
        self.entry_interval = ctk.CTkEntry(top_r, width=50)
        self.entry_interval.pack(side='left', padx=(0, 20))
        self.entry_interval.insert(0, self.settings['check_interval'])
        self.btn_toggle_feeder = ctk.CTkButton(top_r, text='▶️ BẬT TỰ ĐỘNG BƠM BÀI (AUTO FEEDER)', width=280, height=36, font=ctk.CTkFont(size=13, weight='bold'), fg_color='#10b981', hover_color='#059669', command=self._toggle_feeder)
        self.btn_toggle_feeder.pack(side='left', padx=(0, 10))
        btn_open_running = ctk.CTkButton(top_r, text='📂 Mở Thư Mục Đệm', width=130, height=36, fg_color='#475569', command=self._open_running_dir)
        btn_open_running.pack(side='left')
        manual_r = ctk.CTkFrame(ctrl_frame, fg_color='transparent')
        manual_r.pack(fill='x', padx=10, pady=(4, 8))
        ctk.CTkLabel(manual_r, text='Thủ công:', font=ctk.CTkFont(size=12, weight='bold'), text_color='#94a3b8').pack(side='left', padx=(0, 6))
        self.combo_page_select = ctk.CTkComboBox(manual_r, values=['(Chưa khởi tạo)'], width=130)
        self.combo_page_select.pack(side='left', padx=(0, 8))
        self.btn_manual_feed_one = ctk.CTkButton(manual_r, text='⏭️ Bơm Bài Kế Tiếp (Page Chọn)', width=200, height=30, fg_color='#3b82f6', hover_color='#2563eb', command=self._manual_feed_selected)
        self.btn_manual_feed_one.pack(side='left', padx=(0, 8))
        self.btn_reset_post1 = ctk.CTkButton(manual_r, text='🔄 Nạp Lại Bài 1 (Tất Cả)', width=160, height=30, fg_color='#64748b', hover_color='#475569', command=self._manual_reset_all_to_post1)
        self.btn_reset_post1.pack(side='left')
        table_frame = ctk.CTkFrame(tab, corner_radius=6, fg_color='#181b24')
        table_frame.pack(fill='both', expand=True, padx=10, pady=(0, 8))
        ctk.CTkLabel(table_frame, text='📊 BẢNG TIẾN ĐỘ THỜI GIAN THỰC (LIVE PROGRESS):', font=ctk.CTkFont(size=13, weight='bold'), text_color='#38bdf8').pack(anchor='w', padx=10, pady=(8, 4))
        self.text_progress = ctk.CTkTextbox(table_frame, font=ctk.CTkFont(family='Consolas', size=12))
        self.text_progress.pack(fill='both', expand=True, padx=10, pady=(0, 8))
        self.text_progress.insert('1.0', "Chưa khởi tạo. Hãy bấm 'Khởi tạo Scripts & Nạp Bài Số 1' ở Tab 1 trước.")

    def _build_tab_template(self):
        tab = self.tab_template
        note_box = ctk.CTkFrame(tab, corner_radius=6, fg_color='#181b24')
        note_box.pack(fill='x', padx=10, pady=(6, 2))
        ctk.CTkLabel(note_box, text="💡 TÙY BIẾN KỊCH BẢN MÀ KHÔNG CẦN BUILD LẠI FILE .EXE:\n• Mẫu kịch bản được tự động lưu ra file 'template_script.txt' ngay cạnh file .EXE.\n• Khi Facebook thay đổi giao diện/tọa độ, bạn chỉ cần sửa file .txt đó (bằng Notepad hoặc sửa trực tiếp dưới đây).\n• Các biến tự động thay thế: {ID}, {PAGE_NAME}, {VIDEO_DIR}, {CAPTION_PATH}, {COMMENT_PATH}.", font=ctk.CTkFont(size=12), text_color='#38bdf8', justify='left').pack(anchor='w', padx=12, pady=6)
        sub_tab = ctk.CTkTabview(tab)
        sub_tab.pack(fill='both', expand=True, padx=10, pady=4)
        t_txt = sub_tab.add('Kịch Bản .TXT (template_script.txt)')
        t_ini = sub_tab.add('Cấu Hình .INI (template_config.ini)')
        self.tpl_txt_edit = ctk.CTkTextbox(t_txt, font=ctk.CTkFont(family='Consolas', size=12))
        self.tpl_txt_edit.pack(fill='both', expand=True, padx=6, pady=6)
        self.tpl_ini_edit = ctk.CTkTextbox(t_ini, font=ctk.CTkFont(family='Consolas', size=12))
        self.tpl_ini_edit.pack(fill='both', expand=True, padx=6, pady=6)
        self._load_templates_to_ui()
        r = ctk.CTkFrame(tab, fg_color='transparent')
        r.pack(fill='x', padx=10, pady=(2, 6))
        ctk.CTkButton(r, text='💾 Lưu Mẫu Ra File .txt / .ini', width=210, fg_color='#0284c7', hover_color='#0369a1', command=self._save_templates_from_ui).pack(side='left', padx=(0, 10))
        ctk.CTkButton(r, text='📂 Tải Lại Từ File Mẫu', width=160, fg_color='#475569', hover_color='#334155', command=self._load_templates_to_ui).pack(side='left', padx=(0, 10))
        ctk.CTkButton(r, text='🔄 Khôi Phục Chuẩn Gốc', fg_color='#ef4444', hover_color='#dc2626', command=self._reset_templates).pack(side='left')

    def _load_templates_to_ui(self):
        """Đọc mẫu kịch bản từ file template_script.txt ngoài ổ đĩa nếu có"""
        txt_content = DEFAULT_TXT_TEMPLATE
        if os.path.exists(TEMPLATE_TXT_PATH):
            try:
                with open(TEMPLATE_TXT_PATH, 'r', encoding='utf-8') as f:
                    txt_content = f.read()
            except Exception:
                pass
        else:
            try:
                with open(TEMPLATE_TXT_PATH, 'w', encoding='utf-8') as f:
                    f.write(DEFAULT_TXT_TEMPLATE)
            except Exception:
                pass
        ini_content = DEFAULT_INI_TEMPLATE
        if os.path.exists(TEMPLATE_INI_PATH):
            try:
                with open(TEMPLATE_INI_PATH, 'r', encoding='utf-8') as f:
                    ini_content = f.read()
            except Exception:
                pass
        else:
            try:
                with open(TEMPLATE_INI_PATH, 'w', encoding='utf-8') as f:
                    f.write(DEFAULT_INI_TEMPLATE)
            except Exception:
                pass
        self.tpl_txt_edit.delete('1.0', 'end')
        self.tpl_txt_edit.insert('1.0', txt_content)
        self.tpl_ini_edit.delete('1.0', 'end')
        self.tpl_ini_edit.insert('1.0', ini_content)

    def _save_templates_from_ui(self, show_msg=True):
        """Lưu nội dung từ ô nhập ra file template_script.txt ngoài ổ đĩa"""
        txt_content = self.tpl_txt_edit.get('1.0', 'end-1c')
        ini_content = self.tpl_ini_edit.get('1.0', 'end-1c')
        try:
            with open(TEMPLATE_TXT_PATH, 'w', encoding='utf-8') as f:
                f.write(txt_content)
            with open(TEMPLATE_INI_PATH, 'w', encoding='utf-8') as f:
                f.write(ini_content)
            if show_msg:
                self._log('Đã lưu mẫu kịch bản ra template_script.txt và template_config.ini.')
                messagebox.showinfo('Đã Lưu Thành Công', f'Đã lưu mẫu kịch bản ra file ngoài:\n{TEMPLATE_TXT_PATH}\n\nLần sau bạn có thể sửa trực tiếp file .txt này bằng Notepad mà KHÔNG CẦN build lại Tool!')
        except Exception as e:
            if show_msg:
                messagebox.showerror('Lỗi', f'Không thể lưu file mẫu: {e}')

    def _reset_templates(self):
        if messagebox.askyesno('Xác nhận', 'Khôi phục mẫu kịch bản về chuẩn gốc ban đầu?'):
            self.tpl_txt_edit.delete('1.0', 'end')
            self.tpl_txt_edit.insert('1.0', DEFAULT_TXT_TEMPLATE)
            self.tpl_ini_edit.delete('1.0', 'end')
            self.tpl_ini_edit.insert('1.0', DEFAULT_INI_TEMPLATE)
            self._save_templates_from_ui(show_msg=False)
            self._log('Đã khôi phục template chuẩn gốc và lưu lại file mẫu.')
            return None

    def _log(self, msg):
        now = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert('end', f'[{now}] {msg}\n')
        self.log_text.see('end')

    def _browse_source_dir(self):
        d = filedialog.askdirectory(initialdir=self.entry_source_dir.get())
        if d:
            self.entry_source_dir.delete(0, 'end')
            self.entry_source_dir.insert(0, d)
            self._save_settings()
            return None

    def _browse_akphone_dir(self):
        d = filedialog.askdirectory(initialdir=self.entry_akphone_dir.get())
        if d:
            self.entry_akphone_dir.delete(0, 'end')
            self.entry_akphone_dir.insert(0, d)
            adb_guess = os.path.join(d, 'ADB', 'adb.exe')
            if os.path.exists(adb_guess):
                self.entry_adb_path.delete(0, 'end')
                self.entry_adb_path.insert(0, adb_guess)
            self._save_settings()
            return None

    def _open_running_dir(self):
        d = self.entry_running_dir.get().strip()
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def _reset_templates(self):
        if messagebox.askyesno('Xác nhận', 'Khôi phục mẫu kịch bản về chuẩn gốc ban đầu?'):
            self.tpl_txt_edit.delete('1.0', 'end')
            self.tpl_txt_edit.insert('1.0', DEFAULT_TXT_TEMPLATE)
            self.tpl_ini_edit.delete('1.0', 'end')
            self.tpl_ini_edit.insert('1.0', DEFAULT_INI_TEMPLATE)
            self._log('Đã khôi phục template chuẩn gốc.')
            return None

    def _check_adb_status_async(self):
        self.after(600, self._check_adb_background)

    def _test_adb_button(self):
        self._check_adb_background(show_msg=True)

    def _check_adb_background(self, show_msg=False):
        adb_exe = self.entry_adb_path.get().strip()
        threading.Thread(target=self._worker_adb_check, args=(adb_exe, show_msg), daemon=True).start()

    def _worker_adb_check(self, adb_exe, show_msg):
        if not os.path.exists(adb_exe):
            self.after(0, lambda: self.lbl_device_status.configure(text='❌ Không tìm thấy adb.exe', text_color='#ef4444'))
            if show_msg:
                self.after(0, lambda: messagebox.showerror('Lỗi ADB', f'Không tìm thấy file adb.exe tại:\n{adb_exe}'))
            return (False, [])
        try:
            res = subprocess.run([adb_exe, 'devices'], capture_output=True, text=True, timeout=5)
            lines = res.stdout.strip().splitlines()[1:]
            devices = [line.split()[0] for line in lines if 'device' in line and (not line.startswith('*'))]
            if devices:
                dev_str = ', '.join(devices)
                self.after(0, lambda: self.lbl_device_status.configure(text=f'🟢 Đã kết nối: {dev_str}', text_color='#10b981'))
                if show_msg:
                    self.after(0, lambda: messagebox.showinfo('ADB Kết Nối Tốt', f'Tìm thấy {len(devices)} thiết bị:\n{dev_str}'))
                return (True, devices)
            else:
                self.after(0, lambda: self.lbl_device_status.configure(text='⚠️ ADB bật nhưng chưa cắm máy', text_color='#f59e0b'))
                if show_msg:
                    self.after(0, lambda: messagebox.showwarning('Cảnh báo ADB', "Không có thiết bị nào đang kết nối ở chế độ 'device'."))
                return (True, [])
        except Exception as e:
            self.after(0, lambda: self.lbl_device_status.configure(text=f'❌ Lỗi ADB: {e}', text_color='#ef4444'))
            if show_msg:
                self.after(0, lambda: messagebox.showerror('Lỗi', f'Không thể thực thi ADB: {e}'))
            return (False, [])

    def _scan_source_directory(self):
        """Quét cấu trúc thư mục kho bài viết (test-folder/ hoặc tương tự)"""
        src = self.entry_source_dir.get().strip()
        if not os.path.exists(src):
            return {}
        pages_found = {}
        subdirs = sorted([d for d in os.listdir(src) if os.path.isdir(os.path.join(src, d))], key=natural_sort_key)
        for d in subdirs:
            p_path = os.path.join(src, d)
            v_dir = os.path.join(p_path, 'video')
            c_dir = os.path.join(p_path, 'caption')
            m_dir = os.path.join(p_path, 'comments')
            v_files = []
            if os.path.exists(v_dir):
                v_files = sorted([f for f in os.listdir(v_dir) if f.lower().endswith(('.mp4', '.mov', '.avi'))], key=natural_sort_key)
            c_files = []
            if os.path.exists(c_dir):
                c_files = sorted([f for f in os.listdir(c_dir) if f.lower().endswith('.txt')], key=natural_sort_key)
            m_files = []
            if os.path.exists(m_dir):
                m_files = sorted([f for f in os.listdir(m_dir) if f.lower().endswith('.txt')], key=natural_sort_key)
            total_posts = len(v_files)
            if total_posts > 0:
                posts_list = []
                for i in range(total_posts):
                    vf = os.path.join(v_dir, v_files[i])
                    cf = os.path.join(c_dir, c_files[i]) if i < len(c_files) else ''
                    mf = os.path.join(m_dir, m_files[i]) if i < len(m_files) else ''
                    posts_list.append({'post_num': i + 1, 'video_file': vf, 'caption_file': cf, 'comment_file': mf})
                pages_found[d] = posts_list
        return pages_found

    def _scan_and_fill_pages(self):
        """Tự động quét kho bài và điền mẫu ánh xạ tên page"""
        data = self._scan_source_directory()
        if not data:
            messagebox.showwarning('Thông báo', 'Không tìm thấy thư mục page nào có chứa video trong kho!')
            return None
        lines = []
        for p_name in data.keys():
            lines.append(f'{p_name}: Fanpage {p_name}')
        self.text_page_names.delete('1.0', 'end')
        self.text_page_names.insert('1.0', '\n'.join(lines))
        self._log(f"Đã quét thấy {len(data)} Page: {', '.join(data.keys())}")

    def _parse_page_names_mapping(self):
        """Phân tích ánh xạ tên Fanpage từ ô nhập"""
        mapping = {}
        raw = self.text_page_names.get('1.0', 'end-1c').strip().splitlines()
        for idx, line in enumerate(raw, start=1):
            line = line.strip()
            if not line:
                continue
            if ':' in line:
                parts = line.split(':', 1)
                p_key = parts[0].strip()
                p_val = parts[1].strip()
                mapping[p_key] = p_val
                continue
            mapping[f'page-{idx}'] = line
        return mapping

    def _initialize_scripts_and_first_post(self):
        self._save_settings()
        src_dir = self.entry_source_dir.get().strip()
        ak_dir = self.entry_akphone_dir.get().strip()
        running_base = self.entry_running_dir.get().strip()
        m_val = self.entry_m_index.get().strip() or '1'
        if not os.path.exists(src_dir):
            messagebox.showerror('Lỗi', 'Thư mục kho bài viết không tồn tại!')
            return None
        os.makedirs(running_base, exist_ok=True)
        scripts_dest = os.path.join(ak_dir, 'SCRIPTS')
        offers_dest = os.path.join(ak_dir, 'OFFERS')
        os.makedirs(scripts_dest, exist_ok=True)
        os.makedirs(offers_dest, exist_ok=True)
        os.makedirs(running_base, exist_ok=True)
        scripts_dest = os.path.join(ak_dir, 'SCRIPTS')
        offers_dest = os.path.join(ak_dir, 'OFFERS')
        scripts_files_dest = os.path.join(scripts_dest, 'FILES')
        os.makedirs(scripts_dest, exist_ok=True)
        os.makedirs(offers_dest, exist_ok=True)
        os.makedirs(scripts_files_dest, exist_ok=True)
        for f in os.listdir(scripts_dest):
            f_lower = f.lower()
            if f_lower.startswith('caption_') or f_lower.startswith('comment_') or f_lower.startswith('cap') or f_lower.startswith('cmt') or (f_lower == '.txt'):
                try:
                    os.remove(os.path.join(scripts_dest, f))
                except Exception:
                    pass
        scanned = self._scan_source_directory()
        if not scanned:
            messagebox.showerror('Lỗi', f'Không tìm thấy video nào trong: {src_dir}')
            return None
        page_names_map = self._parse_page_names_mapping()
        self._save_templates_from_ui(show_msg=False)
        txt_tpl = self.tpl_txt_edit.get('1.0', 'end-1c')
        ini_tpl = self.tpl_ini_edit.get('1.0', 'end-1c')
        self.pages_data = {}
        self._log(f'Bắt đầu khởi tạo cho {len(scanned)} Pages...')
        for folder_name, posts in scanned.items():
            m_num = re.search('\\d+', folder_name)
            n_num = m_num.group(0) if m_num else folder_name
            page_id = f'page{n_num}fb{m_val}'
            fanpage_name = page_names_map.get(folder_name) or page_names_map.get(f'page-{n_num}') or f'Fanpage {folder_name}'
            page_video_dir = os.path.join(running_base, page_id)
            os.makedirs(page_video_dir, exist_ok=True)
            page_caption_filename = f'Caption_{page_id}.txt'
            page_comment_filename = f'Comment_{page_id}.txt'
            ak_caption_path = os.path.join(scripts_files_dest, page_caption_filename)
            ak_comment_path = os.path.join(scripts_files_dest, page_comment_filename)
            final_txt = txt_tpl.replace('{ID}', page_id).replace('{PAGE_NAME}', fanpage_name).replace('{VIDEO_DIR}', page_video_dir).replace('{CAPTION_PATH}', page_caption_filename).replace('{COMMENT_PATH}', page_comment_filename)
            txt_file_path = os.path.join(scripts_dest, f'{page_id}.txt')
            with open(txt_file_path, 'w', encoding='utf-8') as f:
                f.write(final_txt)
            final_ini = ini_tpl.replace('{ID}', page_id).replace('{PAGE_NAME}', fanpage_name).replace('{VIDEO_DIR}', page_video_dir)
            ini_file_path = os.path.join(offers_dest, f'{page_id}.ini')
            with open(ini_file_path, 'w', encoding='utf-8') as f:
                f.write(final_ini)
            self.pages_data[page_id] = {'folder_name': folder_name, 'fanpage_name': fanpage_name, 'total_posts': len(posts), 'current_idx': 1, 'posts': posts, 'video_dir': page_video_dir, 'caption_destinations': [ak_caption_path], 'comment_destinations': [ak_comment_path]}
            self._feed_post_to_running(page_id, post_idx=1)
        page_ids = list(self.pages_data.keys())
        if hasattr(self, 'combo_page_select'):
            self.combo_page_select.configure(values=page_ids)
            if page_ids:
                self.combo_page_select.set(page_ids[0])
        self._update_progress_display()
        self._log(f'🎉 Đã khởi tạo hoàn tất {len(scanned)} kịch bản cố định vào {scripts_dest} (Không có file rác)!')
        self._log(f'👉 Toàn bộ Video đã nạp vào {running_base}\\{{page_id}}\\ và Caption/Comment nạp vào {scripts_files_dest}!')
        messagebox.showinfo('Khởi Tạo Thành Công', f"Đã tạo hoàn tất {len(scanned)} kịch bản cố định (1 Page = 1 Script):\n📁 Kịch bản: {scripts_dest}\n📁 Cấu hình: {offers_dest}\n📁 Caption & Comment: {scripts_files_dest}\n\n📁 Video đang chạy nằm trực tiếp tại:\n{running_base}\\{{page_id}}\\\n\nBây giờ bạn có thể sang Tab 2 bật 'Auto Feeder' và bấm START trên AKPhone!")

    def _feed_post_to_running(self, page_id, post_idx):
        """Bơm bài thứ post_idx: Video vào running/{page_id}, Caption & Comment vào AKPhone/SCRIPTS/FILES"""
        p_info = self.pages_data.get(page_id)
        if not p_info:
            return False
        posts = p_info['posts']
        if post_idx > len(posts):
            return False
        target = posts[post_idx - 1]
        v_src = target['video_file']
        c_src = target['caption_file']
        m_src = target['comment_file']
        v_dir = p_info['video_dir']
        os.makedirs(v_dir, exist_ok=True)
        for f in os.listdir(v_dir):
            fp = os.path.join(v_dir, f)
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except Exception:
                pass
        if v_src and os.path.exists(v_src):
            v_name = os.path.basename(v_src)
            shutil.copy2(v_src, os.path.join(v_dir, v_name))
        cap_dest_list = p_info.get('caption_destinations') or [p_info.get('caption_file')]
        if not isinstance(cap_dest_list, list):
            cap_dest_list = [cap_dest_list]
        for cap_dest in cap_dest_list:
            if not cap_dest:
                continue
            os.makedirs(os.path.dirname(cap_dest), exist_ok=True)
            if c_src and os.path.exists(c_src):
                with open(c_src, 'r', encoding='utf-8') as f_in:
                    c_text = ' '.join([line.strip() for line in f_in.read().splitlines() if line.strip()])
                with open(cap_dest, 'w', encoding='utf-8') as f_out:
                    f_out.write(c_text)
            else:
                with open(cap_dest, 'w', encoding='utf-8') as f:
                    f.write(f"Bài đăng #{post_idx} cho {p_info['fanpage_name']}")
        com_dest_list = p_info.get('comment_destinations') or [p_info.get('comment_file')]
        if not isinstance(com_dest_list, list):
            com_dest_list = [com_dest_list]
        for com_dest in com_dest_list:
            if not com_dest:
                continue
            os.makedirs(os.path.dirname(com_dest), exist_ok=True)
            if m_src and os.path.exists(m_src):
                with open(m_src, 'r', encoding='utf-8') as f_in:
                    m_text = ' '.join([line.strip() for line in f_in.read().splitlines() if line.strip()])
                with open(com_dest, 'w', encoding='utf-8') as f_out:
                    f_out.write(m_text)
            else:
                with open(com_dest, 'w', encoding='utf-8') as f:
                    f.write(f"Bình luận #{post_idx} cho {p_info['fanpage_name']}")
        p_info['current_idx'] = post_idx
        return True

    def _manual_feed_selected(self):
        """Bơm bài kế tiếp thủ công cho Page được chọn trong combobox"""
        pid = self.combo_page_select.get().strip()
        if not pid or pid not in self.pages_data:
            messagebox.showwarning('Cảnh báo', 'Vui lòng chọn Page hợp lệ từ danh sách!')
            return None
        info = self.pages_data[pid]
        c_idx = info['current_idx']
        total = info['total_posts']
        if c_idx < total:
            next_idx = c_idx + 1
            if self._feed_post_to_running(pid, next_idx):
                self._update_progress_display()
                self._log(f'👉 [Thủ công] Đã nạp bài {next_idx}/{total} (Video, Caption, Comment) cho [{pid}]!')
                messagebox.showinfo('Thành Công', f'Đã nạp bài {next_idx}/{total} cho {pid}!')
                return None
            return None
        messagebox.showinfo('Hoàn Tất', f'Page [{pid}] đã ở bài cuối cùng ({total}/{total})!')

    def _manual_reset_all_to_post1(self):
        """Nạp lại bài số 1 cho tất cả các Page"""
        if not self.pages_data:
            messagebox.showwarning('Cảnh báo', 'Chưa có dữ liệu Page nào để nạp lại!')
            return None
        if messagebox.askyesno('Xác nhận', 'Bạn có chắc muốn nạp lại Bài số 1 cho tất cả các Page không?'):
            for pid in self.pages_data.keys():
                self._feed_post_to_running(pid, post_idx=1)
            self._update_progress_display()
            self._log('🔄 Đã nạp lại Bài số 1 cho toàn bộ các Page!')
            messagebox.showinfo('Thành Công', 'Đã nạp lại Bài số 1 cho toàn bộ các Page!')
            return None

    def _update_progress_display(self):
        """Cập nhật hiển thị bảng tiến độ thời gian thực"""
        self.text_progress.delete('1.0', 'end')
        if not self.pages_data:
            self.text_progress.insert('1.0', 'Chưa có dữ liệu. Vui lòng bấm khởi tạo ở Tab 1.')
            return None
        lines = []
        lines.append(f"{'MÃ SCRIPT':<14} | {'TÊN FANPAGE':<24} | {'TIẾN ĐỘ':<12} | {'TRẠNG THÁI HIỆN TẠI'}")
        lines.append('---------------------------------------------------------------------------')
        for pid, info in self.pages_data.items():
            c_idx = info['current_idx']
            total = info['total_posts']
            p_name = info['fanpage_name'][:22]
            status = f'Sẵn sàng (Đã nạp Bài {c_idx})' if c_idx <= total else '🎉 ĐÃ HOÀN TẤT HẾT KHO!'
            lines.append(f"{pid:<14} | {p_name:<24} | {f'{c_idx}/{total}':<12} | {status}")
        self.text_progress.insert('1.0', '\n'.join(lines))

    def _toggle_feeder(self):
        if not self.feeder_running:
            if not self.pages_data:
                messagebox.showwarning('Chưa Khởi Tạo', "Vui lòng bấm '1. Khởi Tạo Scripts' ở Tab 1 trước khi bật Auto Feeder!")
                return None
            self._start_feeder()
            return None
        self._stop_feeder()

    def _start_feeder(self):
        self.feeder_running = True
        self.stop_event.clear()
        self.btn_toggle_feeder.configure(text='⏹️ DỪNG GIÁM SÁT TỰ ĐỘNG', fg_color='#ef4444', hover_color='#dc2626')
        self.feeder_thread = threading.Thread(target=self._feeder_worker_loop, daemon=True)
        self.feeder_thread.start()
        self._log('🚀 ĐÃ BẬT TIẾN TRÌNH GIÁM SÁT ADB TỰ ĐỘNG BƠM BÀI!')

    def _stop_feeder(self):
        self.feeder_running = False
        self.stop_event.set()
        self.btn_toggle_feeder.configure(text='▶️ BẬT TỰ ĐỘNG BƠM BÀI (AUTO FEEDER)', fg_color='#10b981', hover_color='#059669')
        self._log('⏹️ Đã dừng tiến trình giám sát ADB.')

    def _feeder_worker_loop(self):
        """Vòng lặp chạy ngầm giám sát điện thoại qua ADB và tự động bơm bài tiếp theo"""
        adb_exe = self.entry_adb_path.get().strip()
        try:
            interval = float(self.entry_interval.get().strip())
        except Exception:
            interval = 2.0
        has_video_state = False
        active_running_page = None
        while not self.stop_event.is_set():
            time.sleep(interval)
            try:
                res_dev = subprocess.run([adb_exe, 'devices'], capture_output=True, text=True, timeout=4)
                dev_lines = res_dev.stdout.strip().splitlines()[1:]
                devices = [line.split()[0] for line in dev_lines if 'device' in line and (not line.startswith('*'))]
            except Exception:
                devices = []
            if not devices:
                continue
            target_dev = devices[0]
            try:
                check_cmd = [adb_exe, '-s', target_dev, 'shell', 'ls /storage/emulated/0/DCIM/Camera']
                res = subprocess.run(check_cmd, capture_output=True, text=True, timeout=4)
                files_in_camera = [f.strip() for f in res.stdout.strip().splitlines() if f.strip() and 'No such' not in f]
                count = len(files_in_camera)
                if count > 0 and (not has_video_state):
                    has_video_state = True
                    pushed_file = files_in_camera[0]
                    detected_pid = None
                    for pid, p_info in self.pages_data.items():
                        v_dir = p_info.get('video_dir', '')
                        if os.path.exists(v_dir) and pushed_file in os.listdir(v_dir):
                            detected_pid = pid
                            break
                        if pid.lower() in pushed_file.lower():
                            detected_pid = pid
                            break
                    active_running_page = detected_pid
                    page_desc = f' của [{active_running_page}]' if active_running_page else ''
                    self._log(f'📱 Phát hiện AKPhone vừa PUSH video{page_desc} vào điện thoại ({pushed_file}). Đang đăng...')
                elif count == 0 and has_video_state:
                    has_video_state = False
                    done_page_desc = f' cho [{active_running_page}]' if active_running_page else ''
                    self._log(f'🎉 Phát hiện Camera đã được dọn sạch! AKPhone vừa hoàn tất bài đăng{done_page_desc}!')
                    if active_running_page and active_running_page in self.pages_data:
                        pages_to_feed = [active_running_page]
                    else:
                        pages_to_feed = [p for p, info in self.pages_data.items() if info['current_idx'] < info['total_posts']]
                    for pid in pages_to_feed:
                        info = self.pages_data[pid]
                        c_idx = info['current_idx']
                        total = info['total_posts']
                        if c_idx < total:
                            next_idx = c_idx + 1
                            if self._feed_post_to_running(pid, next_idx):
                                self._log(f'⚡ ĐÃ TỰ ĐỘNG BƠM BÀI MỚI {next_idx}/{total} (Video, Caption, Comment) cho [{pid}]!')
                        else:
                            self._log(f'✅ [{pid}] đã hoàn tất toàn bộ {total} bài trong kho!')
                    active_running_page = None
                    self.after(0, self._update_progress_display)
            except Exception as e:
                pass
if __name__ == '__main__':
    app = AKPhoneFeederApp()
    app.mainloop()
