# Thiết kế: theo dõi lượt xem và đăng comment tự động

Trạng thái: **giai đoạn 1 (manifest, sổ cái), template mới và giai đoạn 2 (dịch vụ theo dõi với nguồn giả) đã cài đặt; vòng comment trên điện thoại, nguồn quét web thật và vận hành 24/24 chưa làm.**

## 1. Mục tiêu

Sau khi một bài được đăng, tool theo dõi lượt xem của bài đó. Khi lượt xem đạt ngưỡng, tool
đẩy nội dung comment (có sẵn lúc nhập kho) vào hàng đợi để AKPhone đăng comment cho đúng page.
Chạy liên tục 24/24, cùng điện thoại với luồng đăng bài hiện tại.

## 2. Quyết định đã chốt

| Mục | Quyết định |
|---|---|
| Điều kiện | Lượt xem đạt ngưỡng. Ngưỡng chung mặc định; page nào cần thì chuyển sang ngưỡng riêng |
| Nguồn lượt xem | Quét web bằng nhiều tài khoản Facebook đăng nhập, gắn proxy (xem mục 7). **Không dùng Graph API** |
| Cách chạy trên điện thoại | AKPhone chạy script theo thứ tự cơ bản; phần comment nằm ở cuối script đăng bài của đúng page |
| Video | Template dùng `{VIDEO_DIR}`, không cố định ổ đĩa |
| Nội dung comment | Có sẵn lúc nhập kho, mỗi bài một comment (nối thành một dòng như hiện nay) |
| Quy mô | 4–10 máy, 20–50 page mỗi máy, 3–5 bài mỗi page mỗi ngày (tối đa 500 page, khoảng 2.500 bài/ngày) |
| Thử lại | Mỗi script chạy một lần. Lỗi thì bỏ qua và báo, không chạy lại |

## 3. Kiến trúc

```
Kho (manifest) ─► Worker đăng bài ──ghi sự kiện──►  SỔ CÁI (SQLite)  ◄──── Dịch vụ theo dõi
                        │                                  │                 (nguồn lượt xem)
                        │                                  ▼ đạt ngưỡng
                        └─────────── cùng vòng ◄── Bộ điều phối comment
                                                   (ghi file link/comment, tạo mục báo việc)
```

- **Sổ cái** là nguồn sự thật duy nhất; mọi trạng thái sống sót sau khi tắt máy.
- **Nguồn lượt xem** là giao diện cắm thay được (bản giả để test, bản quét web để chạy thật).
- **Luồng đăng bài** chỉ thêm việc ghi sự kiện vào sổ cái; phần còn lại giữ nguyên.
- **Vòng comment** dùng lại cơ chế cờ, watchdog, phát hiện lỗi của vòng đăng bài.

## 4. Sổ cái và vòng đời bài

Bảng chính `posts` (mỗi bài một dòng): `post_uid`, `serial`, `via`, `page_id`, `page_folder`,
`seq`, `status`, `fed_at`, `posted_at`, `fb_url`, `views`, `views_at`, `comment_file`,
`comment_state`, `expires_at`.

Bảng phụ: `post_events` (nhật ký mọi chuyển trạng thái), `page_settings`
(`threshold_mode` = `global`|`custom`, `custom_threshold`), `accounts`, `proxies`, `scrape_log`.

Trạng thái bài:

```
queued → fed → posted → tracking → threshold_hit → comment_marked → commented
                 │          │                              └────→ comment_failed
                 │          └────→ expired (quá hạn chưa đạt ngưỡng)
                 └────→ post_failed
```

Quy tắc: chuyển sang `comment_marked` được ghi **trước** khi tạo mục báo việc trên điện thoại,
nên một bài không bao giờ bị comment hai lần dù tool bị tắt giữa chừng.

## 5. Nhập kho và định danh bài

Bước nhập kho tạo `manifest.json` cho mỗi thư mục page, liệt kê từng bài:

```json
{ "page": "page-1", "posts": [
  { "uid": "page-1_0007", "video": "video/v07.mp4",
    "caption": "caption/c07.txt", "comment": "comments/m07.txt" } ] }
```

