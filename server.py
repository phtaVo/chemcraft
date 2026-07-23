import os
import re
import time
from collections import Counter
from datetime import datetime
from threading import Lock

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

import database as db
import admin_auth
import quiz_bank
import lab_bank
import firestore_db as fsdb
import lms_db
import lms_routes

load_dotenv()

app = Flask(__name__, static_folder='.')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

db.init_db()
admin_auth.bootstrap_default_admin()

# ── Firestore: lưu bền vững cả (1) dữ liệu tracking (events, ai_*, quiz_*,
# lab_* tracking) lẫn (2) nội dung admin biên tập (ngân hàng câu hỏi quiz,
# hoá chất/phản ứng Lab 3D) — thay cho SQLite, vốn bị xoá sạch mỗi khi Render
# free tier redeploy/restart. Không crash cả server nếu thiếu cấu hình — chỉ
# các endpoint liên quan Firestore sẽ trả lỗi rõ ràng, để /api/admin-login
# (vẫn dùng SQLite cho admin_accounts) không bị ảnh hưởng.
try:
    fsdb.init()
    print('✅ Firestore đã kết nối.')

    # LMS: lớp học, bài học/tài liệu/bài tập/bài kiểm tra, livestream,
    # phân quyền student/teacher/admin, freemium usage limits.
    lms_routes.register(app)
    print('✅ LMS routes đã đăng ký (/api/lms/*).')

    try:
        n = quiz_bank.seed_default_questions()
        print(f'📦 quiz questions: đã seed {n} câu mới (0 = đã đủ từ trước).')
    except Exception as e:
        print(f'❌ Lỗi khi seed quiz questions: {e}')

    try:
        n = lab_bank.seed_default_molecules()
        print(f'📦 lab molecules: đã seed {n} phân tử mới (0 = đã đủ từ trước).')
    except Exception as e:
        print(f'❌ Lỗi khi seed lab molecules: {e}')

    try:
        n = lab_bank.seed_default_reactions()
        print(f'📦 lab reactions: đã seed {n} phản ứng mới (0 = đã đủ từ trước).')
    except Exception as e:
        print(f'❌ Lỗi khi seed lab reactions: {e}')

    try:
        n = lab_bank.seed_default_shelf_chemicals()
        print(f'📦 lab shelf chemicals: đã seed {n} hoá chất mới (0 = đã đủ từ trước).')
    except Exception as e:
        print(f'❌ Lỗi khi seed lab shelf chemicals: {e}')

except Exception as e:
    print(f'⚠️  Firestore CHƯA sẵn sàng: {e}')
    print('   → Mọi tính năng thu thập dữ liệu, ngân hàng quiz, ngân hàng Lab 3D sẽ lỗi cho tới khi cấu hình xong.')


# ── Config ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_URL = (
    'https://generativelanguage.googleapis.com/v1beta/models/'
    'gemini-2.5-flash:generateContent'
)

# ── Gửi email trực tiếp từ trang Admin (thay cho mailto:) ───────────────────
# QUAN TRỌNG: Render CHẶN toàn bộ kết nối SMTP đi ra ngoài (cổng 25/465/587)
# ở tầng hạ tầng để chống spam — vì vậy nối thẳng smtplib tới smtp.gmail.com
# sẽ LUÔN báo lỗi "[Errno 101] Network is unreachable", kể cả khi App
# Password đúng 100%. Đây không phải lỗi cấu hình, mà là giới hạn mạng của
# Render, không có cách nào mở lại từ phía code.
#
# → Giải pháp: gửi email qua HTTP API (đi qua cổng 443 bình thường, không bị
# chặn) bằng Resend — free 3.000 email/tháng vĩnh viễn, API rất gọn, docs rõ
# ràng, setup nhanh hơn hầu hết các dịch vụ khác.
MAIL_SENDER_ADDRESS = os.getenv('CHEMCRAFT_MAIL_ADDRESS', 'voducphat.learncode.tk01@gmail.com')
MAIL_SENDER_NAME    = os.getenv('CHEMCRAFT_MAIL_NAME', 'ChemCraft')
BREVO_API_KEY       = os.getenv('BREVO_API_KEY', '').strip()
BREVO_URL           = 'https://api.brevo.com/v3/smtp/email'  # tên có "smtp" nhưng đây là REST API qua HTTPS, không phải cổng SMTP
# QUAN TRỌNG: Brevo cho verify từng ĐỊA CHỈ EMAIL đơn lẻ (single sender) chỉ
# bằng cách bấm link xác nhận gửi vào hộp thư đó — KHÔNG cần sở hữu domain
# riêng hay đụng vào DNS như Resend/Mailgun/SES. Vì vậy có thể dùng đúng
# voducphat.learncode.tk01@gmail.com làm sender và gửi tới bất kỳ ai, ngay
# cả khi không có domain riêng. Xem hướng dẫn lấy BREVO_API_KEY ở cuối file
# (phần comment cuối cùng).


def send_admin_email(to_email: str, subject: str, body_text: str) -> None:
    """Gửi email trực tiếp (không qua mailto:) bằng Brevo HTTP API — dùng
    HTTPS (cổng 443) nên không bị Render chặn như SMTP. Raise Exception với
    thông báo tiếng Việt dễ hiểu nếu thất bại."""
    if not BREVO_API_KEY:
        raise RuntimeError(
            'Server chưa cấu hình BREVO_API_KEY. Xem hướng dẫn cấu hình gửi '
            'email ở comment phía trên hàm send_admin_email() trong server.py.'
        )
    payload = {
        'sender': {'name': MAIL_SENDER_NAME, 'email': MAIL_SENDER_ADDRESS},
        'to': [{'email': to_email}],
        'subject': subject,
        'textContent': body_text,
    }
    resp = requests.post(
        BREVO_URL,
        headers={
            'api-key': BREVO_API_KEY,
            'Content-Type': 'application/json',
            'accept': 'application/json',
        },
        json=payload,
        timeout=20,
    )
    if resp.status_code not in (200, 201, 202):
        detail = resp.text[:300]
        try:
            detail = resp.json().get('message', detail)
        except Exception:
            pass
        raise RuntimeError(f'Brevo từ chối gửi (status {resp.status_code}): {detail}')


FIREBASE_CONFIG = {
    'apiKey':            os.getenv('FIREBASE_API_KEY', ''),
    'authDomain':        os.getenv('FIREBASE_AUTH_DOMAIN', ''),
    'projectId':         os.getenv('FIREBASE_PROJECT_ID', ''),
    'storageBucket':     os.getenv('FIREBASE_STORAGE_BUCKET', ''),
    'messagingSenderId': os.getenv('FIREBASE_MESSAGING_SENDER_ID', ''),
    'appId':             os.getenv('FIREBASE_APP_ID', ''),
    'measurementId':     os.getenv('FIREBASE_MEASUREMENT_ID', ''),
}

# ── Rate limiting (unchanged from original) ─────────────────────────────────
RATE_WINDOW = 60
RATE_MAX    = 20
COOLDOWN    = 3

from collections import defaultdict
ip_requests: dict[str, list[float]] = defaultdict(list)
ip_last_req:  dict[str, float]       = {}
rate_lock = Lock()

_server_started_at = time.time()
_runtime_lock = Lock()
_runtime = {
    'chat_total': 0, 'chat_ok': 0, 'chat_errors': 0,
    'gemini_errors': 0, 'rate_limit_hits': 0,
    'active_ips': {},
}

# ── Giới hạn AI/ngày cho khách vãng lai (chưa đăng nhập, không có userId) ──
# Trước đây tài khoản ẩn danh KHÔNG bị giới hạn (lms_db.try_consume_ai_usage
# chỉ áp dụng khi có user_id) — đây là lỗ hổng cho phép spam Gemini API
# không giới hạn chỉ bằng cách không đăng nhập. Dùng bộ đếm trong RAM theo
# IP (không ghi Firestore, tránh tốn quota ghi cho lượt truy cập vãng lai)
# — chấp nhận được vì bộ đếm reset khi server restart, giống các bộ đếm
# rate-limit khác trong file này.
ANON_AI_DAILY_LIMIT = 5
_anon_ai_usage: dict[str, dict] = {}
_anon_ai_lock = Lock()


def _check_anon_ai_limit(ip: str) -> str | None:
    """Trả về thông báo lỗi nếu IP này đã dùng hết lượt AI miễn phí hôm nay,
    None nếu còn được phép (và đã tăng bộ đếm)."""
    today = time.strftime('%Y-%m-%d', time.gmtime(time.time() + 7 * 3600))
    with _anon_ai_lock:
        entry = _anon_ai_usage.get(ip)
        if not entry or entry.get('date') != today:
            entry = {'date': today, 'count': 0}
        if entry['count'] >= ANON_AI_DAILY_LIMIT:
            _anon_ai_usage[ip] = entry
            return (f"Bạn đã sử dụng hết {ANON_AI_DAILY_LIMIT} lượt AI miễn phí hôm nay cho khách "
                    f"vãng lai. Vui lòng đăng nhập để có thêm lượt dùng, hoặc quay lại vào ngày mai.")
        entry['count'] += 1
        _anon_ai_usage[ip] = entry
    return None


def _get_client_ip() -> str:
    return request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()


def _require_admin() -> dict | None:
    token = request.headers.get('X-Admin-Token', '')
    return admin_auth.verify(token)


def check_rate_limit(ip: str) -> str | None:
    now = time.time()
    with rate_lock:
        last = ip_last_req.get(ip, 0)
        if now - last < COOLDOWN:
            with _runtime_lock:
                _runtime['rate_limit_hits'] += 1
            return 'Bạn đang gửi yêu cầu quá nhanh. Vui lòng chờ vài giây.'
        window_start = now - RATE_WINDOW
        ip_requests[ip] = [t for t in ip_requests[ip] if t > window_start]
        if len(ip_requests[ip]) >= RATE_MAX:
            with _runtime_lock:
                _runtime['rate_limit_hits'] += 1
            return 'Bạn đã gửi quá nhiều yêu cầu. Vui lòng thử lại sau ít phút.'
        ip_requests[ip].append(now)
        ip_last_req[ip] = now
    return None


# ── Gemini call (unchanged logic from original server.py) ──────────────────
def _call_gemini_once(contents, system_prompt: str, max_tokens: int = 65536):
    payload = {
        'contents': contents,
        'systemInstruction': {'parts': [{'text': system_prompt}]},
        'generationConfig': {'temperature': 0.3, 'maxOutputTokens': max_tokens},
    }
    resp = requests.post(GEMINI_URL, params={'key': GEMINI_API_KEY}, json=payload, timeout=120)
    if not resp.content:
        raise ValueError('Gemini trả về phản hồi rỗng.')
    try:
        data = resp.json()
    except Exception:
        raise ValueError(f'Gemini trả về dữ liệu không hợp lệ (status {resp.status_code}).')
    if 'error' in data:
        code = data['error'].get('code', resp.status_code)
        msg  = data['error'].get('message', 'Lỗi không xác định')
        if code == 429:
            raise ValueError('RATE_LIMIT')
        raise ValueError(f'Gemini error {code}: {msg}')
    candidates = data.get('candidates', [])
    if not candidates:
        raise ValueError('Không nhận được phản hồi từ AI.')
    candidate = candidates[0]
    text = ''.join(p.get('text', '') for p in candidate.get('content', {}).get('parts', []))
    return text, candidate.get('finishReason', '')


