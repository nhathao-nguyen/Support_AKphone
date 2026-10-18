# ⚡ AKPhone Smart Dispatcher & Live Feeder (v2.0)

Hệ thống Tự Động Hóa Kịch Bản & Bơm Bài Thông Minh Cho Hệ Thống Phone Farm **AKPhone**.

[![CI](https://github.com/example/Support_AKphone/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🎯 Tính Năng & Ưu Điểm Vượt Trội

1. **Triết Lý "1 Page = 1 Script Cố Định"**:
   - Không sinh file rác: Mỗi Fanpage chỉ có đúng **1 file `.txt` trong `SCRIPTS`** và **1 file `.ini` trong `OFFERS`**.
   - Không cần build lại tool hay can thiệp AKPhone khi đổi video/caption/comment.

2. **Cơ Chế Bơm Bài Gối Đầu (Live Rolling Feeder)**:
   - Giám sát tiến độ thực thi qua hệ thống cờ Done động trên Android (`/storage/emulated/0/Android/Done/`).
   - Tự động cắt bài tiếp theo nạp vào thư mục đệm (`C:\AK_Running`) ngay khi lượt hiện tại sắp hoàn tất.
   - Chuyển giao nguyên tử (Atomic Payload Cut) kèm đối soát SHA-256: Tuyệt đối không mất bài, không trùng lặp, tự động phục hồi nếu mất điện.

3. **Tự Động Bắt Lỗi & Cứu Máy (Self-Healing Watchdog)**:
   - Tự động nhận diện khi Facebook bị crash, văng app, hiện popup checkpoint hoặc treo máy quá thời gian.
   - **Tự động chụp ảnh màn hình lỗi** lưu vào `logs/screenshots/`.
   - Tự động ra lệnh ADB tắt ứng dụng Facebook (`am force-stop`) và bấm phím `HOME` để giải phóng RAM cho lượt sau.

4. **Bắt Đúng Mọi Loại Thiết Bị & Đa Máy (Dynamic Device Detection)**:
   - Tự động quét và hiển thị toàn bộ điện thoại Android đang kết nối qua ADB.
   - Hiển thị đầy đủ thông tin: Hãng, Model, Phiên bản Android, Serial.
   - Hỗ trợ chọn đích danh máy hoặc chế độ tự động chọn thiết bị sẵn sàng.

5. **Giám Sát Từ Xa Qua Telegram Bot**:
   - Gửi thông báo kết quả bài đăng, tổng kết vòng chạy.
   - Gửi tin nhắn cảnh báo kèm **ảnh chụp màn hình điện thoại lỗi** trực tiếp về Telegram.

6. **Thiết Kế Động & Tương Thích Mọi Máy Windows**:
   - Không hardcode bất kỳ ổ đĩa cố định nào (`C:`, `D:`, `E:`).
   - Tự động dò tìm `adb.exe`, thư mục `AKPhone`, thư mục đệm và tài nguyên hệ thống.
   - Quản lý phiên làm việc (`session_state.json`), tự động hỏi khôi phục tiến độ khi mở lại tool.

---

## 📁 Cấu Trúc Dự Án Chuẩn (Clean Architecture)

```text
Support_AKphone/
├── .github/
│   └── workflows/
│       ├── ci.yml                 # CI: Tự động chạy Lint (Ruff) và Test (Pytest) trên Windows/Linux
│       └── build.yml              # CI/CD: Tự động đóng gói file .exe và xuất Release trên GitHub
├── assets/                        # Kịch bản mẫu & cấu hình gốc (nơi duy nhất chứa template)
│   ├── template_script.txt
│   └── template_config.ini
├── configs/                       # Cấu hình người dùng & lưu vết phiên chạy
│   ├── app_settings.json
│   └── session_state.json
├── docs/                          # Tài liệu kiến trúc
│   └── ARCHITECTURE.md
├── src/
│   └── akphone_dispatcher/
│       ├── core/                  # Mô hình dữ liệu, quét kho, chuyển file nguyên tử
│       │   ├── config.py          # Quản lý cấu hình & tự động dò tìm đường dẫn
│       │   ├── models.py          # Dataclasses (PostItem, PageInfo, DeviceInfo)
│       │   ├── payload.py         # Cắt bài an toàn, SHA-256 checksum & rollback
│       │   └── scanner.py         # Quét kho bài, tự động ghép video + caption + cmt
│       ├── services/              # Các dịch vụ nền tảng
│       │   ├── farm_service.py    # Khởi tạo script/kho bài cho từng máy, khôi phục phiên (không phụ thuộc GUI)
│       │   ├── adb_service.py     # Giao tiếp ADB, quét thiết bị, chụp màn hình, dọn rác
│       │   ├── notifier.py        # Gửi thông báo và ảnh lỗi qua Telegram Webhook
│       │   └── session.py         # Lưu và khôi phục phiên làm việc khi tắt tool
│       ├── gui/                   # Giao diện CustomTkinter
│       │   ├── app.py             # Cửa sổ chính: khởi tạo dịch vụ, header, log, quét thiết bị
│       │   ├── tabs/              # Mỗi tab một mixin: farm_tab, progress_tab, template_tab, settings_tab
│       │   └── workers/           # Luồng giám sát chạy ngầm (FeederWorker)
│       └── utils/                 # Tiện ích chung
│           ├── helpers.py         # Sắp xếp tự nhiên (natural sort), chuẩn hóa path
│           └── logger.py          # Ghi log đa kênh (Console, File, GUI Textbox)
├── tests/                         # Bộ kiểm thử tự động (Unit Tests)
├── Build_Tool.bat                 # Script đóng gói ra file .exe chỉ với 1 click
├── Chay_Tool.bat                  # Script khởi chạy nhanh giao diện
├── pyproject.toml                 # Tiêu chuẩn đóng gói Python hiện đại
└── requirements.txt               # Thư viện phụ thuộc
```

---

## 🚀 Hướng Dẫn Sử Dụng

### 1. Khởi chạy nhanh (Môi trường Python)
Nhấp đúp chuột vào file:
```cmd
Chay_Tool.bat
```
Hoặc chạy lệnh qua terminal:
```bash
python main.py
# hoặc
python -m akphone_dispatcher
```

### 2. Đóng gói ra file `.exe` (PyInstaller)
Nhấp đúp chuột vào file:
```cmd
Build_Tool.bat
```
File thực thi độc lập sẽ được tạo tại `dist\AKPhone_Dispatcher_Recovered.exe`.

### 3. Chạy kiểm thử tự động (Tests)
```bash
pytest
```

---

## 🛠️ Yêu Cầu Hệ Thống

- **Hệ điều hành**: Windows 10 / Windows 11 (hoặc Linux/macOS cho các module core).
- **Python**: 3.10 trở lên.
- **Phụ thuộc**: `customtkinter`, ADB (`Android Debug Bridge`).

---

## 📜 Giấy Phép
Dự án được phân phối dưới giấy phép **MIT**.
