"""
lms_db.py — Tầng dữ liệu Firestore cho hệ thống LMS mới của ChemCraft:
phân quyền (student/teacher/admin), lớp học, bài học theo khối, tài liệu,
bài tập/bài nộp, bài kiểm tra, livestream, và giới hạn sử dụng Free/Premium
("ChemCraft for edu").

Dùng chung 1 kết nối Firestore đã init ở firestore_db.py (fsdb.collection()),
KHÔNG gọi firebase_admin.initialize_app() lần nữa ở đây.

Collections mới:
  users                 (đã tồn tại, được index.html tạo lúc đăng ký — ở đây
                         chỉ BỔ SUNG field role/plan/usage, không đụng vào
                         field cũ như displayName/lessonHistory)
  classes
  class_lessons
  class_documents
  assignments
  submissions
  tests
  test_attempts
  livestreams
  livestreams/{id}/chat_messages   (subcollection)
"""
import random
import re
import string
import time

import firestore_db as fsdb

# ── Hằng số Freemium ─────────────────────────────────────────────────────
FREE_AI_LIMIT = 5
FREE_SUBMISSION_LIMIT = 2
EDU_PLAN = 'chemcraft_for_edu'
_stats_cache = {'ts': 0, 'data': None}
_STATS_TTL = 300  # 5 phút — cùng ý tưởng cache như /api/admin/users-stats

# ── Gói Premium RIÊNG cho học sinh (độc lập với sponsoredBy/educationPlan) ──
# Trước đây học sinh chỉ có Premium theo 1 cách DUY NHẤT: được "bảo trợ"
# (sponsoredBy) bởi 1 giáo viên đang có gói 'chemcraft_for_edu' — nếu giáo
# viên bị thu hồi Premium thì TOÀN BỘ học sinh trong lớp mất Premium theo,
# không có cách nào cấp Premium cho RIÊNG 1 học sinh (VD giáo viên tặng cho
# 1 em học giỏi, hoặc sau này học sinh tự mua).
#
# `studentPlan`/`studentPlanStatus`/`studentPlanExpiresAt`/`studentPlanGrantedBy`
# là các field MỚI, tách biệt hoàn toàn với `sponsoredBy`/`educationPlan`
# (cơ chế cũ theo lớp) — has_unlimited_access() coi user có Premium nếu
# THỎA MÃN BẤT KỲ cơ chế nào trong 2 cơ chế trên (OR), không cái nào ghi đè
# cái nào. Khi giáo viên bị thu hồi Premium, `_sync_sponsored_students` vẫn
# chỉ đụng vào educationPlan/sponsoredBy như cũ — không đụng vào studentPlan
# đã cấp riêng.
STUDENT_PREMIUM_PLAN = 'chemcraft_premium_student'


def _now() -> float:
    return time.time()


# ══════════════════════════════════════════════════════════════════════
# USERS — role / plan / usage
# ══════════════════════════════════════════════════════════════════════

def _today_str() -> str:
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() + 7 * 3600))  # giờ VN (UTC+7)


def get_user(uid: str) -> dict | None:
    if not uid:
        return None
    doc = fsdb.collection('users').document(uid).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d['uid'] = uid
    return d


def ensure_user_defaults(uid: str, display_name: str = '', email: str = '') -> dict:
    """Gọi mỗi khi cần đọc quyền của 1 user. Nếu user doc chưa có field
    role/plan (tài khoản tạo trước khi có LMS, hoặc vừa đăng ký lần đầu),
    merge thêm giá trị mặc định — KHÔNG ghi đè field đã có sẵn."""
    ref = fsdb.collection('users').document(uid)
    snap = ref.get()
    ts = time.time()
    if not snap.exists:
        data = {
            'displayName': display_name, 'email': email,
            'role': 'student', 'plan': 'free', 'planStatus': 'inactive',
            'sponsoredBy': None, 'aiUsageToday': 0, 'submissionUsageToday': 0,
            'usageDate': _today_str(), 'createdAt': ts, 'updatedAt': ts,
            'studentPlan': 'free', 'studentPlanStatus': 'inactive',
            'studentPlanExpiresAt': None, 'studentPlanGrantedBy': None,
        }
        ref.set(data)
        data['uid'] = uid
        return data
    d = snap.to_dict()
    patch = {}
    for key, default in (
        ('role', 'student'), ('plan', 'free'), ('planStatus', 'inactive'),
        ('sponsoredBy', None), ('aiUsageToday', 0), ('submissionUsageToday', 0),
        ('usageDate', _today_str()),
        ('studentPlan', 'free'), ('studentPlanStatus', 'inactive'),
        ('studentPlanExpiresAt', None), ('studentPlanGrantedBy', None),
    ):
        if key not in d:
            patch[key] = default
    if patch:
        patch['updatedAt'] = ts
        ref.update(patch)
        d.update(patch)
    d = _expire_student_plan_if_needed(uid, d)
    d['uid'] = uid
    return d


def _expire_student_plan_if_needed(uid: str, user: dict) -> dict:
    """Tự động hạ studentPlanStatus về 'inactive' nếu đã quá hạn
    studentPlanExpiresAt. Gọi mỗi lần đọc user (ensure_user_defaults) để
    Premium hết hạn tự mất đi mà không cần cron job riêng."""
    expires_at = user.get('studentPlanExpiresAt')
    if user.get('studentPlanStatus') == 'active' and expires_at and _now() > expires_at:
        fsdb.collection('users').document(uid).set({
            'studentPlanStatus': 'expired', 'updatedAt': _now(),
        }, merge=True)
        user['studentPlanStatus'] = 'expired'
    return user


def set_role(uid: str, role: str) -> bool:
    if role not in ('student', 'teacher', 'admin'):
        return False
    fsdb.collection('users').document(uid).set(
        {'role': role, 'updatedAt': time.time()}, merge=True
    )
    return True


def set_account_active(uid: str, active: bool) -> bool:
    fsdb.collection('users').document(uid).set(
        {'accountDisabled': (not active), 'updatedAt': time.time()}, merge=True
    )
    return True