# ── /api/chat — now also records structured ai_conversations/ai_messages ───
@app.route('/api/chat', methods=['POST'])
def chat():
    ip = _get_client_ip()
    t0 = time.time()
    with _runtime_lock:
        _runtime['chat_total'] += 1
        _runtime['active_ips'][ip] = time.time()

    rate_err = check_rate_limit(ip)
    if rate_err:
        with _runtime_lock:
            _runtime['chat_errors'] += 1
        return jsonify({'error': rate_err}), 429

    body = request.get_json(silent=True) or {}
    messages      = body.get('messages', [])
    system_prompt = body.get('systemPrompt', '')
    user_id       = body.get('userId', '')
    topic         = body.get('topic', '')
    conversation_id = body.get('conversationId')  # frontend should persist & resend this per thread

    if not messages:
        return jsonify({'error': 'Thiếu nội dung tin nhắn.'}), 400
    if not GEMINI_API_KEY:
        return jsonify({'error': 'Server chưa cấu hình API key.'}), 500

    # Freemium: tối đa 5 lượt AI/ngày cho tài khoản Free (gộp chung mọi loại
    # AI, không tách riêng). Tài khoản chưa đăng nhập (user_id rỗng) dùng bộ
    # đếm theo IP riêng (xem _check_anon_ai_limit) — đã vá lỗ hổng cũ (#7).
    if user_id:
        try:
            allowed, usage_info = lms_db.try_consume_ai_usage(user_id)
            if not allowed:
                return jsonify({
                    'error': f"Bạn đã sử dụng hết {usage_info['limit']} lượt AI miễn phí hôm nay. "
                             f"Vui lòng quay lại vào ngày mai hoặc nâng cấp gói Premium.",
                    'limitReached': True,
                }), 429
        except Exception as e:
            app.logger.warning('Bỏ qua kiểm tra usage AI do lỗi Firestore: %s', e)
    else:
        anon_err = _check_anon_ai_limit(ip)
        if anon_err:
            return jsonify({'error': anon_err, 'limitReached': True}), 429

    # Ensure a conversation row exists so multi-turn chats are threaded,
    # not just isolated question/answer events like before.
    conversation_id = fsdb.create_conversation(user_id, ip, topic, conversation_id)

    try:
        last_msg = messages[-1]
        user_content = last_msg.get('content', '')
        parts = []
        question_text = ''
        has_image = False
        if isinstance(user_content, str):
            parts.append({'text': user_content})
            question_text = user_content
        elif isinstance(user_content, list):
            for item in user_content:
                if item.get('type') == 'text':
                    parts.append({'text': item['text']})
                    question_text += ' ' + item['text']
                elif item.get('type') == 'image_url':
                    has_image = True
                    data_url = item.get('image_url', {}).get('url', '')
                    if data_url.startswith('data:'):
                        m = re.match(r'data:([^;]+);base64,', data_url)
                        mime_type = m.group(1) if m else 'image/jpeg'
                        b64 = data_url.split(',', 1)[1]
                        parts.append({'inlineData': {'mimeType': mime_type, 'data': b64}})

        fsdb.add_message(conversation_id, 'user', question_text.strip()[:2000], has_image=has_image)

        contents = [{'role': 'user', 'parts': parts}]
        full_text, finish_reason = _call_gemini_once(contents, system_prompt)

        tries = 0
        while finish_reason == 'MAX_TOKENS' and tries < 3:
            tries += 1
            cont_contents = [
                {'role': 'user',  'parts': parts},
                {'role': 'model', 'parts': [{'text': full_text}]},
                {'role': 'user',  'parts': [{'text': 'Tiếp tục giải từ chỗ vừa dừng, không lặp lại phần đã có.'}]},
            ]
            extra_text, finish_reason = _call_gemini_once(cont_contents, system_prompt)
            full_text += extra_text

        latency_ms = int((time.time() - t0) * 1000)
        with _runtime_lock:
            _runtime['chat_ok'] += 1

        fsdb.add_message(conversation_id, 'model', full_text[:4000], latency_ms=latency_ms)

        fsdb.record_event('ai_chat', {
            'question': question_text.strip()[:500],
            'topic': topic,
            'ok': True,
            'latency_ms': latency_ms,
            'conversation_id': conversation_id,
        }, user_id=user_id, ip=ip)

        return jsonify({'text': full_text, 'conversationId': conversation_id})

    except ValueError as e:
        msg = str(e)
        with _runtime_lock:
            _runtime['chat_errors'] += 1
            _runtime['gemini_errors'] += 1
        fsdb.add_message(conversation_id, 'model', ok=False, error_msg=msg)
        fsdb.record_event('ai_error', {'error': msg, 'conversation_id': conversation_id}, user_id=user_id, ip=ip)
        if msg == 'RATE_LIMIT':
            return jsonify({'error': 'Hệ thống AI đang bận. Vui lòng thử lại sau vài giây.'}), 429
        return jsonify({'error': msg}), 500
    except requests.exceptions.Timeout:
        with _runtime_lock:
            _runtime['chat_errors'] += 1
        fsdb.record_event('ai_error', {'error': 'timeout', 'conversation_id': conversation_id}, user_id=user_id, ip=ip)
        return jsonify({'error': 'Yêu cầu mất quá nhiều thời gian. Vui lòng thử lại.'}), 504
    except Exception as e:
        app.logger.error('Chat error: %s', e)
        with _runtime_lock:
            _runtime['chat_errors'] += 1
        fsdb.record_event('ai_error', {'error': str(e), 'conversation_id': conversation_id}, user_id=user_id, ip=ip)
        return jsonify({'error': 'Đã xảy ra lỗi. Vui lòng thử lại.'}), 500


# ── Admin login/verify/logout — now hashed + persisted ──────────────────────
@app.route('/api/admin-login', methods=['POST'])
def admin_login():
    body     = request.get_json(silent=True) or {}
    username = body.get('username', '')
    password = body.get('password', '')
    result = admin_auth.login(username, password, _get_client_ip())
    if not result:
        time.sleep(0.5)
        return jsonify({'error': 'Sai tên đăng nhập hoặc mật khẩu.'}), 401
    return jsonify(result)


@app.route('/api/admin-verify', methods=['GET'])
def admin_verify():
    session = _require_admin()
    if not session:
        return jsonify({'error': 'Token không hợp lệ hoặc đã hết hạn.'}), 401
    return jsonify({'username': session['username'], 'name': session['name'], 'role': session['role']})


@app.route('/api/admin-logout', methods=['POST'])
def admin_logout():
    admin_auth.logout(request.headers.get('X-Admin-Token', ''))
    return jsonify({'ok': True})


@app.route('/api/firebase-config', methods=['GET'])
def firebase_config():
    return jsonify(FIREBASE_CONFIG)


# ── /api/admin/send-reply-email ─────────────────────────────────────────────
# Gửi email phản hồi TRỰC TIẾP từ server khi admin bấm "Gửi" ở modal "Soạn
# email phản hồi" — thay cho việc mở mailto: (yêu cầu người dùng có sẵn ứng
# dụng Mail trên máy). Người gửi luôn là MAIL_SENDER_ADDRESS ở trên.
@app.route('/api/admin/send-reply-email', methods=['POST'])
def admin_send_reply_email():
    admin = _require_admin()
    if not admin:
        return jsonify({'error': 'Phiên đăng nhập admin đã hết hạn. Vui lòng đăng nhập lại.'}), 401

    body = request.get_json(silent=True) or {}
    to_email        = (body.get('to') or '').strip()
    subject         = (body.get('subject') or '').strip() or 'Phản hồi từ ChemCraft'
    content         = body.get('body') or ''
    conversation_id = body.get('conversationId')

    if not to_email or '@' not in to_email:
        return jsonify({'error': 'Địa chỉ email người nhận không hợp lệ.'}), 400
    if not content.strip():
        return jsonify({'error': 'Nội dung email đang trống.'}), 400

    try:
        send_admin_email(to_email, subject, content)
    except Exception as e:
        app.logger.error('send_admin_email error: %s', e)
        return jsonify({'error': f'Gửi email thất bại: {e}'}), 500

    fsdb.record_event('admin_reply_email_sent', {
        'to': to_email,
        'subject': subject,
        'conversationId': conversation_id,
        'adminUsername': admin.get('username', ''),
    }, user_id='', ip=_get_client_ip())

    return jsonify({'ok': True})


# ── /api/log-event — expanded contract: quiz + lab now real ────────────────
# NOTE: the frontend (lesson.html, lab.html) must be instrumented to actually
# call ccTrack(...) with these types at the right moments — see the
# INTEGRATION_NOTES.md shipped alongside this file for exact call sites.
ALLOWED_EVENT_TYPES = {
    'session_start', 'heartbeat', 'page_view',
    'lesson_open', 'lesson_complete',
    'quiz_start', 'quiz_answer', 'quiz_complete',
    'lab_open', 'lab_close', 'lab_step',
    'lab_reaction_attempt', 'lab_reaction_result',
    'lab_complete', 'lab_error',
    'search', 'feature_use', 'feedback_submit', 'bug_report',
}

_last_log_by_ip: dict[str, float] = {}
_last_log_lock = Lock()


@app.route('/api/log-event', methods=['POST'])
def log_event():
    ip = _get_client_ip()
    body = request.get_json(silent=True) or {}
    ev_type = (body.get('type') or '').strip()
    if ev_type not in ALLOWED_EVENT_TYPES:
        return jsonify({'error': 'Loại sự kiện không hợp lệ.'}), 400

    now = time.time()
    with _last_log_lock:
        last = _last_log_by_ip.get(ip, 0)
        if now - last < 0.2:
            return jsonify({'ok': False, 'error': 'too_fast'}), 429
        _last_log_by_ip[ip] = now

    user_id = body.get('userId', '')
    payload = {k: v for k, v in body.items() if k not in ('type', 'userId')}
    fsdb.record_event(ev_type, payload, user_id=user_id, ip=ip)

    # Structured side-collections for the event types that feed dedicated
    # analytics. Returns the new Firestore doc id (string) for session/
    # attempt-opening events so the client can thread subsequent events
    # (lab_reaction_result, quiz_answer, ...) back to the right session/
    # attempt — see tracker.js ccLab / ccQuiz.
    new_id = _fanout_structured_tables(ev_type, payload, user_id)

    resp = {'ok': True}
    if ev_type == 'lab_open':
        resp['sessionId'] = new_id
    elif ev_type == 'quiz_start':
        resp['attemptId'] = new_id
    return jsonify(resp)


