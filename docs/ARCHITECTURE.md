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
3. **Mất kết nối điện thoại giữa chừng**: lệnh đọc cờ chạy `ls <thư mục> 2>&1; echo __AKLS_RC=$?`.
   Không có dấu kết thúc (rút cáp, adb không in gì, đọc bị cắt) hoặc thư mục cờ không có (máy đang
   khởi động, bộ nhớ chưa sẵn) → coi là *không đọc được*, không bao giờ coi là "cờ đã bị xóa".
   Cắm cờ đầu vòng cũng đọc lại để chắc cờ đã có trên máy. Áp dụng cho cả cờ Done và cờ CMT.
4. **Lưu tiến độ liên tục** (`configs/session_state.json`, ghi file tạm + fsync + đổi tên): mỗi máy
   lưu ngay khi một page đăng xong (`posted_idx`), khi nạp bài mới (`current_idx`) và đầu/cuối vòng.
   Phiên lưu cả danh sách bài, nên khôi phục sau mất điện không phải quét lại kho (kho đã thiếu các bài
   đã cắt). Page có bài đang ở thư mục đệm *đã đăng rồi* (`posted_idx >= current_idx`) không bao giờ được
   cắm cờ lại: tool nạp bài kế tiếp trước.
5. **Nạp bài lỗi** (thiếu file nguồn, lỗi hash...): page bị tạm ngưng thay vì chạy lại video cũ (sẽ đăng
   trùng), báo Telegram 1 lần, tự thử nạp lại mỗi 60 giây; sửa kho xong là page chạy tiếp. Máy chỉ còn page
   lỗi thì hiện "⚠️ Chờ sửa lỗi nạp bài".

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
| Luồng mới: 60 s sau khi đăng, đọc màn đầu tab Reels (không cuộn); reel mới chỉ là *ứng viên*. Chưa thấy thì thử lại mỗi 4 phút (6 lần), sau đó theo chu kỳ view | `CommentTracker` (luồng quét) | `posted` |
| Luồng xác minh (song song): mở trang từng reel ứng viên, đọc caption gốc + giờ tạo; gán cho bài có **caption khớp** và **giờ tạo trong khoảng 30 phút trước → 15 phút sau lúc cờ Done bị xóa** | `CommentTracker` (luồng xác minh) | `linked` |
| Luồng cũ: mỗi `views_interval_min` (60 phút) đọc 1 lần tab Reels của page, cuộn tới khi thấy đủ mọi reel đang theo dõi | `CommentTracker` | `linked` (cập nhật view) |
| Lượt xem ≥ ngưỡng: ghi `linkcmt{ID}.txt` + `Comment_{ID}.txt` vào `SCRIPTS\FILES`, tạo cờ `CMT/{ID}.txt` | `CommentTracker` | `marked` |
| Script comment xóa cờ `CMT/{ID}.txt` | AKPhoneS | `done` |
| Cờ còn sau `cmt_timeout_s` / quá `expire_hours` chưa đủ view | `CommentTracker` | `failed` / `expired` |

- **Link Fanpage điền theo từng máy ở Tab 1**, ô "Link Fanpage" cạnh ô tên: dòng N của link đi với dòng N
  của tên. Mỗi bài lưu lại link page tại lúc nạp bài, và tool quét theo link đó (không theo tên).
  Bấm "💾 Lưu Tên + Link": page giữ nguyên tên thì link được điền/sửa ngay, kể cả cho bài đang theo dõi;
  page đổi tên (thay page mới) thì áp dụng sau khi Khởi Tạo lại, bài cũ vẫn theo dõi page cũ với link cũ.
- Tab 5 hiển thị theo cây **Máy → Page → Bài** (trạng thái, lượt xem, link reel, nội dung comment);
  nhấp đúp để mở link reel hoặc link Fanpage.
