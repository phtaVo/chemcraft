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
import string
import time

import firestore_db as fsdb

# ── Hằng số Freemium ─────────────────────────────────────────────────────
FREE_AI_LIMIT = 5
FREE_SUBMISSION_LIMIT = 2
EDU_PLAN = 'chemcraft_for_edu'


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
    ):
        if key not in d:
            patch[key] = default
    if patch:
        patch['updatedAt'] = ts
        ref.update(patch)
        d.update(patch)
    d['uid'] = uid
    return d


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
    if (user.get('educationPlan') == EDU_PLAN
            and user.get('educationPlanStatus') == 'active'):
        return True
    return False


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
    }


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
                       a_type: str, deadline: float, max_score: float, lesson_id: str = None) -> str:
    ts = time.time()
    ref = fsdb.collection('assignments').document()
    ref.set({
        'classId': class_id, 'teacherId': teacher_id, 'lessonId': lesson_id,
        'title': title, 'description': description, 'type': a_type,
        'deadline': deadline, 'maxScore': max_score, 'createdAt': ts, 'updatedAt': ts,
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
# TESTS (trắc nghiệm / đúng-sai / trả lời ngắn / tự luận)
# ══════════════════════════════════════════════════════════════════════

def create_test(class_id: str, teacher_id: str, title: str, description: str,
                 questions: list, deadline: float) -> str:
    """questions: [{type, question, options?, correctAnswer?, points}, ...]
    type in ('multiple_choice','true_false','short_answer','essay')."""
    ts = time.time()
    ref = fsdb.collection('tests').document()
    ref.set({
        'classId': class_id, 'teacherId': teacher_id, 'title': title,
        'description': description, 'questions': questions, 'deadline': deadline,
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
    if for_student:
        # Không lộ đáp án đúng cho học sinh trước khi làm bài
        safe_qs = []
        for q in d.get('questions', []):
            q2 = dict(q)
            q2.pop('correctAnswer', None)
            safe_qs.append(q2)
        d['questions'] = safe_qs
    return d


def update_test(test_id: str, **fields) -> bool:
    ref = fsdb.collection('tests').document(test_id)
    if not ref.get().exists:
        return False
    fields['updatedAt'] = time.time()
    ref.update(fields)
    return True


def delete_test(test_id: str) -> bool:
    ref = fsdb.collection('tests').document(test_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True


def _auto_grade(question: dict, answer) -> float | None:
    qtype = question.get('type')
    points = float(question.get('points', 0) or 0)
    correct = question.get('correctAnswer')
    if qtype in ('multiple_choice', 'true_false'):
        return points if answer == correct else 0.0
    if qtype == 'short_answer':
        if answer is None or correct is None:
            return 0.0
        return points if str(answer).strip().lower() == str(correct).strip().lower() else 0.0
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
    docs = (fsdb.collection('livestreams').document(livestream_id)
            .collection('chat_messages').order_by('ts').limit_to_last(limit).stream())
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
# ADMIN STATS
# ══════════════════════════════════════════════════════════════════════

def admin_lms_stats() -> dict:
    teachers = list_teachers()
    premium_teachers = [t for t in teachers if has_unlimited_access(t)]
    all_classes = list(fsdb.collection('classes').stream())
    student_count = set()
    for c in all_classes:
        student_count.update(c.to_dict().get('studentIds', []))
    return {
        'teacherCount': len(teachers),
        'premiumTeacherCount': len(premium_teachers),
        'classCount': len(all_classes),
        'studentCount': len(student_count),
        'submissionCount': len(list(fsdb.collection('submissions').stream())),
        'testAttemptCount': len(list(fsdb.collection('test_attempts').stream())),
        'livestreamCount': len(list(fsdb.collection('livestreams').stream())),
    }