def _fanout_structured_tables(ev_type: str, payload: dict, user_id: str):
    """Ghi vào các collection Firestore có cấu trúc (quiz_attempts,
    lab_sessions, ...) để các endpoint phân tích không phải quét toàn bộ
    `events` mỗi lần. Trả về id document mới (string) khi loại event này mở
    1 session/attempt mới."""
    if ev_type == 'quiz_start':
        return fsdb.create_quiz_attempt(user_id, payload.get('totalQuestions'))
    elif ev_type == 'quiz_answer':
        attempt_id = payload.get('attemptId')
        if attempt_id:
            fsdb.record_quiz_answer(
                attempt_id, payload.get('questionId', ''), payload.get('questionText', ''),
                bool(payload.get('correct')), payload.get('retryCount', 0), payload.get('durationSec'),
                topic=payload.get('topic', '')
            )
    elif ev_type == 'quiz_complete':
        attempt_id = payload.get('attemptId')
        if attempt_id:
            fsdb.complete_quiz_attempt(attempt_id, payload.get('correctCount'), payload.get('durationSec'))
    elif ev_type == 'lab_open':
        return fsdb.create_lab_session(user_id)
    elif ev_type == 'lab_close':
        session_id = payload.get('sessionId')
        if session_id:
            fsdb.close_lab_session(session_id, payload.get('durationSec'))
    elif ev_type == 'lab_reaction_result':
        fsdb.record_lab_reaction_run(
            payload.get('sessionId', ''), user_id, payload.get('reactionEq', ''),
            payload.get('chemicals', '[]'), payload.get('equipment', '[]'),
            payload.get('outcome', 'unknown'), payload.get('errorReason'), payload.get('durationSec')
        )
    return None


# ── /api/admin-metrics — real SQL aggregation, no mock fallback ────────────
@app.route('/api/admin-metrics', methods=['GET'])
def admin_metrics():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    now = time.time()

    with _runtime_lock:
        active_ips = len({ip for ip, t in _runtime['active_ips'].items() if now - t < 300})
        chat_total, chat_ok, chat_errors = _runtime['chat_total'], _runtime['chat_ok'], _runtime['chat_errors']
        gemini_errors, rate_limit_hits = _runtime['gemini_errors'], _runtime['rate_limit_hits']

    ai_chats      = fsdb.count_events('ai_chat')
    lessons_done  = fsdb.count_events('lesson_complete')
    quiz_done     = fsdb.count_events('quiz_complete')
    lab_open      = fsdb.count_events('lab_open')
    lab_completed = fsdb.count_events('lab_complete')
    lab_completion = round(100 * lab_completed / max(1, lab_open)) if lab_open else 0

    events_total = fsdb.count_events_total()

    daily_activity = fsdb.bucket_by_day('page_view', 7)
    daily_ai       = fsdb.bucket_by_day('ai_chat', 7)
    daily_lab      = fsdb.bucket_by_day('lab_open', 7)
    daily_lesson   = fsdb.bucket_by_day('lesson_complete', 7)

    donut = {'Bài học': lessons_done, 'Quiz': quiz_done, 'Lab 3D': lab_open, 'AI': ai_chats}

    # Top AI questions — quét ai_messages (role='user'), gộp theo nội dung.
    user_msgs = fsdb.list_all_messages(role='user')
    content_counter = Counter(m['content'] for m in user_msgs if m.get('content') and len(m['content']) > 8)
    top_questions = content_counter.most_common(10)
    top_questions = [(c[:120], n) for c, n in top_questions]

    stop = set('và của là ở có cho một các những này đó khi được với thì để làm như từ trong về hay hoặc '
               'thế nào tại sao gì bao nhiêu the a an of is are and or to in on for how why what which'.split())
    words = Counter()
    for m in user_msgs:
        for w in (m.get('content') or '').split():
            w = w.strip('.,?!:;()[]"\'').lower()
            if len(w) >= 3 and w not in stop:
                words[w] += 1
    word_cloud = words.most_common(30)

    # Heatmap hour x weekday, last 28 days
    heat_rows = fsdb.events_since(now - 28 * 86400)
    heatmap = [[0] * 24 for _ in range(7)]
    for r in heat_rows:
        d = datetime.fromtimestamp(r['ts'])
        heatmap[d.weekday()][d.hour] += 1

    return jsonify({
        'server': {
            'uptime_sec': int(now - _server_started_at),
            'active_ips': active_ips,
            'chat_total': chat_total, 'chat_ok': chat_ok, 'chat_errors': chat_errors,
            'gemini_errors': gemini_errors, 'rate_limit_hits': rate_limit_hits,
        },
        'counts': {
            'ai_chats': ai_chats, 'lessons_done': lessons_done, 'quiz_done': quiz_done,
            'lab_open': lab_open, 'lab_completed': lab_completed,
            'lab_completion': lab_completion, 'events_total': events_total,
        },
        'daily': {
            'activity_7d': daily_activity, 'ai_7d': daily_ai,
            'lab_7d': daily_lab, 'lesson_7d': daily_lesson,
        },
        'donut': donut,
        'top_questions': top_questions,
        'word_cloud': word_cloud,
        'heatmap': heatmap,
    })


@app.route('/api/admin-events', methods=['GET'])
def admin_events():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    ev_type = request.args.get('type', '')
    limit   = min(int(request.args.get('limit', 200)), 2000)
    since   = float(request.args.get('since', 0))
    events = fsdb.query_events(ev_type, since, limit)
    return jsonify({'events': events, 'total': len(events)})


# ── /api/admin-analytics/quiz — real per-question breakdown ────────────────
# Backed by quiz_attempts (1 row / lượt thi) + quiz_answers (1 row / câu trả
# lời, ghi bởi selectQuizOption -> nextQuestion trong lesson.html qua
# ccTrack('quiz_answer', ...)). question_text được lưu lại tại thời điểm trả
# lời (snapshot) nên vẫn đúng dù sau này admin sửa/xóa câu hỏi trong ngân
# hàng — chỉ cột `difficulty` là tra cứu "best-effort" theo quiz_questions.id
# hiện tại (sẽ là null nếu câu hỏi đã bị xóa, hoặc là câu fallback/thực hành
# không có id số).
@app.route('/api/admin-analytics/quiz', methods=['GET'])
def analytics_quiz():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401

    attempts = fsdb.list_quiz_attempts()
    answers = fsdb.list_quiz_answers()

    total_attempts = len(attempts)
    finished = [a for a in attempts if a.get('finished_at') is not None]
    finished_attempts = len(finished)
    score_ratios = [
        a['correct_q'] / a['total_q'] for a in finished
        if a.get('total_q') and a.get('correct_q') is not None
    ]
    avg_score = sum(score_ratios) / len(score_ratios) if score_ratios else 0
    attempt_durations = [a['duration_sec'] for a in finished if a.get('duration_sec') is not None]
    avg_attempt_duration = sum(attempt_durations) / len(attempt_durations) if attempt_durations else None

    # Gộp câu trả lời theo (question_id, question_text)
    grouped = defaultdict(lambda: {'attempts': 0, 'correct_n': 0, 'wrong_n': 0, 'times': [], 'retries': []})
    for a in answers:
        qid = a.get('question_id')
        if not qid:
            continue
        key = (qid, a.get('question_text'))
        g = grouped[key]
        g['attempts'] += 1
        if a.get('is_correct'):
            g['correct_n'] += 1
        else:
            g['wrong_n'] += 1
        if a.get('duration_sec') is not None:
            g['times'].append(a['duration_sec'])
        g['retries'].append(a.get('retry_count') or 0)

    bank_difficulty = {str(q['id']): q['difficulty'] for q in quiz_bank.list_questions()}

    questions = []
    for (qid, qtext), g in grouped.items():
        attempts_n = g['attempts']
        questions.append({
            'question_id':  qid,
            'text':         qtext,
            'attempts':     attempts_n,
            'correct_pct':  round(100.0 * g['correct_n'] / attempts_n, 1) if attempts_n else 0,
            'wrong_pct':    round(100.0 * g['wrong_n'] / attempts_n, 1) if attempts_n else 0,
            'avg_time_sec': round(sum(g['times']) / len(g['times']), 1) if g['times'] else None,
            'avg_retries':  round(sum(g['retries']) / len(g['retries']), 2) if g['retries'] else 0,
            'difficulty':   bank_difficulty.get(str(qid)),
        })
    questions.sort(key=lambda q: q['wrong_pct'], reverse=True)

    overall_times = [a['duration_sec'] for a in answers if a.get('duration_sec') is not None]
    overall_retries = [a.get('retry_count') or 0 for a in answers]
    overall_avg_time = sum(overall_times) / len(overall_times) if overall_times else None
    overall_avg_retries = sum(overall_retries) / len(overall_retries) if overall_retries else 0

    return jsonify({
        'total_attempts':          total_attempts,
        'finished_attempts':       finished_attempts,
        'avg_score_pct':           round((avg_score or 0) * 100, 1),
        'avg_attempt_duration_sec': round(avg_attempt_duration, 1) if avg_attempt_duration is not None else None,
        'avg_time_sec':            round(overall_avg_time, 1) if overall_avg_time is not None else None,
        'avg_retries':             round(overall_avg_retries, 2) if overall_avg_retries is not None else 0,
        'questions':               questions,
    })


## ── Ngân hàng câu hỏi Quiz (quiz_bank.py) ──────────────────────────────────
# Public: lesson.html gọi để lấy 1 bộ đề random khi học sinh bấm "Bắt đầu thi".
@app.route('/api/quiz-questions', methods=['GET'])
def public_quiz_questions():
    try:
        count = int(request.args.get('count', 34))
    except (TypeError, ValueError):
        count = 34
    count = max(1, min(count, 200))
    topic = (request.args.get('topic') or '').strip() or None
    return jsonify({'questions': quiz_bank.random_questions(count, topic=topic)})


# Public: danh sách chuyên đề hiện có, cho dropdown "Quiz theo chuyên đề tự chọn" (#4).
@app.route('/api/quiz-topics', methods=['GET'])
def public_quiz_topics():
    return jsonify({'topics': quiz_bank.list_topics()})


# Admin: CRUD toàn bộ ngân hàng câu hỏi cho tab "Ngân hàng câu hỏi" trong trang Quiz.
@app.route('/api/admin/quiz-questions', methods=['GET'])
def admin_list_quiz_questions():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify({'questions': quiz_bank.list_questions(include_inactive=True)})


@app.route('/api/admin/quiz-questions', methods=['POST'])
def admin_create_quiz_question():
    session = _require_admin()
    if not session:
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    question = (body.get('question') or '').strip()
    options = body.get('options') or []
    answer_index = body.get('answerIndex')
    difficulty = (body.get('difficulty') or 'TB').strip() or 'TB'

    if not question:
        return jsonify({'error': 'Vui lòng nhập nội dung câu hỏi.'}), 400
    if not isinstance(options, list) or len(options) < 2:
        return jsonify({'error': 'Cần ít nhất 2 phương án trả lời.'}), 400
    options = [str(o).strip() for o in options]
    if any(not o for o in options):
        return jsonify({'error': 'Các phương án trả lời không được để trống.'}), 400
    try:
        answer_index = int(answer_index)
    except (TypeError, ValueError):
        return jsonify({'error': 'Vui lòng chọn đáp án đúng.'}), 400
    if not (0 <= answer_index < len(options)):
        return jsonify({'error': 'Đáp án đúng không hợp lệ.'}), 400

    qid = quiz_bank.create_question(
        question, options, answer_index, difficulty, created_by=session['username']
    )
    return jsonify(quiz_bank.get_question(qid)), 201