def set_teacher_plan(uid: str, active: bool) -> bool:
    """Admin cấp/thu hồi gói 'ChemCraft for edu' cho 1 giáo viên. Khi thu hồi,
    cũng tự động hạ tất cả học sinh đang được giáo viên đó bảo trợ."""
    fsdb.collection('users').document(uid).set({
        'plan': EDU_PLAN if active else 'free',
        'planStatus': 'active' if active else 'inactive',
        'updatedAt': time.time(),
    }, merge=True)
    _sync_sponsored_students(uid, active)
    return True


def _sync_sponsored_students(teacher_id: str, teacher_active: bool):
    class_ids = [c['id'] for c in list_classes(teacher_id=teacher_id)]
    if not class_ids:
        return
    student_ids = set()
    for cid in class_ids:
        cls = get_class(cid)
        if cls:
            student_ids.update(cls.get('studentIds', []))
    for sid in student_ids:
        udoc = get_user(sid)
        # Không đụng vào học sinh đang được bảo trợ bởi 1 giáo viên KHÁC
        if udoc and udoc.get('sponsoredBy') not in (None, teacher_id):
            continue
        fsdb.collection('users').document(sid).set({
            'educationPlan': EDU_PLAN if teacher_active else None,
            'educationPlanStatus': 'active' if teacher_active else 'inactive',
            'sponsoredBy': teacher_id if teacher_active else None,
            'updatedAt': time.time(),
        }, merge=True)


def has_unlimited_access(user: dict) -> bool:
    if not user:
        return False
    if user.get('role') == 'admin':
        return True
    if (user.get('role') == 'teacher' and user.get('plan') == EDU_PLAN
            and user.get('planStatus') == 'active'):
        return True
    # Cơ chế 1 (cũ): học sinh được "bảo trợ" theo cả lớp vì giáo viên chủ
    # nhiệm đang có gói 'chemcraft_for_edu'.
    if (user.get('educationPlan') == EDU_PLAN
            and user.get('educationPlanStatus') == 'active'):
        return True
    # Cơ chế 2 (mới, #1): Premium cấp RIÊNG cho học sinh này — độc lập với
    # giáo viên/lớp học, có thể có hạn (studentPlanExpiresAt) hoặc vĩnh viễn
    # (studentPlanExpiresAt = None). Không kiểm tra hết hạn ở đây để tránh
    # ghi Firestore trên đường đọc nóng (hot path) — việc hạ trạng thái hết
    # hạn được xử lý lười (lazy) trong _expire_student_plan_if_needed(), gọi
    # từ ensure_user_defaults() mỗi khi user được load.
    if (user.get('studentPlanStatus') == 'active'
            and (not user.get('studentPlanExpiresAt') or user.get('studentPlanExpiresAt') > _now())):
        return True
    return False


def grant_student_premium(student_id: str, granted_by: str, days: int | None = 30) -> bool:
    """Cấp Premium riêng cho 1 học sinh, độc lập với sponsoredBy/lớp học.
    `granted_by` là uid giáo viên (khi giáo viên tặng — #3) hoặc 'admin'
    (khi admin cấp trực tiếp). `days=None` = không giới hạn (admin dùng khi
    cần cấp vĩnh viễn); dùng cho gói tặng của giáo viên nên truyền số ngày
    cụ thể để tránh giáo viên tặng vĩnh viễn không kiểm soát."""
    if not student_id:
        return False
    expires_at = (_now() + days * 86400) if days else None
    fsdb.collection('users').document(student_id).set({
        'studentPlan': STUDENT_PREMIUM_PLAN, 'studentPlanStatus': 'active',
        'studentPlanExpiresAt': expires_at, 'studentPlanGrantedBy': granted_by,
        'updatedAt': _now(),
    }, merge=True)
    return True


def revoke_student_premium(student_id: str) -> bool:
    if not student_id:
        return False
    fsdb.collection('users').document(student_id).set({
        'studentPlan': 'free', 'studentPlanStatus': 'inactive',
        'studentPlanExpiresAt': None, 'studentPlanGrantedBy': None,
        'updatedAt': _now(),
    }, merge=True)
    return True


def _reset_usage_if_new_day(uid: str, user: dict) -> dict:
    today = _today_str()
    if user.get('usageDate') != today:
        fsdb.collection('users').document(uid).set({
            'aiUsageToday': 0, 'submissionUsageToday': 0, 'usageDate': today,
        }, merge=True)
        user['aiUsageToday'] = 0
        user['submissionUsageToday'] = 0
        user['usageDate'] = today
    return user


def try_consume_ai_usage(uid: str) -> tuple[bool, dict]:
    """Trả về (được_phép, thông_tin_usage). Tăng aiUsageToday nếu được phép."""
    user = ensure_user_defaults(uid)
    user = _reset_usage_if_new_day(uid, user)
    if has_unlimited_access(user):
        return True, {'unlimited': True}
    used = int(user.get('aiUsageToday', 0))
    if used >= FREE_AI_LIMIT:
        return False, {'unlimited': False, 'used': used, 'limit': FREE_AI_LIMIT}
    fsdb.collection('users').document(uid).set(
        {'aiUsageToday': used + 1, 'updatedAt': time.time()}, merge=True
    )
    return True, {'unlimited': False, 'used': used + 1, 'limit': FREE_AI_LIMIT}


def try_consume_submission_usage(uid: str) -> tuple[bool, dict]:
    user = ensure_user_defaults(uid)
    user = _reset_usage_if_new_day(uid, user)
    if has_unlimited_access(user):
        return True, {'unlimited': True}
    used = int(user.get('submissionUsageToday', 0))
    if used >= FREE_SUBMISSION_LIMIT:
        return False, {'unlimited': False, 'used': used, 'limit': FREE_SUBMISSION_LIMIT}
    fsdb.collection('users').document(uid).set(
        {'submissionUsageToday': used + 1, 'updatedAt': time.time()}, merge=True
    )
    return True, {'unlimited': False, 'used': used + 1, 'limit': FREE_SUBMISSION_LIMIT}