- **Đăng lại (bấm tay, sau khi lượt auto của máy đã xong):** chọn bài/page/máy ở Tab 5 → "🔁 Đăng lại bài lỗi".
  Đăng lại được: bài chưa đăng được, bài "⚠ Nghi đăng lỗi" (đã xóa cờ Done nhưng sau 6 lần không thấy reel),
  bài hết hạn mà chưa có link. Tool chép lại video từ `.payload-store` vào `AK_Running\{ID}`, ghi
  `Caption_{ID}.txt`, cắm cờ `Done/{ID}.txt` (mỗi page 1 bài một lúc); người dùng chạy `{ID}.txt` trên
  AKPhoneS; cờ mất → bài đi tiếp luồng lấy link → view → comment, comment giữ nguyên. Máy đang chạy auto
  thì chờ. Bản lưu của bài chưa có link reel không bị dọn cuối vòng.
- "💬 Comment lại": bài comment lỗi quay về "đang theo dõi" để đẩy lại link + comment.
- "Hiện lịch sử 7 ngày": đọc thêm `configs/comment_archive/*.jsonl` để xem lại bài đã xong của từng via.
- **`cmt{ID}.txt` chỉ có nội dung khi page có việc comment.** Lúc khởi tạo và sau khi comment xong/lỗi, tool
  ghi nó thành `WAIT=1` và làm rỗng `linkcmt{ID}.txt` / `Comment_{ID}.txt`; khi đủ view mới ghi script thật.
  AKPhoneS chạy script comment bao nhiêu lần cũng không comment lặp vào bài cũ.
- Cờ của page cuối và `round_done` thường mất trong cùng 1 lần kiểm tra (removedone.txt chạy ngay sau):
  tool xử lý cờ đã mất trước rồi mới tính page còn kẹt, nên page cuối không bị đánh lỗi và đăng trùng.
