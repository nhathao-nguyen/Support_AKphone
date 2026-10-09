# 🏗️ Kiến Trúc Hệ Thống (Architecture Design Document)

## 1. Mục Tiêu Thiết Kế
- **Tính module hóa (Modularity)**: Tách biệt hoàn toàn giữa Core logic, Dịch vụ ADB/Mạng, Giao diện người dùng (GUI) và Tiện ích.
- **Tính động (Dynamic Design)**: Loại bỏ triệt để hardcode ổ đĩa, đường dẫn hệ thống và thiết bị. Tự động nhận diện môi trường đang chạy.
- **Tính tự phục hồi (Self-Healing)**: Tự động giải phóng ứng dụng bị treo, chụp ảnh lưu vết và gửi cảnh báo khi gặp lỗi.
- **Tính toàn vẹn dữ liệu (Data Integrity)**: Toàn bộ quá trình chuyển giao file giữa kho và thư mục chạy đều được mã hóa kiểm tra SHA-256 kèm cơ chế Rollback an toàn.

---

## 2. Các Lớp Kiến Trúc (Architecture Layers)

```
[ Giao Diện Người Dùng (GUI Layer) ]
        |
        v
[ Dịch Vụ Nền Tảng (Services Layer) ]
  ├── AdbService (ADB Daemon, Screencap, Watchdog)
  ├── TelegramNotifier (Async Webhook Alerts)
  └── SessionManager (Disk State Recovery)
        |
        v
[ Nghiệp Vụ Cốt Lõi (Core Layer) ]
  ├── AppConfig (Dynamic Auto-Discovery Settings)
  ├── Scanner (Natural Sorting & File Grouping)
  └── Payload (Atomic Transfers, SHA-256 Checksums)
        |
        v
[ Tiện Ích Hạ Tầng (Utils Layer) ]
  ├── Dynamic Path Resolvers
  └── Multi-channel Thread-safe Logger
```

---

## 3. Cơ Chế Bắt Lỗi & Watchdog Động

1. **Giám sát cờ Done ngược**:
   - Khi bắt đầu 1 vòng, hệ thống tạo các file cờ trên Android: `/storage/emulated/0/Android/Done/{page_id}.txt`.
   - Khi AKPhone thực thi xong kịch bản của page, lệnh `REMOVE` sẽ xóa file cờ tương ứng.
2. **Nhận diện bất thường**:
   - **Nhảy cóc (Skip detection)**: Nếu page sau đã xóa cờ nhưng page trước vẫn còn cờ -> Page trước đã gặp lỗi kịch bản hoặc bị bỏ qua. Hệ thống giữ nguyên bài của page đó để chạy lại lượt sau.
   - **Treo máy (Timeout detection)**: Nếu thời gian giữa các lần biến mất của cờ vượt quá ngưỡng `watchdog_timeout` (mặc định 360 giây):
     - Chụp ảnh màn hình điện thoại qua lệnh `adb exec-out screencap -p`.
     - Tắt cưỡng bức app Facebook: `am force-stop com.facebook.katana`.
     - Bấm phím Home đưa máy về màn hình chính: `input keyevent KEYCODE_HOME`.
     - Gửi tin nhắn và ảnh màn hình lỗi về Telegram.

---

## 4. CI/CD Pipeline
- **Continuous Integration (.github/workflows/ci.yml)**:
  - Tự động kiểm tra chất lượng mã nguồn bằng `ruff`.
  - Tự động chạy toàn bộ bộ kiểm thử `pytest` trên cả môi trường Ubuntu và Windows cho các phiên bản Python 3.10, 3.11, 3.12, 3.13.
- **Continuous Deployment (.github/workflows/build.yml)**:
  - Tự động chạy PyInstaller trên Windows runner khi push tag hoặc kích hoạt thủ công.
  - Tự động đóng gói `.zip` và tạo GitHub Release đính kèm file thực thi hoàn chỉnh.
