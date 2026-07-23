"""
lms_routes.py — API cho hệ thống LMS mới (Phần I–IV của bản yêu cầu nâng cấp):
phân quyền, lớp học theo khối, bài học/tài liệu/bài tập/bài kiểm tra,
livestream (nhúng YouTube/Facebook Live), freemium usage limits, và các
API quản trị bổ sung cho admin dashboard.

Cách gắn vào server.py hiện tại (KHÔNG đổi cấu trúc file cũ):

    import lms_routes
    lms_routes.register(app)

Đặt ngay sau dòng `CORS(app)` / trước hoặc sau `db.init_db()` đều được —
blueprint không phụ thuộc thứ tự khởi tạo đó, chỉ cần fsdb.init() đã chạy
(đã có sẵn trong server.py) trước khi có request đầu tiên tới các route này.
"""
from flask import Blueprint, request, jsonify, Response

import lms_db
import lms_auth
import firestore_db as fsdb
import ai_pdf_export

bp = Blueprint('lms', __name__, url_prefix='/api/lms')


def register(app):
    app.register_blueprint(bp)


# ══════════════════════════════════════════════════════════════════════
# TÀI KHOẢN / USAGE
# ══════════════════════════════════════════════════════════════════════

@bp.route('/me', methods=['GET'])
@lms_auth.require_auth
def me():
    u = request.lms_user
    return jsonify({
        'uid': u['uid'], 'role': u.get('role'), 'plan': u.get('plan'),
        'planStatus': u.get('planStatus'), 'educationPlan': u.get('educationPlan'),
        'educationPlanStatus': u.get('educationPlanStatus'),
        'studentPlan': u.get('studentPlan'), 'studentPlanStatus': u.get('studentPlanStatus'),
        'studentPlanExpiresAt': u.get('studentPlanExpiresAt'),
        'usage': lms_db.usage_snapshot(u['uid']),
    })


@bp.route('/usage', methods=['GET'])
@lms_auth.require_auth
def usage():
    return jsonify(lms_db.usage_snapshot(request.lms_user['uid']))


@bp.route('/me/weak-topics', methods=['GET'])
@lms_auth.require_auth
def my_weak_topics():
    """Thống kê điểm yếu theo chuyên đề của chính học sinh đang đăng nhập (#2)."""
    return jsonify(lms_db.student_weak_topics(request.lms_user['uid']))


# ══════════════════════════════════════════════════════════════════════
# LỊCH SỬ AI KHÔNG GIỚI HẠN + XUẤT PDF (#5)
# ══════════════════════════════════════════════════════════════════════
# Dữ liệu ai_conversations/ai_messages đã được lưu VĨNH VIỄN trên Firestore
# từ trước (xem firestore_db.py) — cái CHƯA có là 1 API để chính học sinh
# xem lại lịch sử của mình (trước đây chỉ admin xem được qua trang quản trị)
# và xuất PDF lời giải. Không có giới hạn Free/Premium ở đây theo đúng yêu
# cầu #5 ("Lưu lịch sử AI không giới hạn") — mọi học sinh xem được TOÀN BỘ
# lịch sử của chính mình, bất kể gói.

@bp.route('/my-ai-conversations', methods=['GET'])
@lms_auth.require_auth
def my_ai_conversations():
    uid = request.lms_user['uid']
    items = fsdb.list_conversations_for_user(uid)
    return jsonify({'conversations': items})


@bp.route('/my-ai-conversations/<cid>', methods=['GET'])
@lms_auth.require_auth
def my_ai_conversation_detail(cid):
    uid = request.lms_user['uid']
    conv = fsdb.get_conversation(cid)
    if not conv or conv.get('user_id') != uid:
        return jsonify({'error': 'Không tìm thấy hội thoại.'}), 404
    messages = fsdb.list_messages(cid)
    return jsonify({'conversation': conv, 'messages': messages})


@bp.route('/my-ai-conversations/<cid>/export-pdf', methods=['GET'])
@lms_auth.require_auth
def my_ai_conversation_export_pdf(cid):
    uid = request.lms_user['uid']
    conv = fsdb.get_conversation(cid)
    if not conv or conv.get('user_id') != uid:
        return jsonify({'error': 'Không tìm thấy hội thoại.'}), 404
    messages = fsdb.list_messages(cid)
    try:
        pdf_bytes = ai_pdf_export.build_conversation_pdf(conv, messages)
    except Exception as e:
        return jsonify({'error': f'Không tạo được PDF: {e}'}), 500
    return Response(pdf_bytes, mimetype='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="chemcraft-ai-{cid}.pdf"'
    })


