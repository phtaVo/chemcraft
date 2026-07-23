"""
firestore_db.py — Lưu trữ BỀN VỮNG (Firestore) cho toàn bộ dữ liệu "thu thập
từ người dùng": events (page_view, session_start, heartbeat, lesson_complete,
quiz_*, lab_*, bug_report...), ai_conversations/ai_messages, quiz_attempts/
quiz_answers, lab_sessions/lab_reaction_runs.

TẠI SAO CẦN FILE NÀY
=====================
database.py (SQLite) lưu vào 1 file trên đĩa cục bộ của Render. Free tier
của Render có filesystem "ephemeral" — bị xoá sạch mỗi khi service redeploy,
restart, hoặc "ngủ" sau 15 phút không có traffic rồi thức dậy. Đó là lý do
mọi dữ liệu tracking "hiện đúng lúc server đang chạy nhưng mất sau khi vào
lại". Firestore là managed cloud database, không phụ thuộc đĩa của Render,
nên dữ liệu sống sót qua mọi lần restart.

VẪN GIỮ SQLite CHO
===================
admin_accounts/admin_sessions/admin_login_audit (admin_auth.py),
quiz_questions (quiz_bank.py), lab_molecules/lab_reactions/lab_shelf_chemicals
(lab_bank.py) — đây là NỘI DUNG admin biên tập (ngân hàng câu hỏi, hoá chất...),
không phải dữ liệu tracking người dùng. Vẫn bị mất khi Render restart, nhưng
đó là vấn đề tách biệt — có thể xử lý sau bằng Persistent Disk hoặc migrate
tương tự file này nếu cần.

THIẾT LẬP
=========
1. Vào Firebase Console → Project Settings → Service Accounts → Generate new
   private key → tải file JSON service account (KHÔNG phải file config web
   apiKey đang dùng ở FIREBASE_CONFIG trong server.py — đây là 1 file khác,
   có quyền ghi Admin, phải giữ bí mật, KHÔNG commit lên Git).
2. Trên Render, vào service → Environment → thêm biến:
     FIREBASE_SERVICE_ACCOUNT_JSON = <dán toàn bộ nội dung file JSON vào đây>
   (Render cho phép giá trị env var dài, dán nguyên văn cả file JSON là được.)
3. Bật Firestore ở Firebase Console (Build → Firestore Database → Create
   database) nếu dự án chưa bật, chọn chế độ Production/Native, region gần
   người dùng (VD asia-southeast1).
4. Lần đầu chạy các truy vấn kết hợp (VD lọc theo `type` + khoảng `ts`),
   Firestore có thể báo lỗi "The query requires an index" kèm 1 đường link —
   chỉ cần bấm vào link đó (mở console, bấm "Create Index"), đợi 1-2 phút là
   xong, không cần làm gì thêm. Điều này CHỈ xảy ra 1 lần cho mỗi loại truy
   vấn, không phải mỗi request.
"""
import os
import json
import time

import firebase_admin
from firebase_admin import credentials, firestore

_db = None


def init():
    """Khởi tạo kết nối Firestore. Gọi 1 lần lúc server start; các hàm khác
    tự gọi lại hàm này (idempotent) nếu chưa init, để tránh lỗi thứ tự import."""
    global _db
    if _db is not None:
        return _db
    if not firebase_admin._apps:
        cred = _load_credentials()
        firebase_admin.initialize_app(cred)
    _db = firestore.client()
    return _db


def _load_credentials():
    raw = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON', '').strip()
    if raw:
        try:
            info = json.loads(raw)
        except Exception as e:
            raise RuntimeError(
                f'FIREBASE_SERVICE_ACCOUNT_JSON không phải JSON hợp lệ: {e}. '
                f'Kiểm tra lại đã dán ĐÚNG TOÀN BỘ nội dung file service-account.json '
                f'chưa (không thiếu dấu ngoặc, không lẫn ký tự thừa).'
            )
        return credentials.Certificate(info)
    key_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '').strip()
    if key_path and os.path.exists(key_path):
        return credentials.Certificate(key_path)
    raise RuntimeError(
        'Chưa cấu hình Firestore: thiếu biến môi trường FIREBASE_SERVICE_ACCOUNT_JSON '
        '(dán nội dung file service-account JSON tải từ Firebase Console → Project '
        'Settings → Service Accounts → Generate new private key). Xem hướng dẫn đầy '
        'đủ trong docstring đầu file firestore_db.py.'
    )