def usage_snapshot(uid: str) -> dict:
    user = ensure_user_defaults(uid)
    user = _reset_usage_if_new_day(uid, user)
    unlimited = has_unlimited_access(user)
    return {
        'unlimited': unlimited,
        'aiUsed': user.get('aiUsageToday', 0), 'aiLimit': FREE_AI_LIMIT,
        'submissionUsed': user.get('submissionUsageToday', 0), 'submissionLimit': FREE_SUBMISSION_LIMIT,
        'studentPlanStatus': user.get('studentPlanStatus', 'inactive'),
        'studentPlanExpiresAt': user.get('studentPlanExpiresAt'),
    }


def student_weak_topics(uid: str, min_answers: int = 3) -> dict:
    """Thống kê điểm yếu theo chuyên đề (#2) từ quiz_answers (tab Quiz) của
    1 học sinh: với mỗi chuyên đề đã làm, tính tỉ lệ đúng và số câu đã làm.
    `min_answers`: chuyên đề có ÍT HƠN số câu này bị loại khỏi 'weakest' vì
    chưa đủ dữ liệu để kết luận (nhưng vẫn xuất hiện trong 'byTopic' đầy đủ).

    Chỉ dùng dữ liệu quiz_answers có `topic` (câu hỏi cũ trước khi có field
    này, hoặc câu tự luận trong bài kiểm tra riêng của giáo viên, sẽ có
    topic rỗng và bị bỏ qua ở đây — có thể mở rộng sau bằng cách gắn topic
    cho câu hỏi trong tests nếu cần thống kê cả phần đó)."""
    answers = fsdb.list_quiz_answers_for_user(uid)
    by_topic: dict[str, dict] = {}
    for a in answers:
        topic = (a.get('topic') or '').strip()
        if not topic:
            continue
        bucket = by_topic.setdefault(topic, {'correct': 0, 'total': 0})
        bucket['total'] += 1
        if a.get('is_correct'):
            bucket['correct'] += 1
    result = []
    for topic, b in by_topic.items():
        accuracy = (b['correct'] / b['total']) if b['total'] else 0.0
        result.append({'topic': topic, 'correct': b['correct'], 'total': b['total'],
                        'accuracy': round(accuracy, 4)})
    result.sort(key=lambda x: x['accuracy'])
    weakest = [r for r in result if r['total'] >= min_answers]
    return {'byTopic': result, 'weakest': weakest[:3]}


def list_teachers() -> list[dict]:
    docs = fsdb.collection('users').where('role', '==', 'teacher').stream()
    out = []
    for d in docs:
        u = d.to_dict()
        u['uid'] = d.id
        out.append(u)
    return out


# ══════════════════════════════════════════════════════════════════════
# CLASSES
# ══════════════════════════════════════════════════════════════════════

def _gen_join_code(grade: int) -> str:
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f'CHEM{grade}-{suffix}'


def create_class(teacher_id: str, name: str, grade: int, description: str = '',
                  image_url: str = '', banner_url: str = '') -> str:
    ts = time.time()
    ref = fsdb.collection('classes').document()
    code = _gen_join_code(grade)
    ref.set({
        'name': name, 'grade': int(grade), 'teacherId': teacher_id,
        'description': description, 'imageURL': image_url, 'bannerURL': banner_url,
        'joinCode': code, 'studentIds': [], 'status': 'active',
        'createdAt': ts, 'updatedAt': ts,
    })
    return ref.id