- **Chạy nhiều ngày với tên file trùng nhau** (`01.mp4`, `01.txt`… mỗi ngày): tool không dựa vào tên file.
  Mỗi bài có mã riêng trong hàng đợi, bản lưu riêng `.payload-store\<page>\<uuid>\`, comment chép lúc nạp,
  ghép theo link reel. Khởi tạo lại sang kho ngày mới mà page còn bài chưa đăng được thì bài đó thành
  "hết hạn (chưa đăng được)" và vẫn Đăng lại được, không mất im lặng. Không cho Khởi Tạo khi máy đang chạy
  auto. Tab 5 ghi ngày nạp cạnh mỗi bài.
- Chạy thử toàn luồng không cần điện thoại/Facebook: `python tests/simulate_e2e.py` (AKPhoneS, điện thoại và
  Facebook giả lập trong thư mục tạm; kiểm tra đăng bài, lỗi script, mất reel + đăng lại, comment lỗi +
  comment lại, thay page).
- Mỗi page chỉ có **1 việc comment tại một thời điểm**, nên file link/comment không bị ghi đè khi script đang đọc.
- Trạng thái được ghi `marked` **trước** khi tạo cờ: tắt tool giữa chừng không gây comment 2 lần.
- Hàng đợi lưu ở `configs/comment_queue.json`. Danh sách profile Facebook ở `configs/fb_profiles.json`
  (lần đầu: `configs/browser_profile/` đang đăng nhập thành profile "Mặc định"; profile mới ở
  `configs/fb_profiles/<id>/`).
- Lần quét đầu của một page chỉ lấy mốc (mọi reel đang có là bài cũ). Bài đăng xong trước lần quét mốc
  không xác định được link và được đánh `expired`.
- **Không ghép theo thứ tự.** Reel mới chỉ được gán cho bài khi có đủ 2 bằng chứng: caption gốc trên Facebook
  trùng caption đã lưu lúc nạp bài (bỏ qua hoa/thường, emoji, dấu câu, xuống dòng; cho phép một bên bị cắt
  ngắn) và giờ tạo reel nằm trong khoảng `verify_before_min`/`verify_after_min` (30/15 phút) quanh lúc cờ Done
  bị xóa. Nhiều bài cùng caption (đăng lại) → chọn bài có giờ gần nhất. Reel đăng tay / không khớp bài nào:
  chờ 6 giờ rồi bỏ qua, không bao giờ gán. Cùng caption mà lệch giờ: không gán, ghi chú vào bài.
- Trang reel chứa nhiều "story" (reel đang mở, các reel tiếp theo, gợi ý khi đăng nhập). Story của reel là
  story có `post_id` nằm sát mã video của reel (vài chục ký tự), đúng cả khi đăng nhập lẫn không; caption lấy
  từ `message.text` (bản gốc, không phải bản dịch tự động hiển thị trên trang).
- **Bắt buộc tài khoản Facebook**: profile chỉ được quét khi có cookie đăng nhập (`c_user`), kiểm tra lúc mở và
  sau mỗi lần tải trang. Mất cookie (hết hạn/đăng xuất) → profile bị khóa "chưa đăng nhập / phiên hết hạn",
  báo Telegram. Bật theo dõi: tool kiểm tra đăng nhập các profile trước; không có profile nào đăng nhập thì
  không bật (báo lỗi + mở popup Profile). Luồng xác minh mượn cookie + proxy của profile đang quét
  (`shared_session`) trong trình duyệt riêng, nên chạy song song mà không khóa thư mục profile.
- **Nhiều profile, xoay vòng** (`services/fb_pool.py`): mỗi lúc chỉ mở 1 trình duyệt; profile hiện tại mở
  `rotate_every` trang (mặc định 10) rồi đóng, profile bật kế tiếp làm tiếp. Gặp màn hình đăng nhập/checkpoint,
  hoặc không mở được trình duyệt (hồ sơ đang mở ở cửa sổ khác): profile đó bị đánh `blocked` (lưu lại, báo
  Telegram), không tự vượt, quét tiếp bằng profile khác; chỉ dừng quét khi mọi profile bị chặn. Proxy lỗi
  (`ERR_PROXY…`/`ERR_SOCKS…`): profile nghỉ 10 phút rồi tự thử lại (có thể chỉ là mất mạng). Không tự chuyển
  sang Chromium (bản đó không đọc được phiên đăng nhập của Edge).
- **Proxy theo profile**: HTTP (có/không user/pass) đưa thẳng cho trình duyệt. SOCKS5 có user/pass (dạng
  `ip:port:user:pass` của AKPhoneS): Chromium không tự đăng nhập được, nên `services/socks_relay.py` mở cầu
  nối SOCKS5 không mật khẩu trên 127.0.0.1 và tự đăng nhập proxy thật. Cửa sổ đăng nhập dùng đúng proxy của
  profile. Nút "Kiểm tra proxy" lấy IP ra ngoài qua `api.ipify.org`.

**Ba luồng chạy song song:**

- *Luồng cờ CMT* (mỗi 20 giây, không bao giờ chờ trình duyệt): cờ mất → `done`, quá hạn → `failed`,
  hết hạn bài, đẩy link + comment khi đủ ngưỡng, lưu trữ việc đã xong quá 24 giờ sang
  `configs/comment_archive/YYYY-MM-DD.jsonl`.
- *Luồng quét* (mỗi lần 1 page, nghỉ 2–5 giây): ưu tiên lấy mốc → tìm reel mới → view (page quá hạn lâu nhất trước).
- *Luồng xác minh* (mỗi lần 1 reel, nghỉ 1–3 giây, ~2–3 s/reel): đọc caption + giờ của reel ứng viên rồi ghép;
  5 reel liên tiếp không đọc được → báo Telegram (Facebook có thể đã đổi giao diện).

**Tối ưu quét:** 1 tab dùng lại, chặn ảnh/video/font, đọc ngay khi ô reel xuất hiện, cuộn theo nhu cầu,
khởi động lại trình duyệt sau 300 lần quét. Reel mới chỉ tính là các reel đứng **trước reel mới nhất đã
biết**, nên cuộn sâu không biến reel cũ thành "mới". Đo thực tế (tài khoản đã đăng nhập): luồng mới ~2 s,
luồng cũ cuộn tới reel thứ 30 ~3,5 s. Số lần mở trang tính theo page, không theo bài: 150 page ≈ 150 lượt/giờ
cho view + khoảng 19 lượt/giờ cho bài mới.

---

## 6. Chạy lâu ngày (độ bền)

- **Mất mạng khi quét**: lần tải trang lỗi không tính là lần thử lấy link (không gây "Nghi đăng lỗi" oan);
  5 lần lỗi liên tiếp → báo Telegram 1 lần, có mạng lại → báo lại.
- **Lệch đồng hồ**: luồng xác minh đo giờ máy so với Facebook (header `Date`) mỗi giờ, bù độ lệch khi so giờ
  tạo reel với lúc xóa cờ Done; lệch ≥ 2 phút → cảnh báo.
- **Reel gợi ý**: reel đúng giờ nhưng khác caption (caption bị sửa trên Facebook), hoặc cùng caption nhưng lệch
  giờ → không tự gán, ghi `verify_note` + `suggested_url`; người dùng kiểm tra rồi bấm "Nhận reel gợi ý".
- **Watchdog**: quá `watchdog_timeout` không cờ nào mất → chỉ page đang chạy (page đầu còn cờ) bị tính lỗi,
  chụp màn hình + tắt Facebook 1 lần, đếm lại; hết thêm 1 khoảng nữa vẫn không page nào chạy → cả vòng lỗi.
- **Page lỗi liên tiếp**: `max_page_fail_rounds` (3) vòng lỗi liên tiếp → page tạm ngưng (`paused_reason`), báo
  Telegram; bấm Chạy lại máy để thử lại. Lưu trong phiên.
- **Telegram**: một luồng gửi theo hàng đợi (tối đa 300 tin), lỗi mạng → giữ và gửi lại (15 s → 5 phút), tin
  gửi trễ ghi kèm giờ sự việc.
- **Hàng đợi comment**: ghi file tạm + fsync + đổi tên; bản dự phòng `comment_queue.json.bak` (cập nhật tối đa
  10 phút/lần); file chính hỏng → dùng bản dự phòng, giữ file hỏng thành `.broken`. Cập nhật hàng loạt được
  gộp thành 1 lần ghi (`queue.batch()`).
- **Dọn ổ đĩa** (`utils/housekeeping.py`, lúc mở tool và mỗi 6 giờ): ảnh lỗi > 7 ngày, lịch sử comment > 30 ngày,
  bản lưu payload kẹt (`preparing` / `failed_source_retained` / `interrupted_source_restored`) > 7 ngày khi file
  nguồn vẫn còn trong kho, thư mục `.transfer-work` sót > 1 ngày, cache trình duyệt > 300 MB (chỉ khi trình duyệt
  quét đang đóng; cookie đăng nhập giữ nguyên). `app.log` tự chia ở 10 MB, giữ 5 file.
- **Đóng tool (X)**: dừng luồng đăng bài + 3 luồng comment, đóng trình duyệt quét (đã kiểm tra: không sót tiến
  trình Edge), lưu phiên từng máy (không ghi đè phiên đã lưu bằng hồ sơ rỗng), ghi hàng đợi, gửi nốt Telegram.
- **Thư mục cờ CMT**: script comment xóa cờ theo thư mục cài ở Tab 5 (`{CMT_DIR}` trong mẫu, hoặc thay đường
  mặc định).
- **Mẫu script** (`core/script_templates.py`): kiểm tra mẫu đăng bài / comment với luồng tool trước Khởi Tạo, khi
  lưu ở Tab 3 và khi Áp dụng; lỗi chặn (PUSH_FILE cứng, thiếu `{CAPTION_PATH}` / xóa cờ Done / `{LINK_PATH}` /
  `{COMMENT_PATH}` / xóa cờ CMT, biến lạ), cảnh báo (không kiểm tra cờ Done trước khi đăng, còn lệnh sau dòng xóa
  cờ, comment trong script đăng bài). "Áp dụng mẫu" viết lại script mọi page đã khởi tạo mà không đụng bài/kho/
  tiến độ; `cmt{ID}.txt` của page đang có việc comment được viết lại bản thật, page khác giữ `WAIT=1`.
- **Cờ CMT**: khi có việc comment đang chờ script, luồng cờ kiểm tra mỗi 5 giây (bình thường 20 giây) để đưa
  `cmt{ID}.txt` về `WAIT=1` ngay sau khi comment xong, tránh vòng lặp AKPhoneS ngắn chạy lại comment lần 2.