Tool đọc manifest, không ghép theo thứ tự sắp xếp. Kiểm tra và ghi manifest bằng
`python -m akphone_dispatcher.tools.import_store <thư mục kho> [--check]`. Page chưa có manifest
vẫn chạy: tool tự ghép theo số trong tên file (`v07` ↔ `c07` ↔ `m07`), chỉ ghép theo thứ tự khi số
file ba thư mục bằng nhau. Thiếu caption hoặc comment thì báo lỗi lúc
nhập kho, không đăng chuỗi giả. Cách này cũng sửa lỗi ghép sai caption đã biết.

## 6. Giao thức giữa tool và AKPhone

1. **Biến trong template:** `{ID}`, `{PAGE_NAME}`, `{VIDEO_DIR}`, `{CAPTION_PATH}`,
   `{COMMENT_PATH}`, `{LINK_PATH}`, `{CMT_MARKER}`. Không còn tên page cố định trong script.
2. **File trên máy tính** (`SCRIPTS\FILES`): `Caption_{ID}.txt`, `Comment_{ID}.txt`,
   `Link_{ID}.txt` (link bài cần comment).
3. **Mục báo việc trên điện thoại:** tool tạo `cmt{ID}.txt` bằng ADB trong thư mục cấu hình
   được (mặc định `/storage/emulated/0/Cmt/`) khi page có bài đạt ngưỡng. Script comment xóa mục
   này khi xong; tool theo dõi mục biến mất như cờ `Done`.
4. **Thứ tự cờ trong vòng:** `page1, (cmt1 nếu có việc), page2, (cmt2 nếu có việc), …, round_done`.
   Tool biết trước từ đầu vòng page nào có việc comment.
5. **Một mục comment mỗi page mỗi vòng:** page có nhiều bài đạt ngưỡng thì chọn bài cũ nhất,
   các bài còn lại sang vòng sau.
6. **Phần comment ở cuối script đăng bài** của page. Không có mục báo việc thì thoát sau vài giây
   (đặt thời gian chờ ngắn), có thì chạy phần đăng comment. Không đổi page lần hai.

Cờ `Done` của page và cờ comment được theo dõi riêng để watchdog thấy cả hai.

## 7. Nguồn lượt xem: quét web bằng nhiều tài khoản

Giao diện chung: một lần mở trang của một page trả về danh sách bài quan sát được
(`url`, thời gian đăng gần đúng, đoạn caption, lượt xem). Một lần mở trang cho cả 3–5 bài của page.

**Khớp bài:** bài mới đăng được khớp bằng thời gian tool ghi nhận "đã đăng" cộng đoạn caption.
Mỗi page chỉ 3–5 bài mỗi ngày nên ít nhầm; khớp được thì lưu `fb_url` vào sổ cái.

**Phân tích số:** đọc đúng định dạng tiếng Việt (ví dụ `1,2K`, `1,2 N`, `1,2 tr`) và có test riêng.

**Hồ tài khoản và proxy:**
- Mỗi tài khoản có một thư mục hồ sơ trình duyệt riêng, giữ phiên đăng nhập.
- Mỗi tài khoản gắn **một proxy cố định** (sticky). Không đổi IP giữa các request của cùng một
  tài khoản đang đăng nhập, vì việc đó thường khiến Facebook yêu cầu xác minh. Xoay ở mức *tài
  khoản*: phân phối các page cho các tài khoản khác nhau.
- Lập lịch: mỗi tài khoản chỉ mở một trang mỗi 30–60 giây, có độ trễ ngẫu nhiên, và nghỉ theo giờ.
- Cầu dao an toàn: gặp checkpoint, màn hình xác minh hoặc bị đăng xuất thì **tạm dừng tài khoản
  đó và báo Telegram**; không tự giải CAPTCHA, không giả mạo dấu vân tay trình duyệt.
- Tài khoản và proxy lưu ngoài Git (xem mục 10).