@app.route('/api/admin/quiz-questions/<qid>', methods=['PUT'])
def admin_update_quiz_question(qid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    if not quiz_bank.get_question(qid):
        return jsonify({'error': 'Không tìm thấy câu hỏi.'}), 404

    body = request.get_json(silent=True) or {}
    options = body.get('options')
    if options is not None:
        if not isinstance(options, list) or len(options) < 2:
            return jsonify({'error': 'Cần ít nhất 2 phương án trả lời.'}), 400
        options = [str(o).strip() for o in options]
        if any(not o for o in options):
            return jsonify({'error': 'Các phương án trả lời không được để trống.'}), 400

    answer_index = body.get('answerIndex')
    if answer_index is not None:
        try:
            answer_index = int(answer_index)
        except (TypeError, ValueError):
            return jsonify({'error': 'Đáp án đúng không hợp lệ.'}), 400
        bound = len(options) if options is not None else len(quiz_bank.get_question(qid)['options'])
        if not (0 <= answer_index < bound):
            return jsonify({'error': 'Đáp án đúng không hợp lệ.'}), 400

    question = body.get('question')
    if question is not None:
        question = question.strip()
        if not question:
            return jsonify({'error': 'Vui lòng nhập nội dung câu hỏi.'}), 400

    quiz_bank.update_question(
        qid,
        question=question,
        options=options,
        answer_index=answer_index,
        difficulty=body.get('difficulty'),
        active=body.get('active'),
    )
    return jsonify(quiz_bank.get_question(qid))


@app.route('/api/admin/quiz-questions/<qid>', methods=['DELETE'])
def admin_delete_quiz_question(qid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    ok = quiz_bank.delete_question(qid)
    if not ok:
        return jsonify({'error': 'Không tìm thấy câu hỏi.'}), 404
    return jsonify({'ok': True})


## ── Lab 3D: Ngân hàng hoá chất (mô hình 3D) & phản ứng (lab_bank.py) ────────
# Public: lab.html gọi lúc khởi tạo để nạp thêm hoá chất/phản ứng do admin tạo
# (chỉ trả về những bản ghi đã 'published'), gộp thêm vào LAB_MOLECULE_DATA /
# REACTIONS vốn đã có sẵn trong file — không thay thế, chỉ bổ sung.
#
# `premiumOnly` (#6): nội dung nâng cao (công thức/phản ứng khó) admin đánh
# dấu premiumOnly=true sẽ bị ẨN khỏi kết quả nếu người gọi không có Premium.
# Truyền `?uid=<uid>` (Firebase uid) để server kiểm tra quyền — nếu không
# truyền (hoặc user không unlimited), mặc định AN TOÀN là ẩn nội dung
# premium. TODO tích hợp lab.html: hiện lab.html đang gọi các endpoint này
# ĐỒNG BỘ (XHR sync) trước khi Firebase Auth kịp resolve user, nên chưa gửi
# `uid` — cần đợi onAuthStateChanged rồi mới gọi (hoặc gọi lại 1 lần sau khi
# có user) để nội dung Premium thật sự hiện ra cho học sinh đã mua gói.
def _resolve_unlimited_from_query() -> bool:
    uid = (request.args.get('uid') or '').strip()
    if not uid:
        return False
    try:
        user = lms_db.ensure_user_defaults(uid)
        return lms_db.has_unlimited_access(user)
    except Exception:
        return False


@app.route('/api/lab/molecules', methods=['GET'])
def public_lab_molecules():
    return jsonify({'molecules': lab_bank.list_molecules(status='published')})


@app.route('/api/lab/reactions', methods=['GET'])
def public_lab_reactions():
    reactions = lab_bank.list_reactions(status='published')
    reactions = lab_bank.filter_premium_only(reactions, _resolve_unlimited_from_query())
    return jsonify({'reactions': reactions})


# lab.html's evaluateReaction() engine (the code that actually decides what color/
# bubble/precipitate to show when chemicals are mixed) needs the FULL structured
# data — participants, heat condition, before/during/after phases — not just the
# task-card text above. Only reactions with participants defined are returned,
# since those are the only ones evaluateReaction() can match against.
@app.route('/api/lab/reactions-full', methods=['GET'])
def public_lab_reactions_full():
    reactions = lab_bank.list_reactions(status='published')
    reactions = [r for r in reactions if r.get('participants')]
    reactions = lab_bank.filter_premium_only(reactions, _resolve_unlimited_from_query())
    return jsonify({'reactions': reactions})


# Admin: CRUD hoá chất (mô hình 3D) cho tab "Lab 3D" trong trang quản trị.
@app.route('/api/admin/lab-molecules', methods=['GET'])
def admin_list_lab_molecules():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify({'molecules': lab_bank.list_molecules()})


@app.route('/api/admin/lab-molecules', methods=['POST'])
def admin_create_lab_molecule():
    session = _require_admin()
    if not session:
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    chem_id = (body.get('chemId') or '').strip()
    name = (body.get('name') or '').strip()
    formula = (body.get('formula') or '').strip()
    atoms = body.get('atoms') or []
    bonds = body.get('bonds') or []
    status = body.get('status') or 'draft'

    if not chem_id or not name or not formula:
        return jsonify({'error': 'Vui lòng nhập mã hoá chất, tên và công thức.'}), 400
    if not isinstance(atoms, list) or not atoms:
        return jsonify({'error': 'Cần ít nhất 1 nguyên tử (atoms) để dựng mô hình 3D.'}), 400
    if status not in ('draft', 'published'):
        status = 'draft'

    try:
        mid = lab_bank.create_molecule(
            chem_id, name, formula, atoms, bonds,
            mol_weight=body.get('molWeight'), polar=bool(body.get('polar')),
            bond_angle=body.get('bondAngle'), bonds_desc=body.get('bondsDesc', ''),
            desc=body.get('desc', ''), status=status, created_by=session['username'],
        )
    except Exception:
        return jsonify({'error': 'Mã hoá chất đã tồn tại hoặc dữ liệu không hợp lệ.'}), 400
    return jsonify(lab_bank.get_molecule(mid)), 201


@app.route('/api/admin/lab-molecules/<mid>', methods=['PUT'])
def admin_update_lab_molecule(mid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    if not lab_bank.get_molecule(mid):
        return jsonify({'error': 'Không tìm thấy hoá chất.'}), 404
    body = request.get_json(silent=True) or {}
    if 'status' in body and body['status'] not in ('draft', 'published'):
        return jsonify({'error': 'Trạng thái không hợp lệ.'}), 400
    lab_bank.update_molecule(mid, **body)
    return jsonify(lab_bank.get_molecule(mid))


@app.route('/api/admin/lab-molecules/<mid>', methods=['DELETE'])
def admin_delete_lab_molecule(mid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    ok = lab_bank.delete_molecule(mid)
    if not ok:
        return jsonify({'error': 'Không tìm thấy hoá chất.'}), 404
    return jsonify({'ok': True})


# Admin: CRUD phản ứng hoá học cho tab "Lab 3D" trong trang quản trị.
@app.route('/api/admin/lab-reactions', methods=['GET'])
def admin_list_lab_reactions():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify({'reactions': lab_bank.list_reactions()})


@app.route('/api/admin/lab-reactions', methods=['POST'])
def admin_create_lab_reaction():
    session = _require_admin()
    if not session:
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    eq = (body.get('eq') or '').strip()
    status = body.get('status') or 'draft'
    participants = body.get('participants') or []
    _PHENOMENA = ('', 'sủi', 'khí', 'kết tủa')

    if not eq:
        return jsonify({'error': 'Vui lòng nhập phương trình phản ứng.'}), 400
    if not isinstance(participants, list) or len(participants) < 2:
        return jsonify({'error': 'Cần ít nhất 2 chất tham gia phản ứng.'}), 400
    for p in participants:
        if not isinstance(p, dict) or not (p.get('chemId') or '').strip():
            return jsonify({'error': 'Mỗi chất tham gia cần chọn một hoá chất hợp lệ.'}), 400
    for key in ('beforePhenomenon', 'duringPhenomenon', 'afterPhenomenon'):
        if body.get(key, '') not in _PHENOMENA:
            return jsonify({'error': f'Hiện tượng không hợp lệ: {body.get(key)!r}'}), 400
    if status not in ('draft', 'published'):
        status = 'draft'

    rid = lab_bank.create_reaction(
        eq, type_=body.get('type', ''), conditions=body.get('conditions', ''),
        tools=body.get('tools', ''), steps=body.get('steps', ''), obs=body.get('obs', ''),
        product=body.get('product', ''), grp=body.get('grp', ''),
        participants=participants, needs_heat=bool(body.get('needsHeat')),
        before_color=body.get('beforeColor', '#ffffff'), before_phenomenon=body.get('beforePhenomenon', ''),
        during_color=body.get('duringColor', '#ffffff'), during_phenomenon=body.get('duringPhenomenon', ''),
        after_color=body.get('afterColor', '#ffffff'), after_phenomenon=body.get('afterPhenomenon', ''),
        status=status, created_by=session['username'], premium_only=bool(body.get('premiumOnly')),
    )
    return jsonify(lab_bank.get_reaction(rid)), 201


@app.route('/api/admin/lab-reactions/<rid>', methods=['PUT'])
def admin_update_lab_reaction(rid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    if not lab_bank.get_reaction(rid):
        return jsonify({'error': 'Không tìm thấy phản ứng.'}), 404
    body = request.get_json(silent=True) or {}
    if 'status' in body and body['status'] not in ('draft', 'published'):
        return jsonify({'error': 'Trạng thái không hợp lệ.'}), 400
    if 'participants' in body:
        parts = body['participants']
        if not isinstance(parts, list) or len(parts) < 2:
            return jsonify({'error': 'Cần ít nhất 2 chất tham gia phản ứng.'}), 400
    fields = {k: v for k, v in body.items()
              if k in ('eq', 'type', 'conditions', 'tools', 'steps', 'obs', 'product', 'grp', 'status',
                        'participants', 'needsHeat', 'beforeColor', 'beforePhenomenon',
                        'duringColor', 'duringPhenomenon', 'afterColor', 'afterPhenomenon', 'premiumOnly')}
    lab_bank.update_reaction(rid, **fields)
    return jsonify(lab_bank.get_reaction(rid))


@app.route('/api/admin/lab-reactions/<rid>', methods=['DELETE'])
def admin_delete_lab_reaction(rid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    ok = lab_bank.delete_reaction(rid)
    if not ok:
        return jsonify({'error': 'Không tìm thấy phản ứng.'}), 404
    return jsonify({'ok': True})


## ── Lab 3D: Hóa chất trên KỆ (chemDB) ───────────────────────────────────────
# Public: lab.html gọi ĐỒNG BỘ (XHR) lúc dựng cảnh 3D lúc tải trang, để hóa
# chất admin thêm xuất hiện ngay trên kệ cùng lượt tải, không cần load 2 lần.
@app.route('/api/lab/shelf-chemicals', methods=['GET'])
def public_lab_shelf_chemicals():
    chemicals = lab_bank.list_shelf_chemicals(status='published')
    chemicals = lab_bank.filter_premium_only(chemicals, _resolve_unlimited_from_query())
    return jsonify({'chemicals': chemicals})


# Admin: CRUD hóa chất trên kệ cho tab "Lab 3D" trong trang quản trị.
@app.route('/api/admin/lab-shelf-chemicals', methods=['GET'])
def admin_list_lab_shelf_chemicals():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify({'chemicals': lab_bank.list_shelf_chemicals()})


@app.route('/api/admin/lab-shelf-chemicals', methods=['POST'])
def admin_create_lab_shelf_chemical():
    session = _require_admin()
    if not session:
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    chem_id = (body.get('chemId') or '').strip()
    name = (body.get('name') or '').strip()
    status = body.get('status') or 'draft'

    if not chem_id or not name:
        return jsonify({'error': 'Vui lòng nhập mã hoá chất và tên.'}), 400
    if status not in ('draft', 'published'):
        status = 'draft'

    try:
        # Hóa chất mới đi thẳng vào máy cấp hóa chất (tìm được qua toàn bộ chemDB),
        # không cần gán lên kệ vật lý cụ thể — nên cat để trống ('').
        sid = lab_bank.create_shelf_chemical(
            chem_id, name, cat='', desc=body.get('desc', ''), type_=body.get('type', ''),
            color='#ffffff', ph=None, solid=bool(body.get('solid')),
            is_gas=bool(body.get('isGas')), is_paper=False,
            opacity=None, allowed_states=body.get('allowedStates'),
            status=status, created_by=session['username'], premium_only=bool(body.get('premiumOnly')),
        )
    except Exception:
        return jsonify({'error': 'Mã hoá chất đã tồn tại hoặc dữ liệu không hợp lệ.'}), 400
    return jsonify(lab_bank.get_shelf_chemical(sid)), 201


@app.route('/api/admin/lab-shelf-chemicals/<sid>', methods=['PUT'])
def admin_update_lab_shelf_chemical(sid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    if not lab_bank.get_shelf_chemical(sid):
        return jsonify({'error': 'Không tìm thấy hoá chất.'}), 404
    body = request.get_json(silent=True) or {}
    if 'status' in body and body['status'] not in ('draft', 'published'):
        return jsonify({'error': 'Trạng thái không hợp lệ.'}), 400
    if 'cat' in body and body['cat'] not in ('don', 'voco', 'huuco'):
        return jsonify({'error': 'Kệ (cat) phải là một trong: don, voco, huuco.'}), 400
    lab_bank.update_shelf_chemical(sid, **body)
    return jsonify(lab_bank.get_shelf_chemical(sid))


@app.route('/api/admin/lab-shelf-chemicals/<sid>', methods=['DELETE'])
def admin_delete_lab_shelf_chemical(sid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    ok = lab_bank.delete_shelf_chemical(sid)
    if not ok:
        return jsonify({'error': 'Không tìm thấy hoá chất.'}), 404
    return jsonify({'ok': True})


@app.route('/api/admin-analytics/lab', methods=['GET'])
def analytics_lab():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    now = time.time()
    day_sec = 86400

    runs = fsdb.list_lab_reaction_runs()
    sessions = fsdb.list_lab_sessions()

    by_eq = defaultdict(lambda: {'runs': 0, 'successes': 0, 'failures': 0, 'durations': []})
    for r in runs:
        eq = r.get('reaction_eq') or ''
        g = by_eq[eq]
        g['runs'] += 1
        if r.get('outcome') == 'success':
            g['successes'] += 1
        elif r.get('outcome') in ('failure', 'error'):
            g['failures'] += 1
        if r.get('duration_sec') is not None:
            g['durations'].append(r['duration_sec'])

    popular = [
        {'reaction_eq': eq, 'runs': g['runs'], 'successes': g['successes'], 'failures': g['failures'],
         'avg_duration': (sum(g['durations']) / len(g['durations'])) if g['durations'] else None}
        for eq, g in by_eq.items()
    ]
    popular.sort(key=lambda x: x['runs'], reverse=True)
    popular = popular[:20]

    most_failed = [{'reaction_eq': eq, 'failures': g['failures']} for eq, g in by_eq.items() if g['failures'] > 0]
    most_failed.sort(key=lambda x: x['failures'], reverse=True)
    most_failed = most_failed[:15]

    session_durations = [s['duration_sec'] for s in sessions if s.get('duration_sec') is not None]
    avg_session = sum(session_durations) / len(session_durations) if session_durations else 0

    # ── KPI tổng quan: lượt vào Lab, tỉ lệ hoàn thành nhiệm vụ ──────────
    lab_opened = fsdb.count_events('lab_open')
    lab_completed = fsdb.count_events('lab_complete')
    lab_no_reaction = fsdb.count_events('lab_error')
    completion_rate_pct = round(100 * lab_completed / lab_opened) if lab_opened else 0

    # ── Phân phối điểm đánh giá AI (A-F) — event 'lab_ai_grade' ─────────
    grade_events = fsdb.query_events('lab_ai_grade', since=0, limit=5000)
    grade_counts = Counter()
    scores = []
    for ev in grade_events:
        g = (ev.get('grade') or '').strip().upper()
        if g in ('A', 'B', 'C', 'D', 'F'):
            grade_counts[g] += 1
        s = ev.get('score')
        if isinstance(s, (int, float)):
            scores.append(s)
    total_graded = sum(grade_counts.values())
    c_to_f = grade_counts['C'] + grade_counts['D'] + grade_counts['F']
    c_to_f_pct = round(100 * c_to_f / total_graded, 1) if total_graded else 0
    avg_ai_score = round(sum(scores) / len(scores), 1) if scores else 0

    days = 14
    cf_by_day = [0] * days
    graded_by_day = [0] * days
    for ev in grade_events:
        age = now - ev['ts']
        if age < 0 or age > days * day_sec:
            continue
        idx = days - 1 - int(age // day_sec)
        if 0 <= idx < days:
            graded_by_day[idx] += 1
            if (ev.get('grade') or '').strip().upper() in ('C', 'D', 'F'):
                cf_by_day[idx] += 1

    complete_events = fsdb.query_events('lab_complete', since=0, limit=5000)
    eq_counter = Counter()
    for ev in complete_events:
        eq = (ev.get('eq') or '').strip()
        if eq:
            eq_counter[eq] += 1
    top_completed_reactions = eq_counter.most_common(10)

    return jsonify({
        'kpi': {
            'lab_opened': lab_opened,
            'lab_completed': lab_completed,
            'lab_no_reaction': lab_no_reaction,
            'completion_rate_pct': completion_rate_pct,
            'total_graded': total_graded,
            'avg_ai_score': avg_ai_score,
            'c_to_f_count': c_to_f,
            'c_to_f_pct': c_to_f_pct,
        },
        'grade_distribution': {
            'labels': ['A', 'B', 'C', 'D', 'F'],
            'counts': [grade_counts['A'], grade_counts['B'], grade_counts['C'], grade_counts['D'], grade_counts['F']],
        },
        'cf_trend_14d': {'graded': graded_by_day, 'c_to_f': cf_by_day},
        'top_completed_reactions': top_completed_reactions,
        'popular_reactions': popular,
        'most_failed_reactions': most_failed,
        'avg_session_duration_sec': round(avg_session or 0, 1),
    })


@app.route('/api/admin-analytics/ai', methods=['GET'])
def analytics_ai():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    now = time.time()

    # 'admin_reply_draft' = admin dùng nút "AI viết lại chuyên nghiệp hơn" —
    # đã bị loại sẵn bởi list_conversations_all()/list_all_messages().
    conversations = fsdb.list_conversations_all()
    total_conversations = len(conversations)
    unique_users = len({c['user_id'] for c in conversations if c.get('user_id')})

    user_messages = fsdb.list_all_messages(role='user')
    model_messages = fsdb.list_all_messages(role='model')
    total_questions = len(user_messages)

    ok_model = [m for m in model_messages if m.get('ok')]
    latencies = [m['latency_ms'] for m in ok_model if m.get('latency_ms') is not None]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    error_rate = (sum(1 for m in model_messages if not m.get('ok')) / len(model_messages)) if model_messages else 0

    topic_counter = Counter(c.get('topic') or '(chưa gắn nguồn)' for c in conversations)
    by_topic = [{'topic': t, 'c': n} for t, n in topic_counter.most_common(15)]

    # Tần suất sử dụng AI theo ngày (14 ngày) — đếm câu hỏi thật (role=user),
    # không dùng event 'ai_chat' vì event đó chỉ ghi khi Gemini trả lời OK.
    days = 14
    day_sec = 86400
    buckets = [0] * days
    for m in user_messages:
        ts = m.get('ts', 0)
        age = now - ts
        if age < 0 or age > days * day_sec:
            continue
        idx = days - 1 - int(age // day_sec)
        if 0 <= idx < days:
            buckets[idx] += 1

    # Từ khóa phổ biến — chỉ các nguồn là câu hỏi tự nhiên của học sinh.
    stop = set('và của là ở có cho một các những này đó khi được với thì để làm như từ trong về hay hoặc '
               'thế nào tại sao gì bao nhiêu bạn tôi em ạ nhé nha mình cái con giúp hãy vậy nữa rồi '
               'the a an of is are and or to in on for how why what which'.split())
    words = Counter()
    kw_topics = {'ai_solver', 'lab_molecule_chat', 'lab_assistant_chat', ''}
    for m in user_messages:
        if m.get('topic') not in kw_topics:
            continue
        for w in (m.get('content') or '').split():
            w = w.strip('.,?!:;()[]{}"\'“”‘’').lower()
            if len(w) >= 3 and w not in stop and not w.isdigit():
                words[w] += 1
    top_keywords = words.most_common(30)

    return jsonify({
        'total_conversations': total_conversations,
        'total_questions': total_questions,
        'unique_users': unique_users,
        'avg_latency_ms': round(avg_latency or 0),
        'error_rate_pct': round((error_rate or 0) * 100, 2),
        'by_topic': by_topic,
        'daily_questions_14d': buckets,
        'top_keywords': top_keywords,
    })


# ── Admin: danh sách câu hỏi/cuộc hội thoại AI thật — nguồn dữ liệu chính cho
# bảng "Câu hỏi gần đây" trong module AI Assistant. Hỗ trợ tìm kiếm, lọc theo
# nguồn (topic), và phân trang.
@app.route('/api/admin/ai-conversations', methods=['GET'])
def admin_ai_conversations():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        limit = min(100, max(1, int(request.args.get('limit', 20))))
    except (TypeError, ValueError):
        limit = 20
    search = (request.args.get('search') or '').strip()
    topic = (request.args.get('topic') or '').strip()

    conversations = fsdb.list_conversations_all()

    if topic and topic != 'all':
        conversations = [c for c in conversations if c.get('topic') == topic]
    if search:
        matching_ids = fsdb.search_conversation_ids(search)
        conversations = [c for c in conversations if c['id'] in matching_ids]

    topics_counter = Counter(c.get('topic') or '(chưa gắn nguồn)' for c in fsdb.list_conversations_all())
    topics = [{'topic': t, 'c': n} for t, n in topics_counter.most_common()]

    conversations.sort(key=lambda c: c.get('last_ts', 0), reverse=True)
    total = len(conversations)
    offset = (page - 1) * limit
    page_items = conversations[offset:offset + limit]

    items = [{
        'id': c['id'], 'user_id': c.get('user_id', ''), 'ip': c.get('ip', ''),
        'started_at': c.get('started_at'), 'topic': c.get('topic', ''),
        'first_question': c.get('first_question', ''),
        'message_count': c.get('message_count', 0),
        'last_ts': c.get('last_ts'),
        'has_image': c.get('has_image', False),
        'error_count': c.get('error_count', 0),
    } for c in page_items]

    return jsonify({
        'items': items,
        'total': total,
        'page': page,
        'limit': limit,
        'topics': topics,
    })


# ── Admin: chi tiết đầy đủ 1 cuộc hội thoại — dùng khi admin bấm "Xem" hoặc
# "Phản hồi" để đọc lại toàn bộ ngữ cảnh trước khi soạn email.
@app.route('/api/admin/ai-conversations/<cid>', methods=['GET'])
def admin_ai_conversation_detail(cid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    conv = fsdb.get_conversation(cid)
    if not conv:
        return jsonify({'error': 'Không tìm thấy cuộc hội thoại.'}), 404
    msgs = fsdb.list_messages(cid)
    return jsonify({'conversation': conv, 'messages': msgs})


# ── Static file serving (unchanged) ─────────────────────────────────────────
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/admin')
@app.route('/admin.html')
def serve_admin():
    return send_from_directory('.', 'admin.html')

@app.route('/lab')
@app.route('/lab.html')
def serve_lab():
    return send_from_directory('.', 'lab.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

# ═══════════════════════════════════════════════════════════════════════════
# CHEMCRAFT ADMIN — PATCH cho server.py
# ---------------------------------------------------------------------------
# Dán TOÀN BỘ nội dung file này vào CUỐI file server.py (trước khối
# `if __name__ == '__main__':`). Không cần thay đổi phần trên.
#
# Thêm:
#   • Mở rộng ALLOWED_EVENT_TYPES: 'login', 'logout', 'lesson_start',
#     'quiz_answer', 'ai_followup' (tracker.js hiện tại vẫn hợp lệ).
#   • GET  /api/admin-online-uids     → danh sách UID online trong 5 phút
#   • POST /api/admin-user-activity   → bulk activity theo danh sách UID
#                                       (last_seen, sessions, total_time,
#                                        days_used, page_views, online)
#   • GET  /api/admin-user-profile?uid=…&days=30
#         → tổng hợp toàn bộ dữ liệu cho trang Hồ sơ học sinh
#           (KPI học tập, Lab, AI, heatmap, timeline, per-day, top errors…)
# ═══════════════════════════════════════════════════════════════════════════

# Bổ sung event types mới (thêm vào set hiện có, không phá thứ đã có).
ALLOWED_EVENT_TYPES = ALLOWED_EVENT_TYPES | {
    'login', 'logout',
    'lesson_start',
    'quiz_answer',
    'ai_followup',
    'bug_report',   # Người dùng báo cáo lỗi từ index.html / lab.html
    'lab_ai_grade', # Kết quả chấm điểm AI (A-F) trong modal "Đánh Giá AI" của lab.html
}


def _events_snapshot():
    """Trả về toàn bộ sự kiện (đã phẳng hoá) từ Firestore, dùng cho các hàm
    tổng hợp (xếp hạng, hoạt động, hồ sơ...)."""
    return fsdb.all_events()


def _uid_of(ev):
    return ev.get('user_id') or ev.get('userId') or ''


# ── /api/admin-online-uids ────────────────────────────────────────────────
@app.route('/api/admin-online-uids', methods=['GET'])
def admin_online_uids():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    now = time.time()
    window = float(request.args.get('window', 300))  # 5 phút
    events = fsdb.events_since(now - window)
    seen = {}
    for ev in events:
        uid = _uid_of(ev)
        if not uid:
            continue
        if now - ev['ts'] > window:
            continue
        prev = seen.get(uid, 0)
        if ev['ts'] > prev:
            seen[uid] = ev['ts']
    return jsonify({
        'window_sec': window,
        'online': [{'uid': uid, 'last_seen': ts} for uid, ts in seen.items()],
    })


# ── /api/admin-user-activity ──────────────────────────────────────────────
def _activity_for_uid(events, uid, now):
    """Tính activity tóm tắt cho 1 UID từ mảng events."""
    user_evs = [e for e in events if _uid_of(e) == uid]
    if not user_evs:
        return {
            'uid': uid,
            'last_seen': None,
            'first_seen': None,
            'online': False,
            'sessions': 0,
            'total_time_sec': 0,
            'days_used': 0,
            'page_views': 0,
            'ip': '',
            'events': 0,
        }
    user_evs.sort(key=lambda e: e['ts'])
    last_ts  = user_evs[-1]['ts']
    first_ts = user_evs[0]['ts']

    # sessions: chia theo gap > 30 phút giữa 2 event liên tiếp
    GAP = 30 * 60
    sessions = 1
    total_time = 0
    prev_ts = user_evs[0]['ts']
    session_start = prev_ts
    for e in user_evs[1:]:
        if e['ts'] - prev_ts > GAP:
            total_time += prev_ts - session_start
            sessions += 1
            session_start = e['ts']
        prev_ts = e['ts']
    total_time += prev_ts - session_start

    days = {datetime.fromtimestamp(e['ts']).strftime('%Y-%m-%d') for e in user_evs}

    page_views = sum(1 for e in user_evs if e['type'] == 'page_view')

    return {
        'uid': uid,
        'last_seen': last_ts,
        'first_seen': first_ts,
        'online': (now - last_ts) < 300,
        'sessions': sessions,
        'total_time_sec': int(total_time),
        'days_used': len(days),
        'page_views': page_views,
        'ip': user_evs[-1].get('ip', ''),
        'events': len(user_evs),
    }


def _ts_to_epoch(v):
    """Chuẩn hoá createdAt (có thể là Firestore Timestamp object khi đọc qua
    Admin SDK, hoặc epoch số nếu ghi bằng cách khác) về epoch giây, để trả
    JSON được và so sánh được."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return v.timestamp()
    except Exception:
        return None


_users_stats_cache = {'ts': 0, 'data': None}
_USERS_STATS_TTL = 300  # 5 phút — cùng ý tưởng cache mà admin.html từng làm
                        # ở sessionStorage, nay chuyển vào backend để MỌI
                        # admin cùng hưởng lợi từ 1 cache chung thay vì mỗi
                        # trình duyệt tự cache riêng.


@app.route('/api/admin/users-list', methods=['GET'])
def admin_users_list():
    """Thay thế fetchUsersFromFirestore() cũ (đọc thẳng Firestore từ trình
    duyệt, không cần đăng nhập) — giờ đọc qua Admin SDK, yêu cầu admin token."""
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    docs = fsdb.collection('users').stream()
    users = []
    for d in docs:
        u = d.to_dict() or {}
        u['id'] = d.id
        u['createdAt'] = _ts_to_epoch(u.get('createdAt'))
        users.append(u)
    users.sort(key=lambda x: x.get('createdAt') or 0, reverse=True)
    return jsonify({'users': users})


@app.route('/api/admin/users-stats', methods=['GET'])
def admin_users_stats():
    """Thay thế countUsersFromFirestore() + sumLessonsFromFirestore() +
    truy vấn newUsers 7 ngày + bảng recent users — gộp lại 1 API duy nhất,
    tính 1 lần và cache 5 phút thay vì mỗi widget tự đọc lại toàn bộ
    collection users (vốn đã bị đánh dấu 'khá tốn' ngay trong comment cũ)."""
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    now = time.time()
    if _users_stats_cache['data'] and (now - _users_stats_cache['ts']) < _USERS_STATS_TTL:
        return jsonify(_users_stats_cache['data'])

    users = []
    lessons_sum = 0
    progress_sum = 0.0
    progress_count = 0
    week_ago = now - 7 * 86400
    new_users_week = 0
    for d in fsdb.collection('users').stream():
        u = d.to_dict() or {}
        u['id'] = d.id
        ts = _ts_to_epoch(u.get('createdAt'))
        u['createdAt'] = ts
        if ts and ts >= week_ago:
            new_users_week += 1
        lessons_sum += len(u.get('lessonHistory') or [])
        for v in (u.get('lessonProgress') or {}).values():
            val = v if isinstance(v, (int, float)) else (
                (v.get('percent') if isinstance(v, dict) else None)
                or (v.get('progress') if isinstance(v, dict) else None)
            )
            if isinstance(val, (int, float)):
                progress_sum += val
                progress_count += 1
        users.append(u)
    users.sort(key=lambda x: x.get('createdAt') or 0, reverse=True)

    data = {
        'totalUsers': len(users),
        'newUsersWeek': new_users_week,
        'lessonsSum': lessons_sum,
        'avgProgress': round(progress_sum / progress_count) if progress_count else 0,
        'recentUsers': users[:6],
    }
    _users_stats_cache['ts'] = now
    _users_stats_cache['data'] = data
    return jsonify(data)


@app.route('/api/admin/users/<uid>/update', methods=['POST'])
def admin_update_user(uid):
    """Thay thế saveUserEdit() cũ (updateDoc thẳng từ trình duyệt, không
    đăng nhập) — cho phép sửa displayName/school/class/role qua Admin SDK.
    Giữ nguyên field 'role' tự do (student/teacher/admin/super_admin/...)
    như cũ, KHÔNG ép theo 3 giá trị của module LMS, để không phá vỡ các
    role nội bộ khác đang dùng field này."""
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    ref = fsdb.collection('users').document(uid)
    if not ref.get().exists:
        return jsonify({'error': 'Không tìm thấy người dùng.'}), 404
    updates = {k: body[k] for k in ('displayName', 'school', 'class', 'role') if k in body}
    if updates:
        ref.update(updates)
    _users_stats_cache['data'] = None  # invalidate cache vì danh sách vừa đổi
    return jsonify({'ok': True})


@app.route('/api/admin/users/<uid>/lock', methods=['POST'])
def admin_lock_user(uid):
    """Thay thế toggleLockUser() cũ (updateDoc thẳng từ trình duyệt)."""
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    ref = fsdb.collection('users').document(uid)
    if not ref.get().exists:
        return jsonify({'error': 'Không tìm thấy người dùng.'}), 404
    ref.update({'locked': bool(body.get('locked', True))})
    return jsonify({'ok': True})


@app.route('/api/admin/firestore-ping', methods=['GET'])
def admin_firestore_ping():
    """Thay thế bài kiểm tra kết nối Firestore ở tab Cài đặt (cũ: getDocs
    thẳng từ trình duyệt không đăng nhập — giờ luôn 403 với rules mới)."""
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        list(fsdb.collection('users').limit(1).stream())
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/admin/users-lookup', methods=['POST'])
def admin_users_lookup():
    """Thay thế các getDoc(doc(db,'users',uid)) rải rác trong admin.html
    (AI Assistant, Rankings...) để hiện tên/email học sinh — giờ tra theo
    lô (batch) 1 lần qua Admin SDK thay vì N lượt đọc riêng lẻ từ trình
    duyệt (vốn cũng đã bị firestore.rules mới chặn)."""
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    uids = [str(u) for u in (body.get('uids') or []) if u][:500]
    out = {}
    for uid in uids:
        doc = fsdb.collection('users').document(uid).get()
        if doc.exists:
            d = doc.to_dict() or {}
            out[uid] = {
                'name': d.get('displayName') or d.get('name') or d.get('fullName') or '',
                'email': d.get('email', ''),
            }
        else:
            out[uid] = {'name': '', 'email': ''}
    return jsonify({'users': out})


@app.route('/api/admin-user-activity', methods=['POST'])
def admin_user_activity():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    body = request.get_json(silent=True) or {}
    uids = body.get('uids') or []
    if not isinstance(uids, list):
        return jsonify({'error': 'uids phải là mảng'}), 400
    uids = [str(u) for u in uids if u][:500]  # giới hạn 500 uid/request
    now = time.time()

    # Truy vấn theo từng uid (có index user_id+ts) thay vì tải TOÀN BỘ
    # collection events rồi lọc bằng Python — rẻ hơn rất nhiều khi có đông
    # người dùng, vì chỉ đọc đúng số document của các uid được hỏi.
    result = {}
    for uid in uids:
        subset = fsdb.events_for_user(uid)
        result[uid] = _activity_for_uid(subset, uid, now) if subset else \
            _activity_for_uid([], uid, now)
    return jsonify({'activity': result, 'ts': now})


# ── /api/admin-user-profile ───────────────────────────────────────────────
def _daily_series(events, days, key_filter=None):
    """[{date, count}] cho `days` ngày gần nhất."""
    now = time.time()
    day_sec = 86400
    buckets = [0] * days
    labels  = []
    for i in range(days):
        d = datetime.fromtimestamp(now - (days - 1 - i) * day_sec)
        labels.append(d.strftime('%Y-%m-%d'))
    for ev in events:
        if key_filter and not key_filter(ev):
            continue
        age = now - ev['ts']
        if age < 0 or age > days * day_sec:
            continue
        idx = days - 1 - int(age // day_sec)
        if 0 <= idx < days:
            buckets[idx] += 1
    return [{'date': labels[i], 'count': buckets[i]} for i in range(days)]


def _heatmap_hour_dow(events):
    """[7][24] — 4 tuần gần nhất."""
    now = time.time()
    m = [[0] * 24 for _ in range(7)]
    for ev in events:
        if now - ev['ts'] > 28 * 86400:
            continue
        d = datetime.fromtimestamp(ev['ts'])
        m[d.weekday()][d.hour] += 1
    return m


@app.route('/api/admin-user-profile', methods=['GET'])
def admin_user_profile():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    uid = (request.args.get('uid') or '').strip()
    if not uid:
        return jsonify({'error': 'thiếu uid'}), 400
    days = max(7, min(int(request.args.get('days', 30)), 180))
    now = time.time()

    events = fsdb.events_for_user(uid)
    user_evs = events
    user_evs.sort(key=lambda e: e['ts'])

    if not user_evs:
        return jsonify({
            'uid': uid,
            'empty': True,
            'summary': {},
            'lesson': {}, 'lab': {}, 'ai': {},
            'sessions': {},
            'daily': [], 'heatmap': [[0]*24]*7,
            'timeline': [],
        })

    # ── Activity chung ─────────────────────────────────────
    act = _activity_for_uid(user_evs, uid, now)

    # ── Lesson stats ───────────────────────────────────────
    lesson_starts = [e for e in user_evs if e['type'] in ('lesson_open', 'lesson_start')]
    lesson_done   = [e for e in user_evs if e['type'] == 'lesson_complete']
    scores = [e.get('score') for e in lesson_done if isinstance(e.get('score'), (int, float))]
    lesson_avg_score = round(sum(scores) / len(scores), 1) if scores else None
    lesson_titles = Counter()
    for e in lesson_done:
        t = e.get('title') or e.get('lessonId') or ''
        if t:
            lesson_titles[t] += 1

    # ── Quiz stats ─────────────────────────────────────────
    quiz_done   = [e for e in user_evs if e['type'] == 'quiz_complete']
    quiz_retry  = sum(1 for e in user_evs if e['type'] == 'quiz_start') - len(quiz_done)
    quiz_scores = [e.get('score') for e in quiz_done if isinstance(e.get('score'), (int, float))]
    quiz_avg    = round(sum(quiz_scores) / len(quiz_scores), 1) if quiz_scores else None

    # ── Lab stats ──────────────────────────────────────────
    lab_open  = [e for e in user_evs if e['type'] == 'lab_open']
    lab_done  = [e for e in user_evs if e['type'] == 'lab_complete']
    lab_steps = [e for e in user_evs if e['type'] == 'lab_step']
    lab_err   = [e for e in user_evs if e['type'] == 'lab_error']

    lab_by_name = Counter()
    for e in lab_open:
        n = e.get('lab') or e.get('experiment') or e.get('name') or 'Thí nghiệm'
        lab_by_name[n] += 1
    lab_completion = round(100 * len(lab_done) / max(1, len(lab_open))) if lab_open else 0
    lab_time = 0
    for e in lab_done:
        d = e.get('duration')
        if isinstance(d, (int, float)):
            lab_time += d

    # Bước sai (lab_error) hay xảy ra
    err_steps = Counter()
    for e in lab_err:
        s = e.get('step') or e.get('reason') or 'unknown'
        err_steps[s] += 1

    # ── AI stats ───────────────────────────────────────────
    ai_evs   = [e for e in user_evs if e['type'] == 'ai_chat']
    ai_topics = Counter()
    for e in ai_evs:
        t = (e.get('topic') or '').strip()
        if t:
            ai_topics[t] += 1
    followups = sum(1 for e in user_evs if e['type'] == 'ai_followup')

    # ── Timeline (200 event gần nhất) ──────────────────────
    TL_TYPES = {
        'login': ('Đăng nhập hệ thống', 'fa-right-to-bracket', 'green'),
        'logout': ('Đăng xuất', 'fa-right-from-bracket', 'text-m'),
        'session_start': ('Bắt đầu phiên', 'fa-play', 'cyan'),
        'page_view': ('Xem trang', 'fa-eye', 'text-m'),
        'lesson_start': ('Bắt đầu bài học', 'fa-book-open', 'indigo'),
        'lesson_open': ('Mở bài học', 'fa-book-open', 'indigo'),
        'lesson_complete': ('Hoàn thành bài học', 'fa-book-bookmark', 'green'),
        'quiz_start': ('Bắt đầu Quiz', 'fa-list-check', 'gold'),
        'quiz_complete': ('Hoàn thành Quiz', 'fa-square-check', 'green'),
        'quiz_answer': ('Trả lời câu hỏi', 'fa-pen', 'text-m'),
        'lab_open': ('Vào Lab 3D', 'fa-flask', 'purple'),
        'lab_step': ('Thao tác Lab', 'fa-hand-pointer', 'text-m'),
        'lab_complete': ('Hoàn thành Lab', 'fa-vial-circle-check', 'green'),
        'lab_error': ('Sai bước Lab', 'fa-triangle-exclamation', 'red'),
        'ai_chat': ('Hỏi AI', 'fa-robot', 'cyan'),
        'ai_followup': ('Hỏi tiếp AI', 'fa-comments', 'cyan'),
    }
    timeline = []
    for ev in user_evs[-200:][::-1]:
        meta = TL_TYPES.get(ev['type'], (ev['type'], 'fa-circle-dot', 'text-m'))
        timeline.append({
            'ts':    ev['ts'],
            'type':  ev['type'],
            'label': meta[0],
            'icon':  meta[1],
            'color': meta[2],
            'extra': {k: v for k, v in ev.items()
                      if k not in ('ts', 'type', 'user_id', 'ip')},
            'ip':    ev.get('ip', ''),
        })

    return jsonify({
        'uid': uid,
        'empty': False,
        'summary': act,
        'lesson': {
            'started':   len(lesson_starts),
            'completed': len(lesson_done),
            'in_progress': max(0, len(lesson_starts) - len(lesson_done)),
            'avg_score': lesson_avg_score,
            'top':       lesson_titles.most_common(10),
            'completion_rate':
                round(100 * len(lesson_done) / max(1, len(lesson_starts))) if lesson_starts else 0,
        },
        'quiz': {
            'done':    len(quiz_done),
            'retries': max(0, quiz_retry),
            'avg':     quiz_avg,
        },
        'lab': {
            'opened':      len(lab_open),
            'completed':   len(lab_done),
            'unfinished':  max(0, len(lab_open) - len(lab_done)),
            'steps':       len(lab_steps),
            'errors':      len(lab_err),
            'total_time':  int(lab_time),
            'completion':  lab_completion,
            'by_name':     lab_by_name.most_common(10),
            'error_steps': err_steps.most_common(10),
        },
        'ai': {
            'count':     len(ai_evs),
            'topics':    ai_topics.most_common(10),
            'followups': followups,
        },
        'daily':   _daily_series(user_evs, days),
        'heatmap': _heatmap_hour_dow(user_evs),
        'timeline': timeline,
    })


# ── /api/admin-analytics/learning ───────────────────────────────────────────
# Dữ liệu THẬT cho tab "Learning Analytics": KPI tổng quan (đăng nhập, học
# bài, quiz, lab, hoàn thành), funnel theo user thật, tăng trưởng người dùng
# (dựa vào lần đầu xuất hiện của mỗi user_id), phân phối thời gian học/session
# (tách session theo khoảng nghỉ > 30 phút, giống logic _activity_for_uid),
# và heatmap hoạt động theo giờ/ngày trong 28 ngày gần nhất (bỏ 'heartbeat'
# để không bị nhiễu bởi nhịp tim mỗi 60s khi mở tab nền).
@app.route('/api/admin-analytics/learning', methods=['GET'])
def analytics_learning():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    now = time.time()
    day_sec = 86400

    events = fsdb.all_events()
    quiz_attempts = fsdb.list_quiz_attempts()
    lab_sessions = fsdb.list_lab_sessions()

    def _distinct_users(evs):
        return {e.get('user_id') for e in evs if e.get('user_id')}

    # ── KPI tổng quan ────────────────────────────────────────────────────
    total_students = len(_distinct_users(events))
    active_7d = len(_distinct_users([e for e in events if e.get('ts', 0) >= now - 7 * day_sec]))
    active_30d = len(_distinct_users([e for e in events if e.get('ts', 0) >= now - 30 * day_sec]))
    lessons_completed = sum(1 for e in events if e.get('type') == 'lesson_complete')
    quiz_completed = sum(1 for a in quiz_attempts if a.get('finished_at') is not None)
    lab_opened = sum(1 for e in events if e.get('type') == 'lab_open')
    lab_completed = sum(1 for e in events if e.get('type') == 'lab_complete')

    # ── Funnel theo user thật ────────────────────────────────────────────
    users_logged_in = _distinct_users(events)
    users_lesson = _distinct_users([e for e in events if e.get('type') in ('lesson_start', 'lesson_open', 'lesson_complete')])
    users_quiz = {a.get('user_id') for a in quiz_attempts if a.get('user_id')}
    users_lab = {s.get('user_id') for s in lab_sessions if s.get('user_id')}
    users_done = _distinct_users([e for e in events if e.get('type') == 'lab_complete'])
    base = max(1, len(users_logged_in))
    funnel = [
        {'label': 'Đăng nhập',  'count': len(users_logged_in), 'pct': 100},
        {'label': 'Học bài',    'count': len(users_lesson),    'pct': round(100 * len(users_lesson) / base)},
        {'label': 'Làm Quiz',   'count': len(users_quiz),      'pct': round(100 * len(users_quiz) / base)},
        {'label': 'Vào Lab',    'count': len(users_lab),       'pct': round(100 * len(users_lab) / base)},
        {'label': 'Hoàn thành', 'count': len(users_done),      'pct': round(100 * len(users_done) / base)},
    ]

    # ── Tăng trưởng người dùng: ngày xuất hiện lần đầu của mỗi user_id ──
    growth_days = 30
    first_seen = {}
    for e in events:
        uid = e.get('user_id')
        if not uid:
            continue
        ts = e.get('ts', 0)
        if uid not in first_seen or ts < first_seen[uid]:
            first_seen[uid] = ts
    users_before_window = 0
    new_by_day = [0] * growth_days
    window_start = now - growth_days * day_sec
    for first_ts in first_seen.values():
        if first_ts < window_start:
            users_before_window += 1
            continue
        age = now - first_ts
        idx = growth_days - 1 - int(age // day_sec)
        if 0 <= idx < growth_days:
            new_by_day[idx] += 1
    labels = [
        datetime.fromtimestamp(now - (growth_days - 1 - i) * day_sec).strftime('%d/%m')
        for i in range(growth_days)
    ]
    cumulative = []
    running = users_before_window
    for n in new_by_day:
        running += n
        cumulative.append(running)

    # ── Phân phối thời gian học (phút/session), tách session theo gap 30' ──
    session_events = sorted(
        (e for e in events if e.get('user_id') and e.get('type') != 'heartbeat' and e.get('ts', 0) >= now - 60 * day_sec),
        key=lambda e: (e['user_id'], e['ts'])
    )
    GAP = 30 * 60
    durations_min = []
    cur_uid, seg_start, prev_ts = None, None, None
    for e in session_events:
        uid, ts = e['user_id'], e['ts']
        if uid != cur_uid:
            if cur_uid is not None and prev_ts is not None and seg_start is not None:
                durations_min.append((prev_ts - seg_start) / 60.0)
            cur_uid, seg_start, prev_ts = uid, ts, ts
            continue
        if ts - prev_ts > GAP:
            durations_min.append((prev_ts - seg_start) / 60.0)
            seg_start = ts
        prev_ts = ts
    if cur_uid is not None and prev_ts is not None and seg_start is not None:
        durations_min.append((prev_ts - seg_start) / 60.0)

    buckets_def = [(0, 5), (5, 10), (10, 20), (20, 30), (30, 45), (45, 60), (60, None)]
    bucket_labels = ['0-5', '5-10', '10-20', '20-30', '30-45', '45-60', '>60']
    bucket_counts = [0] * len(buckets_def)
    for d in durations_min:
        for i, (lo, hi) in enumerate(buckets_def):
            if d >= lo and (hi is None or d < hi):
                bucket_counts[i] += 1
                break
    avg_session_min = round(sum(durations_min) / len(durations_min), 1) if durations_min else 0

    # ── Heatmap giờ × ngày trong tuần, 28 ngày gần nhất (bỏ heartbeat) ──
    heat_rows = [e for e in events if e.get('ts', 0) >= now - 28 * day_sec and e.get('type') != 'heartbeat']
    heatmap = [[0] * 24 for _ in range(7)]
    for r in heat_rows:
        d = datetime.fromtimestamp(r['ts'])
        heatmap[d.weekday()][d.hour] += 1

    return jsonify({
        'kpi': {
            'total_students': total_students,
            'active_7d': active_7d,
            'active_30d': active_30d,
            'lessons_completed': lessons_completed,
            'quiz_completed': quiz_completed,
            'lab_opened': lab_opened,
            'lab_completed': lab_completed,
            'avg_session_min': avg_session_min,
        },
        'funnel': funnel,
        'growth': {'labels': labels, 'new_users': new_by_day, 'cumulative': cumulative},
        'session_distribution': {'labels': bucket_labels, 'counts': bucket_counts},
        'heatmap': heatmap,
    })


# ── /api/admin-rankings ────────────────────────────────────────────────────
# Xếp hạng học sinh theo 3 tiêu chí:
#   • lab      : số thí nghiệm đã thực hiện (lab_open) và hoàn thành (lab_complete)
#   • quiz     : điểm quiz cao nhất + điểm trung bình
#   • bugs     : số lượt báo cáo lỗi phòng thí nghiệm
# Trả về top N (mặc định 50) cho mỗi bảng, kèm khung thời gian tuỳ chọn.
@app.route('/api/admin-rankings', methods=['GET'])
def admin_rankings():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401

    days  = int(request.args.get('days', 30) or 30)      # 0 = all-time
    limit = max(1, min(int(request.args.get('limit', 50)), 200))
    now   = time.time()
    cutoff = 0 if days <= 0 else now - days * 86400

    # Chỉ đọc đúng khung thời gian cần (thay vì toàn bộ lịch sử) — rẻ hơn
    # rất nhiều khi collection events lớn dần theo thời gian.
    events = fsdb.events_since(cutoff) if cutoff else fsdb.all_events()

    # ── Bảng 1: Lab experiments ─────────────────────────────────
    lab_stats = defaultdict(lambda: {
        'opened': 0, 'completed': 0, 'errors': 0,
        'last_ts': 0, 'labs': set(),
    })
    for e in events:
        uid = _uid_of(e)
        if not uid: continue
        t = e['type']
        if t in ('lab_open', 'lab_complete', 'lab_error'):
            row = lab_stats[uid]
            if t == 'lab_open':
                row['opened'] += 1
                name = e.get('lab') or e.get('experiment') or e.get('name') or ''
                if name: row['labs'].add(name)
            elif t == 'lab_complete':
                row['completed'] += 1
            elif t == 'lab_error':
                row['errors'] += 1
            row['last_ts'] = max(row['last_ts'], e['ts'])

    lab_board = []
    for uid, r in lab_stats.items():
        lab_board.append({
            'uid':          uid,
            'opened':       r['opened'],
            'completed':    r['completed'],
            'errors':       r['errors'],
            'unique_labs':  len(r['labs']),
            'completion':   round(100 * r['completed'] / r['opened']) if r['opened'] else 0,
            'last_ts':      r['last_ts'],
        })
    lab_board.sort(key=lambda x: (-x['opened'], -x['completed'], -x['last_ts']))
    lab_board = lab_board[:limit]

    # ── Bảng 2: Quiz scores ─────────────────────────────────────
    quiz_stats = defaultdict(lambda: {
        'attempts': 0, 'scores': [], 'best': None, 'last_ts': 0,
    })
    for e in events:
        if e['type'] != 'quiz_complete': continue
        uid = _uid_of(e)
        if not uid: continue
        row = quiz_stats[uid]
        row['attempts'] += 1
        row['last_ts'] = max(row['last_ts'], e['ts'])
        s = e.get('score')
        if isinstance(s, (int, float)):
            row['scores'].append(float(s))
            if row['best'] is None or s > row['best']:
                row['best'] = float(s)

    quiz_board = []
    for uid, r in quiz_stats.items():
        avg = round(sum(r['scores']) / len(r['scores']), 1) if r['scores'] else None
        quiz_board.append({
            'uid':      uid,
            'attempts': r['attempts'],
            'best':     r['best'],
            'avg':      avg,
            'last_ts':  r['last_ts'],
        })
    quiz_board.sort(
        key=lambda x: (
            -(x['best']     if x['best'] is not None else -1),
            -(x['avg']      if x['avg']  is not None else -1),
            -x['attempts'],
        )
    )
    quiz_board = quiz_board[:limit]

    # ── Bảng 3: Bug reports ─────────────────────────────────────
    bug_stats = defaultdict(lambda: {
        'count': 0, 'last_ts': 0, 'samples': [], 'severities': Counter(),
    })
    for e in events:
        if e['type'] != 'bug_report': continue
        uid = _uid_of(e)
        if not uid: continue
        row = bug_stats[uid]
        row['count'] += 1
        row['last_ts'] = max(row['last_ts'], e['ts'])
        sev = (e.get('severity') or 'normal').lower()
        row['severities'][sev] += 1
        title = (e.get('title') or e.get('message') or '').strip()
        if title and len(row['samples']) < 3:
            row['samples'].append(title[:140])

    bug_board = []
    for uid, r in bug_stats.items():
        bug_board.append({
            'uid':        uid,
            'count':      r['count'],
            'last_ts':    r['last_ts'],
            'severities': dict(r['severities']),
            'samples':    r['samples'],
        })
    bug_board.sort(key=lambda x: (-x['count'], -x['last_ts']))
    bug_board = bug_board[:limit]

    return jsonify({
        'ts':       now,
        'days':     days,
        'lab':      lab_board,
        'quiz':     quiz_board,
        'bugs':     bug_board,
        'totals': {
            'lab_users':  len(lab_stats),
            'quiz_users': len(quiz_stats),
            'bug_users':  len(bug_stats),
        },
    })


# ── /api/admin-notifications ────────────────────────────────────────────────
# Chuông thông báo trong Admin Dashboard: hiện danh sách báo lỗi (bug_report)
# gần đây kèm đầy đủ thông tin người báo (email, tên, uid), và số lượng
# thông báo "chưa xem" (so với lần cuối admin bấm vào chuông).
@app.route('/api/admin-notifications', methods=['GET'])
def admin_notifications():
    session = _require_admin()
    if not session:
        return jsonify({'error': 'unauthorized'}), 401

    limit = max(1, min(int(request.args.get('limit', 30)), 100))
    admin_id = session.get('admin_id')

    conn = db.get_conn()
    try:
        row = conn.execute(
            'SELECT last_seen_ts FROM admin_notif_state WHERE admin_id = ?', (admin_id,)
        ).fetchone()
    finally:
        conn.close()
    last_seen_ts = row['last_seen_ts'] if row else 0

    events = fsdb.query_events('bug_report', since=0, limit=max(limit, 200))
    events.sort(key=lambda e: e['ts'], reverse=True)
    unread = sum(1 for e in events if e['ts'] > last_seen_ts)
    events = events[:limit]

    items = [{
        'ts':       e['ts'],
        'unread':   e['ts'] > last_seen_ts,
        'uid':      _uid_of(e),
        'email':    e.get('email') or '',
        'name':     e.get('name') or e.get('displayName') or '',
        'title':    e.get('title') or '',
        'message':  e.get('message') or '',
        'severity': (e.get('severity') or 'normal'),
        'area':     e.get('area') or '',
        'path':     e.get('path') or '',
    } for e in events]

    return jsonify({'items': items, 'unread': unread, 'last_seen_ts': last_seen_ts})


@app.route('/api/admin-notifications/mark-read', methods=['POST'])
def admin_notifications_mark_read():
    session = _require_admin()
    if not session:
        return jsonify({'error': 'unauthorized'}), 401
    admin_id = session.get('admin_id')
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            'INSERT INTO admin_notif_state (admin_id, last_seen_ts) VALUES (?, ?) '
            'ON CONFLICT(admin_id) DO UPDATE SET last_seen_ts = excluded.last_seen_ts',
            (admin_id, now)
        )
    return jsonify({'ok': True, 'last_seen_ts': now})


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    print(f'🚀 ChemCraft backend đang chạy tại http://localhost:{port}')
    print(f'   → Database: {db.DB_PATH}')
    app.run(host='0.0.0.0', port=port, debug=debug)