def _col(name: str):
    return init().collection(name)


def collection(name: str):
    """Accessor public cho các module khác (quiz_bank.py, lab_bank.py) cũng
    lưu nội dung admin biên tập vào Firestore — dùng chung 1 kết nối/app đã
    init ở đây thay vì mỗi module tự gọi firebase_admin.initialize_app()
    (initialize_app() thứ hai sẽ lỗi 'app already exists')."""
    return _col(name)


def batched_seed(collection_name: str, docs: dict) -> int:
    """Ghi hàng loạt (batch, tới 400 document/lần commit — thay vì từng cái
    một) cho seed_default_*() ở quiz_bank.py/lab_bank.py.

    `docs` là {doc_id: data_dict}, ID cố định (không phải auto-id) nên việc
    ghi đè (set) là AN TOÀN khi gọi lại nhiều lần — không tạo trùng lặp.
    Chỉ seed (ghi) khi số document hiện có ÍT HƠN tổng số cần có — nếu lần
    trước bị Render kill giữa chừng lúc đang seed (VD do quá trình khởi động
    mất quá lâu), lần khởi động sau sẽ phát hiện thiếu và ghi lại TOÀN BỘ
    (ghi đè các document đã có sẵn không gây hại gì, cùng dữ liệu).

    Trả về số document được ghi trong lần gọi này (0 nếu đã seed đủ).
    """
    col = _col(collection_name)
    existing_count = _agg_count(col)
    if existing_count >= len(docs):
        return 0

    client = init()
    ids = list(docs.keys())
    written = 0
    for i in range(0, len(ids), 400):
        batch = client.batch()
        for doc_id in ids[i:i + 400]:
            batch.set(col.document(doc_id), docs[doc_id])
        batch.commit()
        written += len(ids[i:i + 400])
    return written


def _agg_count(query) -> int:
    """Đếm số document khớp query bằng Aggregation Query (rẻ, nhanh, không
    tải toàn bộ document về). Fallback sang đếm thủ công nếu bản thư viện
    google-cloud-firestore đang dùng chưa hỗ trợ .count()."""
    try:
        result = query.count().get()
        return int(result[0][0].value)
    except Exception:
        return sum(1 for _ in query.stream())


# ═══════════════════════════════════════════════════════════════════════
# EVENTS — thay cho bảng `events` trong database.py
# ═══════════════════════════════════════════════════════════════════════

def record_event(ev_type: str, payload: dict | None = None, user_id: str = '', ip: str = '') -> None:
    payload = payload or {}
    doc = {'type': ev_type, 'user_id': user_id or '', 'ip': ip or '', 'ts': time.time()}
    for k, v in payload.items():
        if k not in doc:  # không cho payload ghi đè 4 field gốc
            doc[k] = v
    _col('events').add(doc)


def count_events(ev_type: str) -> int:
    return _agg_count(_col('events').where('type', '==', ev_type))


def count_events_total() -> int:
    """Tổng số document trong `events`, tính bằng Aggregation Query (rẻ —
    không tải document nào về), dùng cho KPI 'events_total' thay vì
    len(all_events())."""
    return _agg_count(_col('events'))