**Tải ước tính:** 500 page, mỗi page quét 1–2 giờ một lần ≈ 250–500 lần mở trang mỗi giờ. Với
5–10 tài khoản, mỗi tài khoản khoảng 50–100 lần mở mỗi giờ. Bài còn xa ngưỡng quét thưa; bài gần
ngưỡng quét dày; bài đã comment hoặc hết hạn thì dừng.

**Rủi ro cần chấp nhận (ghi rõ):** việc dùng nhiều tài khoản tự động quét Facebook trái điều khoản
của Facebook và có thể dẫn đến khóa các tài khoản dùng để quét; giao diện Facebook đổi sẽ làm
trình phân tích hỏng. Vì vậy nguồn lượt xem phải cắm thay được và có bản giả để test.

## 8. Vòng comment trên điện thoại

- Đầu mỗi vòng, bộ điều phối tra sổ cái: page nào có bài `threshold_hit` thì ghi `Link_{ID}.txt`,
  `Comment_{ID}.txt` và tạo mục `cmt{ID}.txt` (sau khi đã ghi `comment_marked`).
- Trong vòng, tool theo dõi mục biến mất. Mục còn nguyên khi sang cờ sau hoặc hết thời gian là lỗi:
  ghi `comment_failed`, chụp màn hình, báo Telegram, không thử lại.

## 9. Quá tải, tốc độ và chống spam

- Mỗi page tối đa một comment mỗi vòng; có giới hạn comment mỗi page mỗi ngày (mặc định bằng số
  bài mỗi ngày). Thêm độ trễ ngẫu nhiên giữa các lần comment.
- Hạn theo dõi mặc định 7 ngày; quá hạn chưa đạt ngưỡng thì `expired`.
- Cảnh báo quá tải: nếu thời gian ước tính của một vòng vượt quá chu kỳ mong muốn, báo rõ để giảm
  số page trên máy đó.

## 10. Vận hành 24/24

- Dịch vụ nền chạy được không cần mở giao diện; giao diện chỉ để xem sổ cái và trạng thái.
- Tự khôi phục từ sổ cái sau khi mất điện hoặc khởi động lại; tự khởi động lại ADB khi cần.
- Log xoay vòng; Telegram báo: tài khoản bị chặn, nguồn lượt xem lỗi kéo dài, quá tải, comment lỗi.
- Bí mật (tài khoản, mật khẩu, cookie, proxy) lưu ngoài Git và ngoài `app_settings.json`.

## 11. Các giai đoạn

| GĐ | Nội dung | Xong khi |
|---|---|---|
| 0 | Kiểm chứng: đăng nhập một tài khoản qua một proxy, mở trang một page, đọc đúng lượt xem 5 bài; chạy tay một script comment | Số đọc được khớp số hiển thị, ổn định qua một ngày |
| 1 | Manifest nhập kho, mã bài, sổ cái SQLite, worker ghi sự kiện | Đăng bài chạy như cũ và sổ cái đúng sau khi khởi động lại |
| 2 | Dịch vụ theo dõi + nguồn giả; ngưỡng chung/riêng; khớp bài | Test mô phỏng đạt ngưỡng sinh đúng `threshold_hit` |
| 3 | Template mới, mục báo việc, vòng comment, theo dõi cờ comment | Chạy thật một vòng trên một máy với một page |
| 4 | Nguồn quét web thật + hồ tài khoản/proxy | Chạy ổn nhiều giờ, cầu dao hoạt động |
| 5 | Vận hành: dịch vụ nền, log, Telegram, tab xem trạng thái | Chạy liên tục 24 giờ không can thiệp |

## 12. Câu hỏi mở

1. Giá trị mặc định: ngưỡng chung, số ngày hết hạn, giới hạn comment mỗi page mỗi ngày.
2. Số tài khoản Facebook và proxy hiện có; loại proxy (dân cư/cố định).
3. Thư mục mục báo việc trên điện thoại có đúng `/storage/emulated/0/Cmt/` không.
4. Trình duyệt tự động hóa dùng gì (cần chốt ở giai đoạn 0; ảnh hưởng dung lượng bản đóng gói).