# ══════════════════════════════════════════════════════════════════════
# CLASSES
# ══════════════════════════════════════════════════════════════════════

@bp.route('/classes', methods=['POST'])
@lms_auth.require_role('teacher', 'admin')
def create_class():
    b = request.get_json(silent=True) or {}
    name = (b.get('name') or '').strip()
    grade = b.get('grade')
    if not name or grade not in (10, 11, 12):
        return jsonify({'error': 'Thiếu tên lớp hoặc khối lớp không hợp lệ (10/11/12).'}), 400
    cid = lms_db.create_class(
        request.lms_user['uid'], name, grade,
        description=b.get('description', ''), image_url=b.get('imageURL', ''),
        banner_url=b.get('bannerURL', ''),
    )
    return jsonify({'id': cid, 'class': lms_db.get_class(cid)})


@bp.route('/classes/browse', methods=['GET'])
@lms_auth.require_auth
def browse_classes():
    grade = request.args.get('grade', type=int)
    if grade not in (10, 11, 12):
        return jsonify({'error': 'Thiếu tham số grade (10/11/12).'}), 400
    items = [c for c in lms_db.list_classes(grade=grade) if c.get('status') == 'active']
    return jsonify({'classes': items})


@bp.route('/classes/mine', methods=['GET'])
@lms_auth.require_auth
def my_classes():
    u = request.lms_user
    if u.get('role') == 'teacher':
        items = lms_db.list_classes(teacher_id=u['uid'])
    else:
        items = lms_db.list_classes(student_id=u['uid'])
    return jsonify({'classes': items})


@bp.route('/classes/<class_id>', methods=['GET'])
@lms_auth.require_auth
def get_class(class_id):
    u = request.lms_user
    cls = lms_db.get_class(class_id)
    if not cls:
        return jsonify({'error': 'Không tìm thấy lớp học.'}), 404
    is_member = u['uid'] in cls.get('studentIds', []) or cls.get('teacherId') == u['uid']
    if u.get('role') != 'admin' and not is_member:
        return jsonify({'error': 'Bạn chưa tham gia lớp học này.'}), 403
    return jsonify({'class': cls})


@bp.route('/classes/<class_id>', methods=['PUT'])
@lms_auth.require_class_owner_or_admin
def update_class(class_id):
    b = request.get_json(silent=True) or {}
    allowed = {k: v for k, v in b.items() if k in
               ('name', 'description', 'imageURL', 'bannerURL', 'status')}
    if not lms_db.update_class(class_id, **allowed):
        return jsonify({'error': 'Không tìm thấy lớp học.'}), 404
    return jsonify({'ok': True, 'class': lms_db.get_class(class_id)})


@bp.route('/classes/<class_id>', methods=['DELETE'])
@lms_auth.require_class_owner_or_admin
def delete_class(class_id):
    if not lms_db.delete_class(class_id):
        return jsonify({'error': 'Không tìm thấy lớp học.'}), 404
    return jsonify({'ok': True})


@bp.route('/classes/join', methods=['POST'])
@lms_auth.require_auth
def join_class():
    b = request.get_json(silent=True) or {}
    code = (b.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'Thiếu mã lớp.'}), 400
    cls = lms_db.join_class_by_code(request.lms_user['uid'], code)
    if not cls:
        return jsonify({'error': 'Mã lớp không đúng hoặc lớp không tồn tại.'}), 404
    return jsonify({'ok': True, 'class': cls})


@bp.route('/classes/<class_id>/students', methods=['GET'])
@lms_auth.require_class_owner_or_admin
def list_students(class_id):
    return jsonify({'students': lms_db.list_students(class_id)})