def bucket_by_day(ev_type: str, days: int = 7) -> list[int]:
    now = time.time()
    day_sec = 86400
    buckets = [0] * days
    q = (_col('events')
         .where('type', '==', ev_type)
         .where('ts', '>=', now - days * day_sec))
    for doc in q.stream():
        ts = doc.get('ts') or 0
        age = now - ts
        idx = days - 1 - int(age // day_sec)
        if 0 <= idx < days:
            buckets[idx] += 1
    return buckets


def query_events(ev_type: str = '', since: float = 0, limit: int = 200) -> list[dict]:
    q = _col('events')
    if ev_type:
        q = q.where('type', '==', ev_type)
    if since:
        q = q.where('ts', '>=', since)
    q = q.order_by('ts', direction=firestore.Query.DESCENDING).limit(limit)
    return [doc.to_dict() for doc in q.stream()]


def events_since(since: float, limit: int = 20000) -> list[dict]:
    """Event từ mốc thời gian `since` trở đi (không lọc theo type) — chỉ đọc
    đúng khung thời gian cần, KHÔNG quét toàn bộ lịch sử. Dùng cho các
    endpoint chỉ cần vài giờ/vài chục ngày gần nhất (online-uids, rankings...)
    thay vì all_events() (đọc toàn bộ collection, tốn quota, ngày càng đắt
    khi collection lớn dần)."""
    q = (_col('events')
         .where('ts', '>=', since)
         .order_by('ts')
         .limit(limit))
    return [doc.to_dict() for doc in q.stream()]


def events_for_user(uid: str, limit: int = 5000) -> list[dict]:
    """Toàn bộ lịch sử event của MỘT user — dùng chỉ số (index) theo
    user_id, KHÔNG quét toàn bộ collection `events` của mọi user. Rẻ hơn
    all_events() rất nhiều khi có đông người dùng. Dùng cho hồ sơ user, hoạt
    động theo uid — cần tạo composite index (user_id, ts) lần đầu chạy (xem
    link 'create index' trong log nếu Firestore báo thiếu)."""
    if not uid:
        return []
    q = (_col('events')
         .where('user_id', '==', uid)
         .order_by('ts')
         .limit(limit))
    return [doc.to_dict() for doc in q.stream()]


def all_events() -> list[dict]:
    """Toàn bộ event, sắp theo thời gian tăng dần. CHỈ dùng khi thực sự cần
    số liệu TOÀN THỜI GIAN (VD tổng số học sinh từng dùng web, tính từ lúc
    đầu) — các trường hợp chỉ cần vài ngày/user cụ thể nên dùng
    events_since()/events_for_user() ở trên để đọc ít hơn, rẻ hơn nhiều."""
    return [doc.to_dict() for doc in _col('events').order_by('ts').stream()]


# ═══════════════════════════════════════════════════════════════════════
# AI CONVERSATIONS / MESSAGES — thay cho ai_conversations + ai_messages
# Dùng "denormalization" (field tổng hợp lưu sẵn trên conversation) thay
# cho các subquery tương quan (correlated subquery) trong SQL gốc, vì
# Firestore không hỗ trợ JOIN.
# ═══════════════════════════════════════════════════════════════════════

def create_conversation(user_id: str, ip: str, topic: str = '', conversation_id: str | None = None) -> str:
    """Nếu conversation_id được truyền vào và tồn tại → dùng lại (thread tiếp
    cuộc hội thoại). Nếu không → tạo mới. Trả về id (string, Firestore auto-id)."""
    if conversation_id:
        ref = _col('ai_conversations').document(conversation_id)
        if ref.get().exists:
            return conversation_id
        # id được client gửi nhưng chưa tồn tại (VD lần đầu của thread mới) → tạo với đúng id đó
    else:
        ref = _col('ai_conversations').document()
    ref.set({
        'user_id': user_id or '', 'ip': ip or '', 'topic': topic or '',
        'started_at': time.time(), 'last_ts': time.time(),
        'message_count': 0, 'first_question': '', 'has_image': False, 'error_count': 0,
    })
    return ref.id


def add_message(conversation_id: str, role: str, content: str = '', has_image: bool = False,
                 ok: bool = True, error_msg: str | None = None, latency_ms: int | None = None) -> None:
    if not conversation_id:
        return
    ts = time.time()
    conv_ref = _col('ai_conversations').document(conversation_id)
    conv_snap = conv_ref.get()
    topic = conv_snap.get('topic') if conv_snap.exists else ''

    _col('ai_messages').add({
        'conversation_id': conversation_id, 'role': role, 'content': content or '',
        'has_image': bool(has_image), 'ok': bool(ok), 'error_msg': error_msg,
        'latency_ms': latency_ms, 'ts': ts, 'topic': topic or '',
    })

    if not conv_snap.exists:
        return
    updates = {'last_ts': ts, 'message_count': firestore.Increment(1)}
    if role == 'user':
        if has_image:
            updates['has_image'] = True
        if not (conv_snap.get('first_question') or ''):
            updates['first_question'] = (content or '')[:500]
    elif role == 'model' and not ok:
        updates['error_count'] = firestore.Increment(1)
    conv_ref.update(updates)


def get_conversation(cid: str) -> dict | None:
    snap = _col('ai_conversations').document(cid).get()
    if not snap.exists:
        return None
    d = snap.to_dict()
    d['id'] = snap.id
    return d


def list_messages(cid: str) -> list[dict]:
    q = _col('ai_messages').where('conversation_id', '==', cid).order_by('ts')
    return [doc.to_dict() for doc in q.stream()]


def list_conversations_all(exclude_topics: tuple = ('admin_reply_draft',)) -> list[dict]:
    """Toàn bộ conversation (trừ các topic bị loại) — nguồn cho listing có
    tìm kiếm/lọc/phân trang và các số liệu thống kê AI Assistant."""
    out = []
    for d in _col('ai_conversations').stream():
        item = d.to_dict()
        item['id'] = d.id
        if item.get('topic') in exclude_topics:
            continue
        out.append(item)
    return out


def list_conversations_for_user(uid: str, limit: int = 200) -> list[dict]:
    """Lịch sử hội thoại AI của 1 học sinh — dùng cho tính năng 'Lưu lịch sử
    AI không giới hạn' (#5). KHÔNG giới hạn số lượng theo gói Free/Premium ở
    tầng đọc (dữ liệu đã lưu vĩnh viễn trên Firestore từ trước — giới hạn
    Free/Premium chỉ áp dụng cho SỐ LƯỢT HỎI MỚI/ngày qua try_consume_ai_usage,
    không áp dụng cho việc xem lại lịch sử cũ); `limit` chỉ để tránh tải quá
    nhiều trong 1 lần, không phải giới hạn sản phẩm."""
    if not uid:
        return []
    q = (_col('ai_conversations')
         .where('user_id', '==', uid)
         .order_by('last_ts', direction=firestore.Query.DESCENDING)
         .limit(limit))
    out = []
    for d in q.stream():
        item = d.to_dict()
        item['id'] = d.id
        out.append(item)
    return out


def list_all_messages(role: str | None = None, exclude_topics: tuple = ('admin_reply_draft',)) -> list[dict]:
    """Toàn bộ message (có thể lọc theo role) trừ các message thuộc topic bị
    loại — dùng cho top câu hỏi, word cloud, tần suất theo ngày, error rate..."""
    q = _col('ai_messages')
    if role:
        q = q.where('role', '==', role)
    out = []
    for d in q.stream():
        item = d.to_dict()
        if item.get('topic') in exclude_topics:
            continue
        out.append(item)
    return out


def search_conversation_ids(term: str) -> set:
    """Tìm trong TOÀN BỘ nội dung câu hỏi (không chỉ câu đầu) — Firestore
    không hỗ trợ LIKE/full-text nên phải quét & lọc bằng Python. Chấp nhận
    được ở quy mô 1 trường học; nếu dữ liệu lớn hơn nhiều, nên thay bằng
    Algolia/Typesense hoặc Firestore full-text extension."""
    term_low = term.lower()
    ids = set()
    for doc in _col('ai_messages').where('role', '==', 'user').stream():
        content = (doc.get('content') or '')
        if term_low in content.lower():
            ids.add(doc.get('conversation_id'))
    return ids


# ═══════════════════════════════════════════════════════════════════════
# QUIZ — thay cho quiz_attempts + quiz_answers (KHÔNG phải ngân hàng câu
# hỏi quiz_questions — cái đó vẫn ở SQLite qua quiz_bank.py)
# ═══════════════════════════════════════════════════════════════════════

def create_quiz_attempt(user_id: str, total_q) -> str:
    ref = _col('quiz_attempts').document()
    ref.set({
        'user_id': user_id or '', 'started_at': time.time(), 'total_q': total_q,
        'finished_at': None, 'correct_q': None, 'duration_sec': None,
    })
    return ref.id


def record_quiz_answer(attempt_id: str, question_id, question_text: str,
                        is_correct: bool, retry_count: int = 0, duration_sec=None,
                        topic: str = '') -> None:
    if not attempt_id:
        return
    _col('quiz_answers').add({
        'attempt_id': attempt_id, 'question_id': question_id or '',
        'question_text': question_text or '', 'is_correct': bool(is_correct),
        'retry_count': retry_count or 0, 'duration_sec': duration_sec,
        'topic': topic or '', 'answered_at': time.time(),
    })


def complete_quiz_attempt(attempt_id: str, correct_count, duration_sec) -> None:
    if not attempt_id:
        return
    _col('quiz_attempts').document(attempt_id).update({
        'finished_at': time.time(), 'correct_q': correct_count, 'duration_sec': duration_sec,
    })


def list_quiz_attempts() -> list[dict]:
    out = []
    for d in _col('quiz_attempts').stream():
        item = d.to_dict()
        item['id'] = d.id
        out.append(item)
    return out


def list_quiz_answers() -> list[dict]:
    return [d.to_dict() for d in _col('quiz_answers').stream()]


def list_quiz_answers_for_user(uid: str, limit_attempts: int = 50) -> list[dict]:
    """Toàn bộ quiz_answers thuộc về các attempt của 1 học sinh — dùng cho
    thống kê điểm yếu theo chuyên đề (#2). Firestore không hỗ trợ JOIN nên
    phải lấy attempt_id của user trước (giới hạn `limit_attempts` lần gần
    nhất để tránh quét quá nhiều), rồi truy vấn quiz_answers theo lô 30
    attempt_id 1 lần ('in' operator giới hạn tối đa 30 giá trị)."""
    if not uid:
        return []
    attempt_q = (_col('quiz_attempts')
                 .where('user_id', '==', uid)
                 .order_by('started_at', direction=firestore.Query.DESCENDING)
                 .limit(limit_attempts))
    attempt_ids = [d.id for d in attempt_q.stream()]
    if not attempt_ids:
        return []
    out = []
    for i in range(0, len(attempt_ids), 30):
        chunk = attempt_ids[i:i + 30]
        q = _col('quiz_answers').where('attempt_id', 'in', chunk)
        out.extend(d.to_dict() for d in q.stream())
    return out


# ═══════════════════════════════════════════════════════════════════════
# LAB — thay cho lab_sessions + lab_reaction_runs (KHÔNG phải ngân hàng
# hoá chất/phản ứng lab_molecules/lab_reactions — vẫn ở SQLite qua lab_bank.py)
# ═══════════════════════════════════════════════════════════════════════

def create_lab_session(user_id: str) -> str:
    ref = _col('lab_sessions').document()
    ref.set({'user_id': user_id or '', 'started_at': time.time(), 'ended_at': None, 'duration_sec': None})
    return ref.id


def close_lab_session(session_id: str, duration_sec) -> None:
    if not session_id:
        return
    _col('lab_sessions').document(session_id).update({
        'ended_at': time.time(), 'duration_sec': duration_sec,
    })


def record_lab_reaction_run(session_id: str, user_id: str, reaction_eq: str, chemicals, equipment,
                             outcome: str, error_reason, duration_sec) -> None:
    _col('lab_reaction_runs').add({
        'session_id': session_id or '', 'user_id': user_id or '', 'reaction_eq': reaction_eq or '',
        'chemicals': chemicals, 'equipment': equipment, 'outcome': outcome or 'unknown',
        'error_reason': error_reason, 'duration_sec': duration_sec, 'ts': time.time(),
    })


def list_lab_sessions() -> list[dict]:
    return [d.to_dict() for d in _col('lab_sessions').stream()]


def list_lab_reaction_runs() -> list[dict]:
    return [d.to_dict() for d in _col('lab_reaction_runs').stream()]
