# ChemCraft LMS — Hướng dẫn tích hợp (Giai đoạn 1: Phân quyền + Class Management + Teacher Dashboard, cộng nền tảng cho toàn bộ Phần I-IV)

## 1. File mới / đã sửa

| File | Loại | Việc cần làm |
|---|---|---|
| `lms_db.py` | mới | Copy vào thư mục gốc cùng `server.py` |
| `lms_auth.py` | mới | Copy vào thư mục gốc |
| `lms_routes.py` | mới | Copy vào thư mục gốc |
| `server.py` | **đã sửa** | Thay file cũ bằng bản này (chỉ thêm 2 dòng import + đăng ký blueprint + 1 đoạn kiểm tra usage AI trong `/api/chat`, KHÔNG đổi gì khác) |
| `teacher.html` | mới | Copy vào thư mục gốc (route tĩnh, Flask serve qua `static_folder='.'` có sẵn) |
| `classes.html` | mới | Copy vào thư mục gốc — thay thế/di kèm module "Bài học" hiện tại cho học sinh chọn lớp |
| `class-detail.html` | mới | Copy vào thư mục gốc |
| `admin_lms_module.js` | mới | Copy vào thư mục gốc, thêm `<script src="admin_lms_module.js" defer></script>` vào `admin.html` trước `</body>` |
| `firestore.rules` | mới | Dán vào Firebase Console → Firestore Database → Rules → Publish |

Không cần thêm dependency nào trong `requirements.txt` — `firebase_admin` (đã có sẵn) đã kèm `google-cloud-firestore` (dùng cho `ArrayUnion`/`ArrayRemove`) và `firebase_admin.auth` (dùng để verify ID token).

## 2. Việc còn lại bạn cần tự làm trong `admin.html`

Mở `admin.html`, tìm khối `const PAGES = {...}` (dòng ~749) và thêm:

```js
PAGES['lms-teachers']      = { title:'Giáo viên',      sub:'Quản lý quyền & Premium ChemCraft for edu', render: () => window.CC_LMS.renderTeachers() };
PAGES['lms-classes']       = { title:'Lớp học',        sub:'Toàn bộ lớp học trong hệ thống',            render: () => window.CC_LMS.renderClasses() };
PAGES['lms-subscriptions'] = { title:'Subscriptions',  sub:'Thống kê Premium & Usage',                  render: () => window.CC_LMS.renderStats() };
```

Rồi thêm 3 mục tương ứng vào sidebar nav (copy cách `users`/`profile` đang được render trong sidebar hiện tại, chỉ đổi `key` sang `lms-teachers`/`lms-classes`/`lms-subscriptions`).

## 3. Cách phân quyền hoạt động

- Mọi tài khoản mới đăng ký qua `index.html` (Firebase Auth + `users/{uid}`) mặc định có `role: 'student'`, `plan: 'free'` — được tự động gán bởi `lms_db.ensure_user_defaults()` ngay lần đầu bất kỳ API `/api/lms/*` nào đọc user đó (không cần chạy migration thủ công).
- Để 1 tài khoản trở thành giáo viên: vào admin.html → tab **Giáo viên** → nhập UID (xem trong tab **Quản lý người dùng** hiện có) → "Nâng lên Teacher".
- Giáo viên đăng nhập vào `teacher.html` bằng đúng tài khoản Firebase Auth đó → thấy Teacher Dashboard. Học sinh cố vào sẽ thấy "Access Denied" đúng theo yêu cầu.
- Admin quản lý LMS (cấp Premium, xem thống kê...) dùng **đúng hệ thống đăng nhập admin hiện tại** (`X-Admin-Token`, SQLite `admin_accounts`) — KHÔNG cần tài khoản Firebase riêng cho admin, giữ nguyên 1 cổng đăng nhập admin duy nhất như hiện tại.

## 4. Bảo mật — khác biệt quan trọng so với tracker.js

`tracker.js` gửi `userId` không xác minh (chấp nhận được vì chỉ ghi thống kê). Các API LMS mới (`/api/lms/*`, trừ nhóm `/api/lms/admin/*`) đòi hỏi header:

```
Authorization: Bearer <Firebase ID token>
```

lấy từ `await window._currentUser.getIdToken()` phía client — đã có sẵn trong `teacher.html`/`classes.html`/`class-detail.html`. Backend verify bằng `firebase_admin.auth.verify_id_token()`, nên không thể giả mạo `uid` như cách tracking cũ vẫn làm — cần thiết vì giờ đây các action này đụng tới quyền hạn thật (chấm điểm, tạo lớp, cấp Premium).

**Ngoại lệ đã biết**: gate giới hạn AI (5 lượt/ngày) chèn vào `/api/chat` vẫn dùng `userId` từ body JSON như code cũ (không đổi cách xác thực của route này để tránh phá vỡ luồng chat hiện tại) — nghĩa là về lý thuyết một client có thể gửi `userId` giả để né giới hạn free. Nếu cần chặt hơn, bước tiếp theo là đổi `/api/chat` sang cùng cơ chế Bearer token như trên.