@bp.route('/classes/<class_id>/students', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def add_student(class_id):
    b = request.get_json(silent=True) or {}
    sid = (b.get('studentId') or '').strip()
    if not sid or not lms_db.add_student(class_id, sid):
        return jsonify({'error': 'Thiếu studentId hoặc lớp không tồn tại.'}), 400
    return jsonify({'ok': True})


@bp.route('/classes/<class_id>/students/<student_id>', methods=['DELETE'])
@lms_auth.require_class_owner_or_admin
def remove_student(class_id, student_id):
    lms_db.remove_student(class_id, student_id)
    return jsonify({'ok': True})


def _student_in_class(class_id: str, student_id: str) -> bool:
    cls = lms_db.get_class(class_id)
    return bool(cls) and student_id in (cls.get('studentIds') or [])


# Giáo viên tặng Premium riêng cho 1 học sinh trong lớp mình chủ nhiệm (#3).
# Giới hạn số ngày tối đa 1 lần tặng để tránh giáo viên tặng vĩnh viễn không
# kiểm soát — nếu cần tặng lại/gia hạn, giáo viên gọi lại API này.
GIFT_PREMIUM_MAX_DAYS = 90


@bp.route('/classes/<class_id>/students/<student_id>/gift-premium', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def gift_student_premium(class_id, student_id):
    if not _student_in_class(class_id, student_id):
        return jsonify({'error': 'Học sinh này không thuộc lớp học này.'}), 400
    b = request.get_json(silent=True) or {}
    try:
        days = int(b.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, GIFT_PREMIUM_MAX_DAYS))
    lms_db.grant_student_premium(student_id, request.lms_user['uid'], days=days)
    return jsonify({'ok': True, 'days': days})


@bp.route('/classes/<class_id>/students/<student_id>/gift-premium', methods=['DELETE'])
@lms_auth.require_class_owner_or_admin
def revoke_gifted_student_premium(class_id, student_id):
    if not _student_in_class(class_id, student_id):
        return jsonify({'error': 'Học sinh này không thuộc lớp học này.'}), 400
    lms_db.revoke_student_premium(student_id)
    return jsonify({'ok': True})


@bp.route('/classes/<class_id>/students/<student_id>/weak-topics', methods=['GET'])
@lms_auth.require_class_owner_or_admin
def student_weak_topics_for_teacher(class_id, student_id):
    """Giáo viên xem thống kê điểm yếu theo chuyên đề của 1 học sinh trong
    lớp mình — dùng cho báo cáo học sinh (#3, phần 'xem báo cáo học sinh')."""
    if not _student_in_class(class_id, student_id):
        return jsonify({'error': 'Học sinh này không thuộc lớp học này.'}), 400
    return jsonify(lms_db.student_weak_topics(student_id))


# ══════════════════════════════════════════════════════════════════════
# LESSONS
# ══════════════════════════════════════════════════════════════════════

@bp.route('/classes/<class_id>/lessons', methods=['GET'])
@lms_auth.require_auth
def list_lessons(class_id):
    u = request.lms_user
    cls = lms_db.get_class(class_id)
    if not cls:
        return jsonify({'error': 'Không tìm thấy lớp học.'}), 404
    is_member = u['uid'] in cls.get('studentIds', []) or cls.get('teacherId') == u['uid']
    if u.get('role') != 'admin' and not is_member:
        return jsonify({'error': 'Bạn chưa tham gia lớp học này.'}), 403
    lessons = lms_db.list_lessons(class_id)
    if u.get('role') == 'student':
        done = lms_db.list_completed_lessons(u['uid'])
        for l in lessons:
            l['completed'] = l['id'] in done
    return jsonify({'lessons': lessons})


@bp.route('/classes/<class_id>/lessons', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def create_lesson(class_id):
    b = request.get_json(silent=True) or {}
    title = (b.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Thiếu tiêu đề bài học.'}), 400
    lid = lms_db.create_lesson(
        class_id, request.lms_user['uid'], title, chapter=b.get('chapter', ''),
        content=b.get('content', ''), attachments=b.get('attachments', []),
        order=b.get('order', 0),
    )
    return jsonify({'id': lid, 'lesson': lms_db.get_lesson(lid)})


@bp.route('/lessons/<lesson_id>', methods=['PUT'])
@lms_auth.require_auth
def update_lesson(lesson_id):
    lesson = lms_db.get_lesson(lesson_id)
    if not lesson:
        return jsonify({'error': 'Không tìm thấy bài học.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and lesson.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    b = request.get_json(silent=True) or {}
    allowed = {k: v for k, v in b.items() if k in ('title', 'chapter', 'content', 'attachments', 'order')}
    lms_db.update_lesson(lesson_id, **allowed)
    return jsonify({'ok': True, 'lesson': lms_db.get_lesson(lesson_id)})


@bp.route('/lessons/<lesson_id>', methods=['DELETE'])
@lms_auth.require_auth
def delete_lesson(lesson_id):
    lesson = lms_db.get_lesson(lesson_id)
    if not lesson:
        return jsonify({'error': 'Không tìm thấy bài học.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and lesson.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    lms_db.delete_lesson(lesson_id)
    return jsonify({'ok': True})


@bp.route('/lessons/<lesson_id>/complete', methods=['POST'])
@lms_auth.require_auth
def complete_lesson(lesson_id):
    lms_db.mark_lesson_complete(request.lms_user['uid'], lesson_id)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# DOCUMENTS
# ══════════════════════════════════════════════════════════════════════

@bp.route('/classes/<class_id>/documents', methods=['GET'])
@lms_auth.require_auth
def list_documents(class_id):
    lesson_id = request.args.get('lessonId')
    return jsonify({'documents': lms_db.list_documents(class_id, lesson_id)})


@bp.route('/classes/<class_id>/documents', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def create_document(class_id):
    """Client upload file thẳng lên Firebase Storage bằng SDK (giống cách
    index.html đã làm với ảnh đại diện), rồi gọi API này để lưu metadata +
    URL công khai — backend không xử lý file nhị phân."""
    b = request.get_json(silent=True) or {}
    title = (b.get('title') or '').strip()
    file_url = (b.get('fileURL') or '').strip()
    if not title or not file_url:
        return jsonify({'error': 'Thiếu tiêu đề hoặc fileURL.'}), 400
    did = lms_db.create_document(
        class_id, request.lms_user['uid'], title, b.get('description', ''), file_url,
        b.get('fileType', ''), b.get('fileSize', 0), lesson_id=b.get('lessonId'),
    )
    return jsonify({'id': did})


@bp.route('/documents/<document_id>', methods=['DELETE'])
@lms_auth.require_auth
def delete_document(document_id):
    u = request.lms_user
    if u.get('role') not in ('teacher', 'admin'):
        return jsonify({'error': 'Không có quyền.'}), 403
    lms_db.delete_document(document_id)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# ASSIGNMENTS + SUBMISSIONS
# ══════════════════════════════════════════════════════════════════════

@bp.route('/classes/<class_id>/assignments', methods=['GET'])
@lms_auth.require_auth
def list_assignments(class_id):
    return jsonify({'assignments': lms_db.list_assignments(class_id)})


@bp.route('/classes/<class_id>/assignments', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def create_assignment(class_id):
    b = request.get_json(silent=True) or {}
    title = (b.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Thiếu tiêu đề bài tập.'}), 400
    aid = lms_db.create_assignment(
        class_id, request.lms_user['uid'], title, b.get('description', ''),
        b.get('type', 'text'), b.get('deadline', 0), b.get('maxScore', 10),
        lesson_id=b.get('lessonId'), attachments=b.get('attachments', []),
    )
    return jsonify({'id': aid, 'assignment': lms_db.get_assignment(aid)})


@bp.route('/assignments/<assignment_id>', methods=['PUT'])
@lms_auth.require_auth
def update_assignment(assignment_id):
    a = lms_db.get_assignment(assignment_id)
    if not a:
        return jsonify({'error': 'Không tìm thấy bài tập.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and a.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    b = request.get_json(silent=True) or {}
    allowed = {k: v for k, v in b.items() if k in
               ('title', 'description', 'type', 'deadline', 'maxScore', 'attachments')}
    lms_db.update_assignment(assignment_id, **allowed)
    return jsonify({'ok': True})


@bp.route('/assignments/<assignment_id>', methods=['DELETE'])
@lms_auth.require_auth
def delete_assignment(assignment_id):
    a = lms_db.get_assignment(assignment_id)
    if not a:
        return jsonify({'error': 'Không tìm thấy bài tập.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and a.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    lms_db.delete_assignment(assignment_id)
    return jsonify({'ok': True})


@bp.route('/assignments/<assignment_id>/my-submission', methods=['GET'])
@lms_auth.require_auth
def my_submission(assignment_id):
    sub = lms_db.get_student_submission(assignment_id, request.lms_user['uid'])
    return jsonify({'submission': sub})


@bp.route('/assignments/<assignment_id>/submit', methods=['POST'])
@lms_auth.require_auth
def submit_assignment(assignment_id):
    u = request.lms_user
    a = lms_db.get_assignment(assignment_id)
    if not a:
        return jsonify({'error': 'Không tìm thấy bài tập.'}), 404
    import time as _t
    if a.get('deadline') and _t.time() > a['deadline']:
        existing = lms_db.get_student_submission(assignment_id, u['uid'])
        if existing:
            return jsonify({'error': 'Đã quá hạn nộp bài, không thể sửa bài đã nộp.'}), 403
        return jsonify({'error': 'Đã quá hạn nộp bài.'}), 403

    already_submitted = lms_db.get_student_submission(assignment_id, u['uid']) is not None
    if not already_submitted:
        ok, info = lms_db.try_consume_submission_usage(u['uid'])
        if not ok:
            return jsonify({
                'error': f"Bạn đã đạt giới hạn {info['limit']} lần nộp bài trong ngày hôm nay. "
                         f"Nâng cấp ChemCraft for edu để nộp bài không giới hạn.",
                'limitReached': True,
            }), 429

    b = request.get_json(silent=True) or {}
    sid = lms_db.create_submission(
        assignment_id, u['uid'], a['classId'], a['teacherId'],
        b.get('content', ''), b.get('files', []),
    )
    return jsonify({'ok': True, 'submissionId': sid})


@bp.route('/assignments/<assignment_id>/submissions', methods=['GET'])
@lms_auth.require_auth
def list_assignment_submissions(assignment_id):
    a = lms_db.get_assignment(assignment_id)
    if not a:
        return jsonify({'error': 'Không tìm thấy bài tập.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and a.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    return jsonify({'submissions': lms_db.list_submissions(assignment_id=assignment_id)})


@bp.route('/submissions/<submission_id>/grade', methods=['POST'])
@lms_auth.require_role('teacher', 'admin')
def grade_submission(submission_id):
    b = request.get_json(silent=True) or {}
    score = b.get('score')
    if score is None:
        return jsonify({'error': 'Thiếu điểm số.'}), 400
    if not lms_db.grade_submission(submission_id, float(score), b.get('feedback', '')):
        return jsonify({'error': 'Không tìm thấy bài nộp.'}), 404
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════

@bp.route('/classes/<class_id>/tests', methods=['GET'])
@lms_auth.require_auth
def list_tests(class_id):
    return jsonify({'tests': lms_db.list_tests(class_id)})


@bp.route('/classes/<class_id>/tests', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def create_test(class_id):
    b = request.get_json(silent=True) or {}
    title = (b.get('title') or '').strip()
    questions = b.get('questions') or []
    if not title or not questions:
        return jsonify({'error': 'Thiếu tiêu đề hoặc danh sách câu hỏi.'}), 400
    tid = lms_db.create_test(class_id, request.lms_user['uid'], title,
                              b.get('description', ''), questions, b.get('deadline', 0),
                              settings=b.get('settings'))
    return jsonify({'id': tid, 'test': lms_db.get_test(tid)})


@bp.route('/classes/<class_id>/tests/parse-text', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def parse_test_text(class_id):
    """Giáo viên dán toàn bộ đề (kiểu Azota) vào 1 ô — trả về preview danh
    sách câu hỏi đã phân tích để chỉnh sửa trước khi lưu thật (không ghi
    Firestore ở bước này)."""
    b = request.get_json(silent=True) or {}
    raw_text = b.get('text') or ''
    if not raw_text.strip():
        return jsonify({'error': 'Thiếu nội dung đề để phân tích.'}), 400
    questions = lms_db.parse_azota_text(raw_text)
    if not questions:
        return jsonify({'error': 'Không nhận diện được câu hỏi nào — kiểm tra lại cú pháp "Câu N."'}), 400
    return jsonify({'questions': questions})


@bp.route('/tests/<test_id>', methods=['GET'])
@lms_auth.require_auth
def get_test(test_id):
    u = request.lms_user
    for_student = u.get('role') == 'student'
    t = lms_db.get_test(test_id, for_student=for_student)
    if not t:
        return jsonify({'error': 'Không tìm thấy bài kiểm tra.'}), 404
    return jsonify({'test': t})


@bp.route('/tests/<test_id>', methods=['PUT'])
@lms_auth.require_auth
def update_test(test_id):
    t = lms_db.get_test(test_id)
    if not t:
        return jsonify({'error': 'Không tìm thấy bài kiểm tra.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and t.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    b = request.get_json(silent=True) or {}
    allowed = {k: v for k, v in b.items() if k in ('title', 'description', 'questions', 'deadline', 'settings')}
    lms_db.update_test(test_id, **allowed)
    return jsonify({'ok': True})


@bp.route('/tests/<test_id>/publish-results', methods=['POST'])
@lms_auth.require_auth
def publish_test_results(test_id):
    t = lms_db.get_test(test_id)
    if not t:
        return jsonify({'error': 'Không tìm thấy bài kiểm tra.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and t.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    b = request.get_json(silent=True) or {}
    lms_db.publish_test_results(test_id, bool(b.get('published', True)))
    return jsonify({'ok': True})


@bp.route('/tests/<test_id>', methods=['DELETE'])
@lms_auth.require_auth
def delete_test(test_id):
    t = lms_db.get_test(test_id)
    if not t:
        return jsonify({'error': 'Không tìm thấy bài kiểm tra.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and t.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    lms_db.delete_test(test_id)
    return jsonify({'ok': True})


@bp.route('/tests/<test_id>/submit', methods=['POST'])
@lms_auth.require_auth
def submit_test(test_id):
    u = request.lms_user
    b = request.get_json(silent=True) or {}
    answers = b.get('answers') or []
    try:
        attempt_id = lms_db.submit_test_attempt(test_id, u['uid'], answers)
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'ok': True, 'attemptId': attempt_id})


@bp.route('/tests/<test_id>/attempts', methods=['GET'])
@lms_auth.require_auth
def list_test_attempts(test_id):
    t = lms_db.get_test(test_id)
    if not t:
        return jsonify({'error': 'Không tìm thấy bài kiểm tra.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and t.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    return jsonify({'attempts': lms_db.list_test_attempts(test_id=test_id)})


@bp.route('/my-test-attempts', methods=['GET'])
@lms_auth.require_auth
def my_test_attempts():
    return jsonify({'attempts': lms_db.list_test_attempts(student_id=request.lms_user['uid'])})


@bp.route('/test-attempts/<attempt_id>', methods=['GET'])
@lms_auth.require_auth
def get_test_attempt_detail(attempt_id):
    """Học sinh xem lại bài đã làm (đề + đáp án nếu settings cho phép);
    giáo viên/admin luôn xem được để chấm."""
    attempt = lms_db.get_test_attempt(attempt_id)
    if not attempt:
        return jsonify({'error': 'Không tìm thấy lượt làm bài.'}), 404
    u = request.lms_user
    is_owner = attempt.get('studentId') == u['uid']
    is_teacher = u.get('role') == 'admin' or attempt.get('teacherId') == u['uid']
    if not is_owner and not is_teacher:
        return jsonify({'error': 'Không có quyền xem bài làm này.'}), 403
    data = lms_db.get_test_attempt_for_review(attempt_id)
    if is_teacher:
        data['revealAnswers'] = True
        test = lms_db.get_test(attempt['testId'])
        if test:
            data['questions'] = test.get('questions', [])
    return jsonify({'attempt': data})


@bp.route('/test-attempts/<attempt_id>/grade-essay', methods=['POST'])
@lms_auth.require_role('teacher', 'admin')
def grade_essay(attempt_id):
    b = request.get_json(silent=True) or {}
    qi = b.get('questionIndex')
    score = b.get('score')
    if qi is None or score is None:
        return jsonify({'error': 'Thiếu questionIndex hoặc score.'}), 400
    if not lms_db.grade_essay_answer(attempt_id, int(qi), float(score)):
        return jsonify({'error': 'Không tìm thấy lượt làm bài.'}), 404
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# LIVESTREAMS (nhúng YouTube Live / Facebook Live) + chat
# ══════════════════════════════════════════════════════════════════════

@bp.route('/classes/<class_id>/livestreams', methods=['GET'])
@lms_auth.require_auth
def list_livestreams(class_id):
    return jsonify({'livestreams': lms_db.list_livestreams(class_id)})


@bp.route('/classes/<class_id>/livestreams', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def create_livestream(class_id):
    b = request.get_json(silent=True) or {}
    title = (b.get('title') or '').strip()
    embed_url = (b.get('embedURL') or '').strip()
    if not title or not embed_url:
        return jsonify({'error': 'Thiếu tiêu đề hoặc link nhúng YouTube/Facebook Live.'}), 400
    lid = lms_db.create_livestream(class_id, request.lms_user['uid'], title,
                                    b.get('description', ''), b.get('scheduledAt', 0), embed_url)
    return jsonify({'id': lid, 'livestream': lms_db.get_livestream(lid)})


@bp.route('/livestreams/<livestream_id>', methods=['PUT'])
@lms_auth.require_auth
def update_livestream(livestream_id):
    ls = lms_db.get_livestream(livestream_id)
    if not ls:
        return jsonify({'error': 'Không tìm thấy buổi livestream.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and ls.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    b = request.get_json(silent=True) or {}
    allowed = {k: v for k, v in b.items() if k in
               ('title', 'description', 'scheduledAt', 'embedURL')}
    lms_db.update_livestream(livestream_id, **allowed)
    return jsonify({'ok': True})


@bp.route('/livestreams/<livestream_id>/status', methods=['POST'])
@lms_auth.require_auth
def set_livestream_status(livestream_id):
    ls = lms_db.get_livestream(livestream_id)
    if not ls:
        return jsonify({'error': 'Không tìm thấy buổi livestream.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and ls.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    b = request.get_json(silent=True) or {}
    status = b.get('status')
    if not lms_db.set_livestream_status(livestream_id, status, recording_url=b.get('recordingURL')):
        return jsonify({'error': 'Trạng thái không hợp lệ.'}), 400
    return jsonify({'ok': True})


@bp.route('/livestreams/<livestream_id>', methods=['DELETE'])
@lms_auth.require_auth
def delete_livestream(livestream_id):
    ls = lms_db.get_livestream(livestream_id)
    if not ls:
        return jsonify({'error': 'Không tìm thấy buổi livestream.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and ls.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    lms_db.delete_livestream(livestream_id)
    return jsonify({'ok': True})


@bp.route('/livestreams/<livestream_id>/chat', methods=['GET'])
@lms_auth.require_auth
def get_chat(livestream_id):
    return jsonify({'messages': lms_db.list_chat_messages(livestream_id)})


@bp.route('/livestreams/<livestream_id>/chat', methods=['POST'])
@lms_auth.require_auth
def post_chat(livestream_id):
    u = request.lms_user
    b = request.get_json(silent=True) or {}
    msg = (b.get('message') or '').strip()
    if not msg:
        return jsonify({'error': 'Tin nhắn trống.'}), 400
    ls = lms_db.get_livestream(livestream_id)
    if not ls:
        return jsonify({'error': 'Không tìm thấy buổi livestream.'}), 404
    if ls.get('status') == 'ended':
        return jsonify({'error': 'Livestream đã kết thúc, không thể chat.'}), 403
    mid = lms_db.add_chat_message(livestream_id, u['uid'], u.get('displayName', 'Học sinh'), msg)
    return jsonify({'ok': True, 'id': mid})


@bp.route('/livestreams/<livestream_id>/chat/<message_id>', methods=['DELETE'])
@lms_auth.require_auth
def delete_chat(livestream_id, message_id):
    ls = lms_db.get_livestream(livestream_id)
    if not ls:
        return jsonify({'error': 'Không tìm thấy buổi livestream.'}), 404
    u = request.lms_user
    if u.get('role') != 'admin' and ls.get('teacherId') != u['uid']:
        return jsonify({'error': 'Không có quyền.'}), 403
    lms_db.delete_chat_message(livestream_id, message_id)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# CHAT TRỰC TIẾP HỌC SINH ↔ GIÁO VIÊN (theo lớp)
# ══════════════════════════════════════════════════════════════════════
# Khác livestream chat (công khai, chỉ tồn tại trong buổi live): đây là
# kênh riêng tư 1-1 giữa 1 học sinh và giáo viên chủ nhiệm lớp, tồn tại lâu
# dài (không gắn với buổi học nào), dùng cho hỏi bài/trao đổi ngoài giờ.

@bp.route('/classes/<class_id>/chat', methods=['GET'])
@lms_auth.require_auth
def student_get_class_chat(class_id):
    """Học sinh xem hội thoại của CHÍNH MÌNH với giáo viên chủ nhiệm lớp này."""
    u = request.lms_user
    cls = lms_db.get_class(class_id)
    if not cls:
        return jsonify({'error': 'Không tìm thấy lớp học.'}), 404
    if u['uid'] not in (cls.get('studentIds') or []):
        return jsonify({'error': 'Bạn không thuộc lớp học này.'}), 403
    messages = lms_db.list_class_chat_messages(class_id, u['uid'])
    lms_db.mark_class_chat_read(class_id, u['uid'], reader_role='student')
    return jsonify({'messages': messages, 'teacherId': cls.get('teacherId')})


@bp.route('/classes/<class_id>/chat', methods=['POST'])
@lms_auth.require_auth
def student_post_class_chat(class_id):
    u = request.lms_user
    cls = lms_db.get_class(class_id)
    if not cls:
        return jsonify({'error': 'Không tìm thấy lớp học.'}), 404
    if u['uid'] not in (cls.get('studentIds') or []):
        return jsonify({'error': 'Bạn không thuộc lớp học này.'}), 403
    b = request.get_json(silent=True) or {}
    try:
        mid = lms_db.send_class_chat_message(
            class_id, u['uid'], sender_id=u['uid'], sender_role='student',
            sender_name=u.get('displayName', 'Học sinh'), message=b.get('message', '')
        )
    except ValueError:
        return jsonify({'error': 'Tin nhắn trống.'}), 400
    return jsonify({'ok': True, 'id': mid})


@bp.route('/classes/<class_id>/students/<student_id>/chat', methods=['GET'])
@lms_auth.require_class_owner_or_admin
def teacher_get_class_chat(class_id, student_id):
    """Giáo viên (hoặc admin) xem hội thoại với 1 học sinh cụ thể trong lớp."""
    cls = lms_db.get_class(class_id)
    if not cls or student_id not in (cls.get('studentIds') or []):
        return jsonify({'error': 'Học sinh này không thuộc lớp học này.'}), 400
    messages = lms_db.list_class_chat_messages(class_id, student_id)
    lms_db.mark_class_chat_read(class_id, student_id, reader_role='teacher')
    return jsonify({'messages': messages})


@bp.route('/classes/<class_id>/students/<student_id>/chat', methods=['POST'])
@lms_auth.require_class_owner_or_admin
def teacher_post_class_chat(class_id, student_id):
    cls = lms_db.get_class(class_id)
    if not cls or student_id not in (cls.get('studentIds') or []):
        return jsonify({'error': 'Học sinh này không thuộc lớp học này.'}), 400
    u = request.lms_user
    b = request.get_json(silent=True) or {}
    try:
        mid = lms_db.send_class_chat_message(
            class_id, student_id, sender_id=u['uid'], sender_role='teacher',
            sender_name=u.get('displayName', 'Giáo viên'), message=b.get('message', '')
        )
    except ValueError:
        return jsonify({'error': 'Tin nhắn trống.'}), 400
    return jsonify({'ok': True, 'id': mid})


@bp.route('/teacher/chat-threads', methods=['GET'])
@lms_auth.require_role('teacher', 'admin')
def teacher_chat_threads():
    """Hộp thư: toàn bộ hội thoại của các học sinh trong các lớp giáo viên
    này chủ nhiệm, mới nhất lên đầu — dùng cho tab 'Tin nhắn' trong teacher.html."""
    return jsonify({'threads': lms_db.list_chat_threads_for_teacher(request.lms_user['uid'])})


# ══════════════════════════════════════════════════════════════════════
# ADMIN — quản lý giáo viên / premium / thống kê
# ══════════════════════════════════════════════════════════════════════

@bp.route('/admin/teachers', methods=['GET'])
@lms_auth.require_admin_token
def admin_list_teachers():
    return jsonify({'teachers': lms_db.list_teachers()})


@bp.route('/admin/users/<uid>/role', methods=['POST'])
@lms_auth.require_admin_token
def admin_set_role(uid):
    b = request.get_json(silent=True) or {}
    role = b.get('role')
    if not lms_db.set_role(uid, role):
        return jsonify({'error': "role phải là 'student', 'teacher' hoặc 'admin'."}), 400
    return jsonify({'ok': True})


@bp.route('/admin/users/<uid>/active', methods=['POST'])
@lms_auth.require_admin_token
def admin_set_active(uid):
    b = request.get_json(silent=True) or {}
    lms_db.set_account_active(uid, bool(b.get('active', True)))
    return jsonify({'ok': True})


@bp.route('/admin/teachers/<uid>/premium', methods=['POST'])
@lms_auth.require_admin_token
def admin_set_premium(uid):
    b = request.get_json(silent=True) or {}
    lms_db.set_teacher_plan(uid, bool(b.get('active', True)))
    return jsonify({'ok': True})


@bp.route('/admin/students/<uid>/premium', methods=['POST'])
@lms_auth.require_admin_token
def admin_set_student_premium(uid):
    """Admin cấp/thu hồi Premium cho 1 học sinh BẤT KỲ, không phụ thuộc lớp
    học/giáo viên nào (#1) — khác với gift-premium của giáo viên (chỉ trong
    phạm vi lớp mình chủ nhiệm, có hạn số ngày)."""
    b = request.get_json(silent=True) or {}
    if bool(b.get('active', True)):
        days = b.get('days')  # None = vĩnh viễn, admin được phép cấp không hạn
        try:
            days = int(days) if days is not None else None
        except (TypeError, ValueError):
            days = None
        lms_db.grant_student_premium(uid, 'admin', days=days)
    else:
        lms_db.revoke_student_premium(uid)
    return jsonify({'ok': True})


@bp.route('/admin/classes', methods=['GET'])
@lms_auth.require_admin_token
def admin_list_classes():
    return jsonify({'classes': lms_db.list_classes()})


@bp.route('/admin/stats', methods=['GET'])
@lms_auth.require_admin_token
def admin_stats():
    return jsonify(lms_db.admin_lms_stats())