def get_class(class_id: str) -> dict | None:
    doc = fsdb.collection('classes').document(class_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d['id'] = doc.id
    return d


def list_classes(grade: int = None, teacher_id: str = None, student_id: str = None) -> list[dict]:
    col = fsdb.collection('classes')
    q = col
    if teacher_id:
        q = q.where('teacherId', '==', teacher_id)
    elif grade:
        q = q.where('grade', '==', int(grade))
    docs = list(q.stream())
    items = []
    for d in docs:
        item = d.to_dict()
        item['id'] = d.id
        if student_id and student_id not in item.get('studentIds', []):
            continue
        if grade and teacher_id and item.get('grade') != int(grade):
            continue
        items.append(item)
    items.sort(key=lambda x: x.get('createdAt') or 0, reverse=True)
    return items


def update_class(class_id: str, **fields) -> bool:
    ref = fsdb.collection('classes').document(class_id)
    if not ref.get().exists:
        return False
    fields['updatedAt'] = time.time()
    ref.update(fields)
    return True


def delete_class(class_id: str) -> bool:
    ref = fsdb.collection('classes').document(class_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def join_class_by_code(student_id: str, code: str) -> dict | None:
    col = fsdb.collection('classes')
    docs = list(col.where('joinCode', '==', code.strip().upper()).limit(1).stream())
    if not docs:
        return None
    ref = docs[0].reference
    from google.cloud.firestore_v1 import ArrayUnion
    ref.update({'studentIds': ArrayUnion([student_id]), 'updatedAt': time.time()})
    cls = docs[0].to_dict()
    cls['id'] = docs[0].id
    # Nếu giáo viên của lớp đang có ChemCraft for edu -> bảo trợ học sinh mới luôn
    teacher = get_user(cls.get('teacherId', ''))
    if teacher and has_unlimited_access(teacher) and teacher.get('role') == 'teacher':
        fsdb.collection('users').document(student_id).set({
            'educationPlan': EDU_PLAN, 'educationPlanStatus': 'active',
            'sponsoredBy': cls.get('teacherId'), 'updatedAt': time.time(),
        }, merge=True)
    return cls


def add_student(class_id: str, student_id: str) -> bool:
    from google.cloud.firestore_v1 import ArrayUnion
    ref = fsdb.collection('classes').document(class_id)
    if not ref.get().exists:
        return False
    ref.update({'studentIds': ArrayUnion([student_id]), 'updatedAt': time.time()})
    return True


def remove_student(class_id: str, student_id: str) -> bool:
    from google.cloud.firestore_v1 import ArrayRemove
    ref = fsdb.collection('classes').document(class_id)
    if not ref.get().exists:
        return False
    ref.update({'studentIds': ArrayRemove([student_id]), 'updatedAt': time.time()})
    return True


def list_students(class_id: str) -> list[dict]:
    cls = get_class(class_id)
    if not cls:
        return []
    out = []
    for sid in cls.get('studentIds', []):
        u = get_user(sid)
        if u:
            out.append({
                'uid': sid, 'displayName': u.get('displayName', ''), 'email': u.get('email', ''),
                'educationPlan': u.get('educationPlan'), 'educationPlanStatus': u.get('educationPlanStatus'),
            })
    return out


# ══════════════════════════════════════════════════════════════════════
# LESSONS (theo lớp học của giáo viên — khác quiz_bank.py là ngân hàng câu
# hỏi chung của toàn hệ thống)
# ══════════════════════════════════════════════════════════════════════

def create_lesson(class_id: str, teacher_id: str, title: str, chapter: str = '',
                   content: str = '', attachments: list = None, order: int = 0) -> str:
    ts = time.time()
    ref = fsdb.collection('class_lessons').document()
    ref.set({
        'classId': class_id, 'teacherId': teacher_id, 'title': title, 'chapter': chapter,
        'content': content, 'attachments': attachments or [], 'order': order,
        'createdAt': ts, 'updatedAt': ts,
    })
    return ref.id


def list_lessons(class_id: str) -> list[dict]:
    docs = fsdb.collection('class_lessons').where('classId', '==', class_id).stream()
    items = []
    for d in docs:
        it = d.to_dict()
        it['id'] = d.id
        items.append(it)
    items.sort(key=lambda x: (x.get('order') or 0, x.get('createdAt') or 0))
    return items


def get_lesson(lesson_id: str) -> dict | None:
    doc = fsdb.collection('class_lessons').document(lesson_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d['id'] = doc.id
    return d


def update_lesson(lesson_id: str, **fields) -> bool:
    ref = fsdb.collection('class_lessons').document(lesson_id)
    if not ref.get().exists:
        return False
    fields['updatedAt'] = time.time()
    ref.update(fields)
    return True


def delete_lesson(lesson_id: str) -> bool:
    ref = fsdb.collection('class_lessons').document(lesson_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def mark_lesson_complete(student_id: str, lesson_id: str) -> None:
    ref = fsdb.collection('lesson_progress').document(f'{student_id}_{lesson_id}')
    ref.set({'studentId': student_id, 'lessonId': lesson_id, 'completedAt': time.time()})


def list_completed_lessons(student_id: str) -> set:
    docs = fsdb.collection('lesson_progress').where('studentId', '==', student_id).stream()
    return {d.to_dict().get('lessonId') for d in docs}


# ══════════════════════════════════════════════════════════════════════
# DOCUMENTS (metadata — file thật lưu ở Firebase Storage, client SDK upload
# thẳng, backend chỉ lưu URL + metadata trả về sau khi upload xong)
# ══════════════════════════════════════════════════════════════════════

def create_document(class_id: str, teacher_id: str, title: str, description: str,
                     file_url: str, file_type: str, file_size: int, lesson_id: str = None) -> str:
    ts = time.time()
    ref = fsdb.collection('class_documents').document()
    ref.set({
        'classId': class_id, 'teacherId': teacher_id, 'lessonId': lesson_id,
        'title': title, 'description': description, 'fileURL': file_url,
        'fileType': file_type, 'fileSize': file_size, 'createdAt': ts, 'updatedAt': ts,
    })
    return ref.id


def list_documents(class_id: str, lesson_id: str = None) -> list[dict]:
    docs = fsdb.collection('class_documents').where('classId', '==', class_id).stream()
    items = []
    for d in docs:
        it = d.to_dict()
        it['id'] = d.id
        if lesson_id and it.get('lessonId') != lesson_id:
            continue
        items.append(it)
    items.sort(key=lambda x: x.get('createdAt') or 0, reverse=True)
    return items


def update_document(document_id: str, **fields) -> bool:
    ref = fsdb.collection('class_documents').document(document_id)
    if not ref.get().exists:
        return False
    fields['updatedAt'] = time.time()
    ref.update(fields)
    return True


def delete_document(document_id: str) -> bool:
    ref = fsdb.collection('class_documents').document(document_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


# ══════════════════════════════════════════════════════════════════════
# ASSIGNMENTS + SUBMISSIONS
# ══════════════════════════════════════════════════════════════════════

def create_assignment(class_id: str, teacher_id: str, title: str, description: str,
                       a_type: str, deadline: float, max_score: float, lesson_id: str = None,
                       attachments: list = None) -> str:
    ts = time.time()
    ref = fsdb.collection('assignments').document()
    ref.set({
        'classId': class_id, 'teacherId': teacher_id, 'lessonId': lesson_id,
        'title': title, 'description': description, 'type': a_type,
        'deadline': deadline, 'maxScore': max_score, 'attachments': attachments or [],
        'createdAt': ts, 'updatedAt': ts,
    })
    return ref.id


def list_assignments(class_id: str) -> list[dict]:
    docs = fsdb.collection('assignments').where('classId', '==', class_id).stream()
    items = []
    for d in docs:
        it = d.to_dict()
        it['id'] = d.id
        items.append(it)
    items.sort(key=lambda x: x.get('deadline') or 0)
    return items


def get_assignment(assignment_id: str) -> dict | None:
    doc = fsdb.collection('assignments').document(assignment_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d['id'] = doc.id
    return d


def update_assignment(assignment_id: str, **fields) -> bool:
    ref = fsdb.collection('assignments').document(assignment_id)
    if not ref.get().exists:
        return False
    fields['updatedAt'] = time.time()
    ref.update(fields)
    return True


def delete_assignment(assignment_id: str) -> bool:
    ref = fsdb.collection('assignments').document(assignment_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def get_student_submission(assignment_id: str, student_id: str) -> dict | None:
    docs = list(fsdb.collection('submissions')
                .where('assignmentId', '==', assignment_id)
                .where('studentId', '==', student_id).limit(1).stream())
    if not docs:
        return None
    d = docs[0].to_dict()
    d['id'] = docs[0].id
    return d


def create_submission(assignment_id: str, student_id: str, class_id: str, teacher_id: str,
                       content: str, files: list = None) -> str:
    """Nộp bài. Nếu đã nộp trước đó và assignment chưa quá hạn -> cập nhật
    (cho phép sửa trước deadline), nếu đã quá hạn thì server phải chặn ở
    route trước khi gọi hàm này."""
    existing = get_student_submission(assignment_id, student_id)
    ts = time.time()
    if existing:
        ref = fsdb.collection('submissions').document(existing['id'])
        ref.update({
            'content': content, 'files': files or [], 'status': 'submitted',
            'submittedAt': ts, 'updatedAt': ts,
        })
        return existing['id']
    ref = fsdb.collection('submissions').document()
    ref.set({
        'assignmentId': assignment_id, 'studentId': student_id, 'classId': class_id,
        'teacherId': teacher_id, 'content': content, 'files': files or [],
        'status': 'submitted', 'score': None, 'feedback': '',
        'submittedAt': ts, 'gradedAt': None, 'updatedAt': ts,
    })
    return ref.id


def list_submissions(assignment_id: str = None, class_id: str = None, student_id: str = None) -> list[dict]:
    col = fsdb.collection('submissions')
    if assignment_id:
        q = col.where('assignmentId', '==', assignment_id)
    elif student_id:
        q = col.where('studentId', '==', student_id)
    elif class_id:
        q = col.where('classId', '==', class_id)
    else:
        q = col
    items = []
    for d in q.stream():
        it = d.to_dict()
        it['id'] = d.id
        items.append(it)
    items.sort(key=lambda x: x.get('submittedAt') or 0, reverse=True)
    return items


def grade_submission(submission_id: str, score: float, feedback: str) -> bool:
    ref = fsdb.collection('submissions').document(submission_id)
    if not ref.get().exists:
        return False
    ref.update({
        'score': score, 'feedback': feedback, 'status': 'graded',
        'gradedAt': time.time(), 'updatedAt': time.time(),
    })
    return True


# ══════════════════════════════════════════════════════════════════════
# TESTS (trắc nghiệm / đúng-sai nhiều ý / trả lời ngắn / tự luận)
# ══════════════════════════════════════════════════════════════════════

DEFAULT_TEST_SETTINGS = {
    'shuffleQuestions': False,
    'shuffleOptions': False,
    'timeLimitMinutes': None,      # None = không giới hạn giờ
    'allowReview': True,           # cho phép quay lại sửa câu đã làm trong lúc thi
    'showAnswersAfter': 'after_submit',  # 'after_submit' | 'manual' | 'never'
    'autoSubmitOnTimeout': True,
}


def _merge_settings(settings: dict = None) -> dict:
    out = dict(DEFAULT_TEST_SETTINGS)
    if settings:
        out.update({k: v for k, v in settings.items() if k in DEFAULT_TEST_SETTINGS})
    return out


def create_test(class_id: str, teacher_id: str, title: str, description: str,
                 questions: list, deadline: float, settings: dict = None) -> str:
    """questions: [{type, question, options?, correctAnswer?, subItems?, points}, ...]
    type in ('multiple_choice','true_false','true_false_group','short_answer','essay').
    - true_false_group: subItems=[{text, correct: bool}, ...] (dạng 4 ý a/b/c/d
      giống đề thi THPT 2025), chấm theo tỉ lệ số ý đúng.
    - short_answer.correctAnswer có thể là 1 chuỗi hoặc list các đáp án tương
      đương được chấp nhận.
    """
    ts = time.time()
    ref = fsdb.collection('tests').document()
    ref.set({
        'classId': class_id, 'teacherId': teacher_id, 'title': title,
        'description': description, 'questions': questions, 'deadline': deadline,
        'settings': _merge_settings(settings), 'resultsPublished': False,
        'createdAt': ts, 'updatedAt': ts,
    })
    return ref.id


def list_tests(class_id: str) -> list[dict]:
    docs = fsdb.collection('tests').where('classId', '==', class_id).stream()
    items = []
    for d in docs:
        it = d.to_dict()
        it['id'] = d.id
        items.append(it)
    items.sort(key=lambda x: x.get('deadline') or 0)
    return items


def get_test(test_id: str, *, for_student: bool = False) -> dict | None:
    doc = fsdb.collection('tests').document(test_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d['id'] = doc.id
    d['settings'] = _merge_settings(d.get('settings'))
    if for_student:
        # Không lộ đáp án đúng cho học sinh trước khi làm bài
        safe_qs = []
        for q in d.get('questions', []):
            q2 = dict(q)
            q2.pop('correctAnswer', None)
            if 'subItems' in q2:
                q2['subItems'] = [{'text': si.get('text', '')} for si in q2['subItems']]
            safe_qs.append(q2)
        d['questions'] = safe_qs
    return d


def update_test(test_id: str, **fields) -> bool:
    ref = fsdb.collection('tests').document(test_id)
    if not ref.get().exists:
        return False
    if 'settings' in fields:
        fields['settings'] = _merge_settings(fields['settings'])
    fields['updatedAt'] = time.time()
    ref.update(fields)
    return True


def publish_test_results(test_id: str, published: bool = True) -> bool:
    return update_test(test_id, resultsPublished=published)


def delete_test(test_id: str) -> bool:
    ref = fsdb.collection('tests').document(test_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def _norm_short_answer(s):
    if s is None:
        return None
    return str(s).strip().lower().replace(',', '.')


def _values_match(given, expected) -> bool:
    g, e = _norm_short_answer(given), _norm_short_answer(expected)
    if g is None or e is None:
        return False
    if g == e:
        return True
    try:
        return abs(float(g) - float(e)) < 1e-6
    except (TypeError, ValueError):
        return False


def _auto_grade(question: dict, answer) -> float | None:
    qtype = question.get('type')
    points = float(question.get('points', 0) or 0)
    correct = question.get('correctAnswer')

    if qtype in ('multiple_choice', 'true_false'):
        return points if answer == correct else 0.0

    if qtype == 'true_false_group':
        # Đề dạng "câu lớn + 4 ý a/b/c/d đúng/sai" — chấm theo tỉ lệ số ý đúng
        # (giống thang điểm THPT 2025), answer là list bool theo đúng thứ tự subItems.
        sub_items = question.get('subItems') or []
        if not sub_items:
            return 0.0
        ans_list = answer if isinstance(answer, list) else []
        correct_n = sum(
            1 for i, si in enumerate(sub_items)
            if i < len(ans_list) and ans_list[i] is not None and bool(ans_list[i]) == bool(si.get('correct'))
        )
        return round(points * correct_n / len(sub_items), 4)

    if qtype == 'short_answer':
        if answer is None or correct is None:
            return 0.0
        accepted = correct if isinstance(correct, list) else [correct]
        return points if any(_values_match(answer, a) for a in accepted) else 0.0

    return None  # essay -> chấm tay


def submit_test_attempt(test_id: str, student_id: str, answers: list) -> str:
    test = get_test(test_id)
    if not test:
        raise ValueError('test không tồn tại')
    questions = test.get('questions', [])
    graded_answers = []
    total_score = 0.0
    max_score = 0.0
    needs_manual_grading = False
    for i, q in enumerate(questions):
        ans = answers[i] if i < len(answers) else None
        max_score += float(q.get('points', 0) or 0)
        score = _auto_grade(q, ans)
        if score is None:
            needs_manual_grading = True
        else:
            total_score += score
        graded_answers.append({'answer': ans, 'score': score})
    ts = time.time()
    ref = fsdb.collection('test_attempts').document()
    ref.set({
        'testId': test_id, 'studentId': student_id, 'classId': test.get('classId'),
        'teacherId': test.get('teacherId'), 'answers': graded_answers,
        'totalScore': total_score, 'maxScore': max_score,
        'status': 'pending_review' if needs_manual_grading else 'graded',
        'submittedAt': ts,
    })
    return ref.id


def get_test_attempt(attempt_id: str) -> dict | None:
    doc = fsdb.collection('test_attempts').document(attempt_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d['id'] = doc.id
    return d


def get_test_attempt_for_review(attempt_id: str) -> dict | None:
    """Trả về attempt kèm đề gốc — chỉ lộ đáp án đúng nếu settings cho phép
    (showAnswersAfter != 'never', và nếu là 'manual' thì test phải đã được
    giáo viên bấm công bố kết quả)."""
    attempt = get_test_attempt(attempt_id)
    if not attempt:
        return None
    test = get_test(attempt['testId'])
    if not test:
        return attempt
    settings = test.get('settings') or DEFAULT_TEST_SETTINGS
    reveal = (settings.get('showAnswersAfter') == 'after_submit'
              or (settings.get('showAnswersAfter') == 'manual' and test.get('resultsPublished')))
    attempt['revealAnswers'] = bool(reveal)
    attempt['testTitle'] = test.get('title')
    if reveal:
        attempt['questions'] = test.get('questions', [])
    else:
        attempt['questions'] = [{'question': q.get('question'), 'type': q.get('type')}
                                 for q in test.get('questions', [])]
    return attempt


def grade_essay_answer(attempt_id: str, question_index: int, score: float) -> bool:
    ref = fsdb.collection('test_attempts').document(attempt_id)
    snap = ref.get()
    if not snap.exists:
        return False
    d = snap.to_dict()
    answers = d.get('answers', [])
    if question_index >= len(answers):
        return False
    answers[question_index]['score'] = score
    total = sum(a.get('score') or 0 for a in answers)
    still_pending = any(a.get('score') is None for a in answers)
    ref.update({
        'answers': answers, 'totalScore': total,
        'status': 'pending_review' if still_pending else 'graded',
    })
    return True


def list_test_attempts(test_id: str = None, student_id: str = None) -> list[dict]:
    col = fsdb.collection('test_attempts')
    q = col.where('testId', '==', test_id) if test_id else col.where('studentId', '==', student_id)
    items = []
    for d in q.stream():
        it = d.to_dict()
        it['id'] = d.id
        items.append(it)
    items.sort(key=lambda x: x.get('submittedAt') or 0, reverse=True)
    return items


# ── Parser đề dạng text (kiểu Azota) ────────────────────────────────────
_Q_HEADER_RE = re.compile(r'(?im)^[ \t]*C[âa]u[ \t]*(\d+)[\.:]?[ \t]*(.*)$')
_MC_OPT_RE = re.compile(r'^[ \t]*(\*?)([A-D])[\.\)][ \t]*(.*)$')
_TF_OPT_RE = re.compile(r'^[ \t]*(\*?)([a-d])[\.\)][ \t]*(.*)$')
_ANSKEY_HEADER_RE = re.compile(r'(?im)^[ \t]*answer[_ ]?key[ \t]*:?[ \t]*$')
_ANSKEY_ENTRY_RE = re.compile(r'(\d+)[ \t]*[\.\):][ \t]*([^;\n]+)')


def parse_azota_text(raw_text: str) -> list[dict]:
    """Phân tích đề dạng text theo cú pháp kiểu Azota:
      - Mỗi câu bắt đầu bằng dòng 'Câu N.'
      - Trắc nghiệm: các dòng 'A. ...'/'B. ...'/'C. ...'/'D. ...', '*' trước
        chữ cái đánh dấu đáp án đúng.
      - Đúng/Sai nhiều ý: các dòng 'a) ...'/'b) ...'/'c) ...'/'d) ...', '*'
        trước chữ đánh dấu ý ĐÚNG.
      - Trả lời ngắn: câu không có lựa chọn nhưng có mặt trong khối
        'answer_key' ở cuối đề (nhiều đáp án tương đương cách nhau bằng '/').
      - Còn lại (không lựa chọn, không có trong answer_key) -> tự luận.
    Trả về danh sách câu hỏi ở dạng preview để giáo viên xem/sửa trước khi lưu
    — KHÔNG tự lưu vào Firestore."""
    text = (raw_text or '').replace('\r\n', '\n').replace('\r', '\n')

    m = _ANSKEY_HEADER_RE.search(text)
    key_map = {}
    if m:
        key_blob = text[m.end():]
        text = text[:m.start()]
        for km in _ANSKEY_ENTRY_RE.finditer(key_blob):
            qnum, val = int(km.group(1)), km.group(2).strip()
            vals = [v.strip() for v in val.split('/') if v.strip()]
            key_map[qnum] = vals if len(vals) > 1 else (vals[0] if vals else '')

    headers = list(_Q_HEADER_RE.finditer(text))
    questions = []
    for i, h in enumerate(headers):
        qnum = int(h.group(1))
        stem_first_line = h.group(2).strip()
        body = text[h.end():(headers[i + 1].start() if i + 1 < len(headers) else len(text))]
        lines = body.split('\n')

        stem_lines = [stem_first_line] if stem_first_line else []
        idx = 0
        while idx < len(lines):
            line = lines[idx].strip()
            if not line:
                idx += 1
                continue
            if _MC_OPT_RE.match(line) or _TF_OPT_RE.match(line):
                break
            stem_lines.append(line)
            idx += 1
        question_text = '\n'.join(l for l in stem_lines if l).strip()

        mc_opts, tf_opts = [], []
        while idx < len(lines):
            line = lines[idx].strip()
            if not line:
                idx += 1
                continue
            mc = _MC_OPT_RE.match(line)
            tf = None if mc else _TF_OPT_RE.match(line)
            if mc:
                mc_opts.append((bool(mc.group(1)), mc.group(3).strip()))
            elif tf:
                tf_opts.append((bool(tf.group(1)), tf.group(3).strip()))
            idx += 1

        if mc_opts:
            correct_idx = next((k for k, o in enumerate(mc_opts) if o[0]), 0)
            questions.append({
                'type': 'multiple_choice', 'question': question_text,
                'options': [o[1] for o in mc_opts], 'correctAnswer': correct_idx, 'points': 1,
            })
        elif tf_opts:
            questions.append({
                'type': 'true_false_group', 'question': question_text,
                'subItems': [{'text': o[1], 'correct': o[0]} for o in tf_opts], 'points': 1,
            })
        elif qnum in key_map:
            questions.append({
                'type': 'short_answer', 'question': question_text,
                'correctAnswer': key_map[qnum], 'points': 1,
            })
        else:
            questions.append({'type': 'essay', 'question': question_text, 'points': 1})
    return questions


# ══════════════════════════════════════════════════════════════════════
# LIVESTREAMS (nhúng YouTube Live / Facebook Live) + chat
# ══════════════════════════════════════════════════════════════════════

def create_livestream(class_id: str, teacher_id: str, title: str, description: str,
                       scheduled_at: float, embed_url: str) -> str:
    ts = time.time()
    ref = fsdb.collection('livestreams').document()
    ref.set({
        'classId': class_id, 'teacherId': teacher_id, 'title': title,
        'description': description, 'scheduledAt': scheduled_at, 'embedURL': embed_url,
        'status': 'scheduled', 'recordingURL': '', 'createdAt': ts, 'updatedAt': ts,
    })
    return ref.id


def list_livestreams(class_id: str) -> list[dict]:
    docs = fsdb.collection('livestreams').where('classId', '==', class_id).stream()
    items = []
    for d in docs:
        it = d.to_dict()
        it['id'] = d.id
        items.append(it)
    items.sort(key=lambda x: x.get('scheduledAt') or 0, reverse=True)
    return items


def get_livestream(livestream_id: str) -> dict | None:
    doc = fsdb.collection('livestreams').document(livestream_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d['id'] = doc.id
    return d


def update_livestream(livestream_id: str, **fields) -> bool:
    ref = fsdb.collection('livestreams').document(livestream_id)
    if not ref.get().exists:
        return False
    fields['updatedAt'] = time.time()
    ref.update(fields)
    return True


def set_livestream_status(livestream_id: str, status: str, recording_url: str = None) -> bool:
    if status not in ('scheduled', 'live', 'ended'):
        return False
    fields = {'status': status}
    if recording_url is not None:
        fields['recordingURL'] = recording_url
    return update_livestream(livestream_id, **fields)


def delete_livestream(livestream_id: str) -> bool:
    ref = fsdb.collection('livestreams').document(livestream_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def add_chat_message(livestream_id: str, user_id: str, name: str, message: str) -> str:
    ref = fsdb.collection('livestreams').document(livestream_id).collection('chat_messages').document()
    ref.set({'userId': user_id, 'name': name, 'message': message[:1000], 'ts': time.time()})
    return ref.id


def list_chat_messages(livestream_id: str, limit: int = 200) -> list[dict]:
    # Cùng lỗi với list_class_chat_messages: limit_to_last() không dùng
    # được với .stream(), phải dùng .get().
    docs = (fsdb.collection('livestreams').document(livestream_id)
            .collection('chat_messages').order_by('ts').limit_to_last(limit).get())
    items = []
    for d in docs:
        it = d.to_dict()
        it['id'] = d.id
        items.append(it)
    return items


def delete_chat_message(livestream_id: str, message_id: str) -> bool:
    ref = fsdb.collection('livestreams').document(livestream_id).collection('chat_messages').document(message_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


# ══════════════════════════════════════════════════════════════════════
# CHAT TRỰC TIẾP HỌC SINH ↔ GIÁO VIÊN CHỦ NHIỆM (theo từng lớp)
# ══════════════════════════════════════════════════════════════════════
# Khác với chat công khai trong livestream (mọi người trong buổi live thấy
# nhau), đây là kênh RIÊNG TƯ giữa 1 học sinh và giáo viên chủ nhiệm LỚP đó
# — mỗi (lớp, học sinh) là 1 luồng hội thoại (`thread`) độc lập, giáo viên
# thấy được danh sách hội thoại của TẤT CẢ học sinh trong các lớp mình dạy,
# học sinh chỉ thấy hội thoại của chính mình với giáo viên lớp mình đang học.
#
# Firestore: class_chat_threads/{class_id}__{student_id}
#              .messages (subcollection, order theo ts)
_CHAT_THREADS_COL = 'class_chat_threads'


def _chat_thread_id(class_id: str, student_id: str) -> str:
    return f'{class_id}__{student_id}'


def send_class_chat_message(class_id: str, student_id: str, sender_id: str,
                             sender_role: str, sender_name: str, message: str) -> str:
    """`sender_role` là 'teacher' hoặc 'student' — quyền gửi được kiểm tra ở
    route (lms_routes.py), hàm này chỉ ghi dữ liệu."""
    message = (message or '').strip()[:2000]
    if not message:
        raise ValueError('empty_message')
    tid = _chat_thread_id(class_id, student_id)
    thread_ref = fsdb.collection(_CHAT_THREADS_COL).document(tid)
    ts = _now()
    msg_ref = thread_ref.collection('messages').document()
    msg_ref.set({
        'senderId': sender_id, 'senderRole': sender_role, 'senderName': sender_name or '',
        'message': message, 'ts': ts,
    })
    # unreadForTeacher/unreadForStudent: đánh dấu bên KIA có tin chưa đọc —
    # đơn giản hoá bằng cờ boolean thay vì đếm số lượng, đủ dùng cho badge "●".
    thread_ref.set({
        'classId': class_id, 'studentId': student_id,
        'lastMessage': message, 'lastSenderRole': sender_role, 'updatedAt': ts,
        'unreadForTeacher': sender_role == 'student',
        'unreadForStudent': sender_role == 'teacher',
    }, merge=True)
    return msg_ref.id


def list_class_chat_messages(class_id: str, student_id: str, limit: int = 300) -> list[dict]:
    tid = _chat_thread_id(class_id, student_id)
    # LƯU Ý: query có limit_to_last() không được phép dùng .stream(),
    # thư viện google-cloud-firestore bắt buộc phải dùng .get() —
    # đây chính là nguyên nhân gây lỗi 500 (ValueError) trước đó.
    docs = (fsdb.collection(_CHAT_THREADS_COL).document(tid)
            .collection('messages').order_by('ts').limit_to_last(limit).get())
    items = []
    for d in docs:
        it = d.to_dict()
        it['id'] = d.id
        items.append(it)
    return items


def mark_class_chat_read(class_id: str, student_id: str, reader_role: str) -> None:
    tid = _chat_thread_id(class_id, student_id)
    field = 'unreadForTeacher' if reader_role == 'teacher' else 'unreadForStudent'
    fsdb.collection(_CHAT_THREADS_COL).document(tid).set({field: False}, merge=True)


def get_class_chat_thread(class_id: str, student_id: str) -> dict | None:
    doc = fsdb.collection(_CHAT_THREADS_COL).document(_chat_thread_id(class_id, student_id)).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d['id'] = doc.id
    return d


def list_chat_threads_for_teacher(teacher_id: str) -> list[dict]:
    """Toàn bộ hội thoại (1 dòng / học sinh) của các lớp giáo viên này chủ
    nhiệm, kèm tên học sinh + tên lớp, sắp xếp mới nhất lên đầu — dùng cho
    hộp thư "Tin nhắn học sinh" của giáo viên."""
    classes = {c['id']: c for c in list_classes(teacher_id=teacher_id)}
    if not classes:
        return []
    threads = []
    # Firestore 'in' giới hạn 30 giá trị — số lớp của 1 giáo viên thường rất
    # nhỏ nên không cần chia lô, nhưng vẫn chia phòng khi vượt 30 cho an toàn.
    class_ids = list(classes.keys())
    for i in range(0, len(class_ids), 30):
        chunk = class_ids[i:i + 30]
        q = fsdb.collection(_CHAT_THREADS_COL).where('classId', 'in', chunk)
        for d in q.stream():
            t = d.to_dict()
            t['id'] = d.id
            cls = classes.get(t.get('classId'), {})
            t['className'] = cls.get('name', '')
            student = ensure_user_defaults(t.get('studentId'))
            t['studentName'] = student.get('displayName') or student.get('email') or '(chưa đặt tên)'
            threads.append(t)
    threads.sort(key=lambda x: x.get('updatedAt') or 0, reverse=True)
    return threads


# ══════════════════════════════════════════════════════════════════════
# ADMIN STATS
# ══════════════════════════════════════════════════════════════════════

def _agg_count(query) -> int:
    """Đếm bằng Aggregation Query — 1 lượt đọc bất kể collection to cỡ nào,
    thay vì .stream() toàn bộ document chỉ để lấy len(). Quan trọng trên
    Firestore Spark (free) plan vốn có hạn mức đọc/ngày."""
    try:
        result = query.count().get()
        return int(result[0][0].value)
    except Exception:
        return sum(1 for _ in query.stream())


def admin_lms_stats() -> dict:
    now = time.time()
    cached = _stats_cache.get('data')
    if cached and (now - _stats_cache.get('ts', 0)) < _STATS_TTL:
        return cached

    teachers = list_teachers()
    premium_teachers = [t for t in teachers if has_unlimited_access(t)]
    all_classes = list(fsdb.collection('classes').stream())  # collection classes thường nhỏ, cần đọc để gộp studentIds
    student_count = set()
    for c in all_classes:
        student_count.update(c.to_dict().get('studentIds', []))
    data = {
        'teacherCount': len(teachers),
        'premiumTeacherCount': len(premium_teachers),
        'classCount': len(all_classes),
        'studentCount': len(student_count),
        'submissionCount': _agg_count(fsdb.collection('submissions')),
        'testAttemptCount': _agg_count(fsdb.collection('test_attempts')),
        'livestreamCount': _agg_count(fsdb.collection('livestreams')),
        'studentPremiumCount': _agg_count(
            fsdb.collection('users').where('studentPlanStatus', '==', 'active')
        ),
    }
    _stats_cache['ts'] = now
    _stats_cache['data'] = data
    return data