## 5. Livestream — YouTube/Facebook Live

Đã chọn theo yêu cầu: giáo viên tạo buổi livestream trong `teacher.html` bằng cách dán **link nhúng (embed)**:
- YouTube Live: `https://www.youtube.com/embed/<VIDEO_ID>`
- Facebook Live: lấy link "Nhúng video" (Embed) từ nút Share trên bài live, dán URL trong thẻ `<iframe src="...">` vào ô nhập.

Chat trong lúc xem livestream KHÔNG dùng chat gốc của YouTube/Facebook (vì đó là chat của nền tảng ngoài, không đồng bộ được với hệ thống ChemCraft) — mà dùng chat riêng của ChemCraft (`livestreams/{id}/chat_messages`, poll mỗi 4 giây), hiển thị bên dưới khung video nhúng, đúng bố cục trong bản yêu cầu.

## 6. Freemium đã áp dụng ở đâu

- **AI**: gate trong `/api/chat` (server.py), đếm gộp mọi loại AI, reset lúc 00:00 giờ Việt Nam.
- **Submission**: gate trong `POST /api/lms/assignments/<id>/submit` — chỉ tính khi NỘP MỚI (sửa bài đã nộp trước hạn không tính thêm lượt).
- Bài kiểm tra (`tests`) hiện **chưa** tính vào giới hạn submission — bản yêu cầu chỉ liệt kê "Bài tập lớp học" + "Bài kiểm tra" cộng lại = 2/ngày ở ví dụ, nhưng đó chỉ là ví dụ minh hoạ, không phải yêu cầu bắt buộc tách biệt. Nếu bạn muốn bài kiểm tra cũng tính vào cùng giới hạn 2 lượt/ngày, thêm 1 dòng gọi `lms_db.try_consume_submission_usage(uid)` vào đầu route `submit_test` trong `lms_routes.py`.
- "ChemCraft for edu": admin cấp cho giáo viên → **tự động lan xuống mọi học sinh đã tham gia lớp của giáo viên đó** (`lms_db._sync_sponsored_students`), và học sinh tham gia lớp MỚI sau khi giáo viên đã Premium cũng tự động được hưởng (xử lý trong `join_class_by_code`).

## 7. Những phần của bản yêu cầu CHƯA làm ở lượt này (do quy mô)

Nền dữ liệu (Firestore schema + toàn bộ API) đã có sẵn cho tất cả, nhưng UI chưa hoàn thiện 100% các chi tiết nhỏ:
- Upload file thật (tài liệu/bài nộp dạng file) — hiện `documents`/`submissions` nhận **URL file có sẵn**, chưa có nút "kéo thả upload" gọi thẳng Firebase Storage SDK từ `teacher.html`/`class-detail.html`. Cách làm: thêm đoạn `uploadBytes()` từ `firebase/storage` (giống cách bạn có thể đã làm cho ảnh đại diện), lấy `getDownloadURL()`, rồi gọi API `/documents`/`/submit` như hiện tại.
- Trang tổng quan `class-detail.html` mới hiển thị số liệu cơ bản, chưa có biểu đồ.
- Bài kiểm tra dạng "nhiều đáp án đúng" (multiple-select) trong trắc nghiệm chưa có — hiện chỉ hỗ trợ 1 đáp án đúng/câu.
- Giao diện tự luận cho giáo viên chấm (`grade-essay`) mới có API, chưa có màn hình chấm riêng trong `teacher.html` (mới có "Xem kết quả" tổng, chưa có ô nhập điểm từng câu tự luận) — dễ bổ sung vì `POST /api/lms/test-attempts/<id>/grade-essay` đã sẵn sàng.

Tất cả các phần trên chỉ là UI còn thiếu — dữ liệu, API, và phân quyền cho chúng đã đầy đủ trong `lms_db.py`/`lms_routes.py`, nên có thể bổ sung dần mà không cần đổi kiến trúc.

## 8. Thứ tự triển khai đề xuất khi deploy lên Render

1. Deploy `firestore.rules` trước (không phụ thuộc code).
2. Deploy code (server.py + các file .py + .html + .js mới) — an toàn vì mọi thay đổi ở server.py chỉ CỘNG THÊM, không xoá/sửa route cũ nào.
3. Test nhanh: đăng nhập 1 tài khoản học sinh thường → mở `classes.html` → không thấy lỗi 500. Vào `admin.html` → tab Giáo viên → nâng tài khoản đó lên teacher → tài khoản đó mở lại `teacher.html` → thấy Dashboard thay vì Access Denied.
4. Nếu Firestore báo lỗi "query requires an index" ở bất kỳ endpoint LMS nào (không nên xảy ra với các query hiện tại vì đều dùng 1 điều kiện `where` mỗi query), bấm vào link trong lỗi để tạo composite index — chỉ xảy ra 1 lần.
