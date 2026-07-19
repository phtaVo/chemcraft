"""
lms_auth.py — Xác thực người dùng cho các API LMS mới.

Khác với phần tracking cũ (tracker.js gửi thẳng userId, không xác minh —
chấp nhận được vì chỉ ghi số liệu thống kê), các thao tác LMS mới đụng vào
quyền hạn thật (tạo lớp, chấm điểm, cấp Premium...) nên cần xác minh danh
tính thật của người gọi API, không chỉ tin vào body JSON.

Cách làm: mọi request tới các API LMS nhạy cảm phải kèm header
    Authorization: Bearer <Firebase ID token>
lấy từ `await window._currentUser.getIdToken()` phía client. Ta dùng
firebase_admin (đã init sẵn ở firestore_db.py) để verify token này — không
tốn thêm dependency, không tốn thêm project Firebase nào khác.

Với các API chỉ cần "đã đăng nhập, không cần role cụ thể" (ví dụ học sinh
xem lớp mình đã tham gia) vẫn dùng cùng cơ chế để lấy uid đáng tin cậy.
"""
from functools import wraps

from flask import request, jsonify
from firebase_admin import auth as fb_auth

import lms_db
import admin_auth


def _extract_token() -> str:
    header = request.headers.get('Authorization', '')
    if header.startswith('Bearer '):
        return header[7:].strip()
    return ''


def current_user() -> dict | None:
    """Verify Firebase ID token của request hiện tại, trả về user Firestore
    (kèm role/plan, tạo mặc định nếu chưa có) hoặc None nếu token invalid."""
    token = _extract_token()
    if not token:
        return None
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception:
        return None
    uid = decoded.get('uid')
    if not uid:
        return None
    user = lms_db.ensure_user_defaults(
        uid, display_name=decoded.get('name', ''), email=decoded.get('email', '')
    )
    if user.get('accountDisabled'):
        return None
    return user


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({'error': 'Chưa đăng nhập hoặc token không hợp lệ.'}), 401
        request.lms_user = user
        return fn(*args, **kwargs)
    return wrapper


def require_role(*roles):
    """Ví dụ: @require_role('teacher', 'admin')"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify({'error': 'Chưa đăng nhập hoặc token không hợp lệ.'}), 401
            if user.get('role') not in roles:
                return jsonify({'error': 'Bạn không có quyền truy cập tính năng này.'}), 403
            request.lms_user = user
            return fn(*args, **kwargs)
        return wrapper
    return deco


def require_admin_token(fn):
    """Dùng cho các API '/api/lms/admin/*' — đây là hành động của TRANG ADMIN
    (admin.html), vốn đăng nhập bằng tài khoản admin SQLite riêng
    (admin_auth.py, header X-Admin-Token), KHÔNG phải Firebase Auth như
    học sinh/giáo viên. Dùng chung cơ chế với mọi route /api/admin/* khác
    trong server.py để nhất quán 1 hệ thống đăng nhập admin duy nhất."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get('X-Admin-Token', '')
        session = admin_auth.verify(token)
        if not session:
            return jsonify({'error': 'Cần đăng nhập admin.'}), 401
        request.admin_session = session
        return fn(*args, **kwargs)
    return wrapper


def require_class_owner_or_admin(fn):
    """Dùng cho route có <class_id> trong URL — chỉ giáo viên chủ nhiệm lớp
    đó hoặc admin mới được thao tác (sửa/xoá lớp, tạo bài học/bài tập...)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({'error': 'Chưa đăng nhập hoặc token không hợp lệ.'}), 401
        class_id = kwargs.get('class_id')
        if user.get('role') != 'admin':
            cls = lms_db.get_class(class_id) if class_id else None
            if not cls or cls.get('teacherId') != user.get('uid'):
                return jsonify({'error': 'Bạn không phải giáo viên chủ nhiệm lớp này.'}), 403
        request.lms_user = user
        return fn(*args, **kwargs)
    return wrapper
