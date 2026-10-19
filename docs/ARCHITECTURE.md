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

---

## 5. Comment Khi Đủ Lượt Xem (tách khỏi script đăng bài)

Script đăng bài (`template_script.txt`) không còn phần comment. Mỗi page có thêm script comment riêng
`SCRIPTS\cmt{ID}.txt` sinh từ `template_cmt.txt` (biến `{LINK_PATH}` = `linkcmt{ID}.txt`,
`{COMMENT_PATH}` = `Comment_{ID}.txt`).

| Bước | Ai làm | Trạng thái bài |
|---|---|---|
| Cắt bài vào thư mục đệm: chỉ ghi caption, comment giữ trong hàng đợi | Feeder | `fed` |
| Cờ `Done/{ID}.txt` bị xóa | Feeder | `posted` |
| Luồng mới: 60 s sau khi đăng, đọc màn đầu tab Reels (không cuộn); reel mới được gán cho bài. Chưa thấy thì thử lại mỗi 4 phút (6 lần), sau đó theo chu kỳ view | `CommentTracker` | `linked` |
| Luồng cũ: mỗi `views_interval_min` (60 phút) đọc 1 lần tab Reels của page, cuộn tới khi thấy đủ mọi reel đang theo dõi | `CommentTracker` | `linked` (cập nhật view) |
| Lượt xem ≥ ngưỡng: ghi `linkcmt{ID}.txt` + `Comment_{ID}.txt` vào `SCRIPTS\FILES`, tạo cờ `CMT/{ID}.txt` | `CommentTracker` | `marked` |
| Script comment xóa cờ `CMT/{ID}.txt` | AKPhoneS | `done` |
| Cờ còn sau `cmt_timeout_s` / quá `expire_hours` chưa đủ view | `CommentTracker` | `failed` / `expired` |

- Mỗi page chỉ có **1 việc comment tại một thời điểm**, nên file link/comment không bị ghi đè khi script đang đọc.
- Trạng thái được ghi `marked` **trước** khi tạo cờ: tắt tool giữa chừng không gây comment 2 lần.
- Hàng đợi lưu ở `configs/comment_queue.json`; phiên đăng nhập trình duyệt ở `configs/browser_profile/`.
- Lần quét đầu của một page chỉ lấy mốc (mọi reel đang có là bài cũ). Bài đăng xong trước lần quét mốc
  không xác định được link và được đánh `expired`.
- Ghép reel mới với bài theo thứ tự đăng; nếu ai đó đăng tay reel khác xen giữa thì có thể ghép nhầm (log cảnh báo).
- Gặp màn hình đăng nhập/checkpoint, hoặc không mở được trình duyệt (hồ sơ đang mở ở cửa sổ khác):
  dừng quét, báo Telegram, không tự vượt. Không tự chuyển sang Chromium (bản đó không đọc được phiên
  đăng nhập của Edge).

**Hai luồng chạy song song:**

- *Luồng cờ CMT* (mỗi 20 giây, không bao giờ chờ trình duyệt): cờ mất → `done`, quá hạn → `failed`,
  hết hạn bài, đẩy link + comment khi đủ ngưỡng, lưu trữ việc đã xong quá 24 giờ sang
  `configs/comment_archive/YYYY-MM-DD.jsonl`.
- *Luồng quét* (mỗi lần 1 page, nghỉ 2–5 giây): ưu tiên lấy mốc → link bài mới → view (page quá hạn lâu nhất trước).

**Tối ưu quét:** 1 tab dùng lại, chặn ảnh/video/font, đọc ngay khi ô reel xuất hiện, cuộn theo nhu cầu,
khởi động lại trình duyệt sau 300 lần quét. Reel mới chỉ tính là các reel đứng **trước reel mới nhất đã
biết**, nên cuộn sâu không biến reel cũ thành "mới". Đo thực tế (tài khoản đã đăng nhập): luồng mới ~2 s,
luồng cũ cuộn tới reel thứ 30 ~3,5 s. Số lần mở trang tính theo page, không theo bài: 150 page ≈ 150 lượt/giờ
cho view + khoảng 19 lượt/giờ cho bài mới.
