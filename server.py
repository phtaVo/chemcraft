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

load_dotenv()

app = Flask(__name__, static_folder='.')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

db.init_db()
admin_auth.bootstrap_default_admin()
_migrated = db.migrate_jsonl_events(os.getenv('EVENT_LOG_PATH', 'events.jsonl'))
if _migrated:
    print(f'📦 Migrated {_migrated} legacy events from events.jsonl into SQLite.')
_seeded_questions = quiz_bank.seed_default_questions()
if _seeded_questions:
    print(f'📦 Seeded {_seeded_questions} legacy quiz questions into quiz_questions table.')

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

    # Ensure a conversation row exists so multi-turn chats are threaded,
    # not just isolated question/answer events like before.
    with db.tx() as conn:
        if conversation_id:
            row = conn.execute('SELECT id FROM ai_conversations WHERE id = ?', (conversation_id,)).fetchone()
        else:
            row = None
        if not row:
            cur = conn.execute(
                'INSERT INTO ai_conversations (user_id, ip, started_at, topic) VALUES (?, ?, ?, ?)',
                (user_id, ip, time.time(), topic)
            )
            conversation_id = cur.lastrowid

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

        with db.tx() as conn:
            conn.execute(
                'INSERT INTO ai_messages (conversation_id, role, content, has_image, ok, ts) '
                'VALUES (?, ?, ?, ?, 1, ?)',
                (conversation_id, 'user', question_text.strip()[:2000], int(has_image), time.time())
            )

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

        with db.tx() as conn:
            conn.execute(
                'INSERT INTO ai_messages (conversation_id, role, content, latency_ms, ok, ts) '
                'VALUES (?, ?, ?, ?, 1, ?)',
                (conversation_id, 'model', full_text[:4000], latency_ms, time.time())
            )

        db.record_event('ai_chat', {
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
        with db.tx() as conn:
            conn.execute(
                'INSERT INTO ai_messages (conversation_id, role, ok, error_msg, ts) VALUES (?, ?, 0, ?, ?)',
                (conversation_id, 'model', msg, time.time())
            )
        db.record_event('ai_error', {'error': msg, 'conversation_id': conversation_id}, user_id=user_id, ip=ip)
        if msg == 'RATE_LIMIT':
            return jsonify({'error': 'Hệ thống AI đang bận. Vui lòng thử lại sau vài giây.'}), 429
        return jsonify({'error': msg}), 500
    except requests.exceptions.Timeout:
        with _runtime_lock:
            _runtime['chat_errors'] += 1
        db.record_event('ai_error', {'error': 'timeout', 'conversation_id': conversation_id}, user_id=user_id, ip=ip)
        return jsonify({'error': 'Yêu cầu mất quá nhiều thời gian. Vui lòng thử lại.'}), 504
    except Exception as e:
        app.logger.error('Chat error: %s', e)
        with _runtime_lock:
            _runtime['chat_errors'] += 1
        db.record_event('ai_error', {'error': str(e), 'conversation_id': conversation_id}, user_id=user_id, ip=ip)
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

    db.record_event('admin_reply_email_sent', {
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
    db.record_event(ev_type, payload, user_id=user_id, ip=ip)

    # Structured side-tables for the event types that feed dedicated analytics.
    # Returns the new row id for session/attempt-opening events so the client
    # can thread subsequent events (lab_reaction_result, quiz_answer, ...)
    # back to the right session/attempt — see tracker.js ccLab / ccQuiz.
    new_id = _fanout_structured_tables(ev_type, payload, user_id)

    resp = {'ok': True}
    if ev_type == 'lab_open':
        resp['sessionId'] = new_id
    elif ev_type == 'quiz_start':
        resp['attemptId'] = new_id
    return jsonify(resp)


def _fanout_structured_tables(ev_type: str, payload: dict, user_id: str):
    """Write into the typed tables (quiz_attempts, lab_sessions, ...) so
    analytics queries are simple SQL instead of scanning the generic event
    log every time. Called synchronously — cheap, single-row inserts.
    Returns the new row id when the event type opens a session/attempt."""
    ts = time.time()
    with db.tx() as conn:
        if ev_type == 'quiz_start':
            cur = conn.execute(
                'INSERT INTO quiz_attempts (user_id, started_at, total_q) VALUES (?, ?, ?)',
                (user_id, ts, payload.get('totalQuestions'))
            )
            return cur.lastrowid
        elif ev_type == 'quiz_answer':
            attempt_id = payload.get('attemptId')
            if attempt_id:
                conn.execute(
                    'INSERT INTO quiz_answers (attempt_id, question_id, question_text, is_correct, '
                    'retry_count, duration_sec, answered_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (attempt_id, payload.get('questionId', ''), payload.get('questionText', ''),
                     int(bool(payload.get('correct'))), payload.get('retryCount', 0),
                     payload.get('durationSec'), ts)
                )
        elif ev_type == 'quiz_complete':
            attempt_id = payload.get('attemptId')
            if attempt_id:
                conn.execute(
                    'UPDATE quiz_attempts SET finished_at = ?, correct_q = ?, duration_sec = ? WHERE id = ?',
                    (ts, payload.get('correctCount'), payload.get('durationSec'), attempt_id)
                )
        elif ev_type == 'lab_open':
            cur = conn.execute(
                'INSERT INTO lab_sessions (user_id, started_at) VALUES (?, ?)', (user_id, ts)
            )
            return cur.lastrowid
        elif ev_type == 'lab_close':
            session_id = payload.get('sessionId')
            if session_id:
                conn.execute(
                    'UPDATE lab_sessions SET ended_at = ?, duration_sec = ? WHERE id = ?',
                    (ts, payload.get('durationSec'), session_id)
                )
        elif ev_type == 'lab_reaction_result':
            conn.execute(
                'INSERT INTO lab_reaction_runs (session_id, user_id, reaction_eq, chemicals, equipment, '
                'outcome, error_reason, duration_sec, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (payload.get('sessionId', 0), user_id, payload.get('reactionEq', ''),
                 payload.get('chemicals', '[]'), payload.get('equipment', '[]'),
                 payload.get('outcome', 'unknown'), payload.get('errorReason'),
                 payload.get('durationSec'), ts)
            )
    return None


# ── /api/admin-metrics — real SQL aggregation, no mock fallback ────────────
@app.route('/api/admin-metrics', methods=['GET'])
def admin_metrics():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    conn = db.get_conn()
    now = time.time()

    with _runtime_lock:
        active_ips = len({ip for ip, t in _runtime['active_ips'].items() if now - t < 300})
        chat_total, chat_ok, chat_errors = _runtime['chat_total'], _runtime['chat_ok'], _runtime['chat_errors']
        gemini_errors, rate_limit_hits = _runtime['gemini_errors'], _runtime['rate_limit_hits']

    ai_chats      = db.count_events('ai_chat')
    lessons_done  = db.count_events('lesson_complete')
    quiz_done     = db.count_events('quiz_complete')
    lab_open      = db.count_events('lab_open')
    lab_completed = db.count_events('lab_complete')
    lab_completion = round(100 * lab_completed / max(1, lab_open)) if lab_open else 0
    events_total  = conn.execute('SELECT COUNT(*) c FROM events').fetchone()['c']

    daily_activity = db.bucket_by_day('page_view', 7)
    daily_ai       = db.bucket_by_day('ai_chat', 7)
    daily_lab      = db.bucket_by_day('lab_open', 7)
    daily_lesson   = db.bucket_by_day('lesson_complete', 7)

    donut = {'Bài học': lessons_done, 'Quiz': quiz_done, 'Lab 3D': lab_open, 'AI': ai_chats}

    # Top AI questions — real query against ai_messages, not the old
    # event-payload scan.
    q_rows = conn.execute(
        "SELECT content, COUNT(*) c FROM ai_messages WHERE role='user' AND length(content) > 8 "
        "GROUP BY content ORDER BY c DESC LIMIT 10"
    ).fetchall()
    top_questions = [(r['content'][:120], r['c']) for r in q_rows]

    stop = set('và của là ở có cho một các những này đó khi được với thì để làm như từ trong về hay hoặc '
               'thế nào tại sao gì bao nhiêu the a an of is are and or to in on for how why what which'.split())
    words = Counter()
    for r in conn.execute("SELECT content FROM ai_messages WHERE role='user'").fetchall():
        for w in (r['content'] or '').split():
            w = w.strip('.,?!:;()[]"\'').lower()
            if len(w) >= 3 and w not in stop:
                words[w] += 1
    word_cloud = words.most_common(30)

    # Heatmap hour x weekday, last 28 days — real SQL over `events`
    heat_rows = conn.execute(
        "SELECT ts FROM events WHERE ts >= ?", (now - 28 * 86400,)
    ).fetchall()
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


# ── /api/admin-events — raw event query (now backed by SQL, not a deque) ───
@app.route('/api/admin-events', methods=['GET'])
def admin_events():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    ev_type = request.args.get('type', '')
    limit   = min(int(request.args.get('limit', 200)), 2000)
    since   = float(request.args.get('since', 0))
    events = db.query_events(ev_type, since, limit)
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
    conn = db.get_conn()

    total_attempts = conn.execute('SELECT COUNT(*) c FROM quiz_attempts').fetchone()['c']
    finished_attempts = conn.execute(
        'SELECT COUNT(*) c FROM quiz_attempts WHERE finished_at IS NOT NULL'
    ).fetchone()['c']
    avg_score = conn.execute(
        'SELECT AVG(1.0 * correct_q / NULLIF(total_q, 0)) a FROM quiz_attempts WHERE finished_at IS NOT NULL'
    ).fetchone()['a']
    avg_attempt_duration = conn.execute(
        'SELECT AVG(duration_sec) a FROM quiz_attempts WHERE finished_at IS NOT NULL'
    ).fetchone()['a']

    rows = conn.execute('''
        SELECT
            question_id,
            question_text,
            COUNT(*)                                            AS attempts,
            SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)      AS correct_n,
            SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END)      AS wrong_n,
            AVG(duration_sec)                                    AS avg_time,
            AVG(retry_count)                                     AS avg_retries
        FROM quiz_answers
        WHERE question_id IS NOT NULL AND question_id != ''
        GROUP BY question_id, question_text
    ''').fetchall()

    bank_difficulty = {}
    try:
        bank_rows = conn.execute('SELECT id, difficulty FROM quiz_questions').fetchall()
        bank_difficulty = {str(r['id']): r['difficulty'] for r in bank_rows}
    except Exception:
        pass

    questions = []
    for r in rows:
        attempts = r['attempts'] or 0
        correct_n = r['correct_n'] or 0
        wrong_n = r['wrong_n'] or 0
        questions.append({
            'question_id':  r['question_id'],
            'text':         r['question_text'],
            'attempts':     attempts,
            'correct_pct':  round(100.0 * correct_n / attempts, 1) if attempts else 0,
            'wrong_pct':    round(100.0 * wrong_n / attempts, 1) if attempts else 0,
            'avg_time_sec': round(r['avg_time'], 1) if r['avg_time'] is not None else None,
            'avg_retries':  round(r['avg_retries'], 2) if r['avg_retries'] is not None else 0,
            'difficulty':   bank_difficulty.get(str(r['question_id'])),
        })
    questions.sort(key=lambda q: q['wrong_pct'], reverse=True)

    overall_avg_time = conn.execute(
        'SELECT AVG(duration_sec) a FROM quiz_answers WHERE duration_sec IS NOT NULL'
    ).fetchone()['a']
    overall_avg_retries = conn.execute('SELECT AVG(retry_count) a FROM quiz_answers').fetchone()['a']

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
    return jsonify({'questions': quiz_bank.random_questions(count)})


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


@app.route('/api/admin/quiz-questions/<int:qid>', methods=['PUT'])
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


@app.route('/api/admin/quiz-questions/<int:qid>', methods=['DELETE'])
def admin_delete_quiz_question(qid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    ok = quiz_bank.delete_question(qid)
    if not ok:
        return jsonify({'error': 'Không tìm thấy câu hỏi.'}), 404
    return jsonify({'ok': True})


@app.route('/api/admin-analytics/lab', methods=['GET'])
def analytics_lab():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    conn = db.get_conn()
    popular = conn.execute(
        'SELECT reaction_eq, COUNT(*) runs, '
        'SUM(CASE WHEN outcome="success" THEN 1 ELSE 0 END) successes, '
        'SUM(CASE WHEN outcome="failure" OR outcome="error" THEN 1 ELSE 0 END) failures, '
        'AVG(duration_sec) avg_duration '
        'FROM lab_reaction_runs GROUP BY reaction_eq ORDER BY runs DESC LIMIT 20'
    ).fetchall()
    most_failed = conn.execute(
        'SELECT reaction_eq, COUNT(*) failures FROM lab_reaction_runs '
        'WHERE outcome IN ("failure","error") GROUP BY reaction_eq ORDER BY failures DESC LIMIT 15'
    ).fetchall()
    avg_session = conn.execute(
        'SELECT AVG(duration_sec) a FROM lab_sessions WHERE duration_sec IS NOT NULL'
    ).fetchone()['a']
    return jsonify({
        'popular_reactions': [dict(r) for r in popular],
        'most_failed_reactions': [dict(r) for r in most_failed],
        'avg_session_duration_sec': round(avg_session or 0, 1),
    })


@app.route('/api/admin-analytics/ai', methods=['GET'])
def analytics_ai():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    conn = db.get_conn()
    now = time.time()

    # 'admin_reply_draft' = admin dùng nút "AI viết lại chuyên nghiệp hơn" trong
    # modal Phản hồi (AI Assistant module) — không phải câu hỏi của học sinh nên
    # loại khỏi mọi số liệu thống kê bên dưới.
    total_conversations = conn.execute(
        "SELECT COUNT(*) c FROM ai_conversations WHERE topic != 'admin_reply_draft'"
    ).fetchone()['c']
    total_questions = conn.execute(
        "SELECT COUNT(*) c FROM ai_messages m JOIN ai_conversations c ON c.id = m.conversation_id "
        "WHERE m.role='user' AND c.topic != 'admin_reply_draft'"
    ).fetchone()['c']
    unique_users = conn.execute(
        "SELECT COUNT(DISTINCT user_id) c FROM ai_conversations "
        "WHERE user_id != '' AND topic != 'admin_reply_draft'"
    ).fetchone()['c']
    avg_latency = conn.execute(
        "SELECT AVG(latency_ms) a FROM ai_messages WHERE role='model' AND ok=1"
    ).fetchone()['a']
    error_rate = conn.execute(
        "SELECT 1.0 * SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) / COUNT(*) r FROM ai_messages WHERE role='model'"
    ).fetchone()['r']
    by_topic = conn.execute(
        "SELECT COALESCE(NULLIF(topic,''),'(chưa gắn nguồn)') topic, COUNT(*) c "
        "FROM ai_conversations WHERE topic != 'admin_reply_draft' GROUP BY topic ORDER BY c DESC LIMIT 15"
    ).fetchall()

    # Tần suất sử dụng AI theo ngày (14 ngày gần nhất) — đếm số CÂU HỎI thật
    # (ai_messages role='user'), không dùng event 'ai_chat' vì event đó chỉ được
    # ghi khi Gemini trả lời thành công, sẽ thiếu các lượt hỏi bị lỗi.
    days = 14
    day_sec = 86400
    buckets = [0] * days
    day_rows = conn.execute(
        "SELECT m.ts ts FROM ai_messages m JOIN ai_conversations c ON c.id = m.conversation_id "
        "WHERE m.role='user' AND m.ts >= ? AND c.topic != 'admin_reply_draft'",
        (now - days * day_sec,)
    ).fetchall()
    for r in day_rows:
        age = now - r['ts']
        idx = days - 1 - int(age // day_sec)
        if 0 <= idx < days:
            buckets[idx] += 1

    # Từ khóa phổ biến — chỉ quét các nguồn là câu hỏi tự nhiên của học sinh,
    # bỏ qua lab_reaction_predict/lab_ai_assessment (prompt kỹ thuật/JSON nội bộ,
    # không phản ánh học sinh đang thắc mắc về nội dung gì).
    stop = set('và của là ở có cho một các những này đó khi được với thì để làm như từ trong về hay hoặc '
               'thế nào tại sao gì bao nhiêu bạn tôi em ạ nhé nha mình cái con giúp hãy vậy nữa rồi '
               'the a an of is are and or to in on for how why what which'.split())
    words = Counter()
    kw_rows = conn.execute(
        "SELECT m.content content FROM ai_messages m JOIN ai_conversations c ON c.id = m.conversation_id "
        "WHERE m.role='user' AND c.topic IN ('ai_solver','lab_molecule_chat','lab_assistant_chat','')"
    ).fetchall()
    for r in kw_rows:
        for w in (r['content'] or '').split():
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
        'by_topic': [dict(r) for r in by_topic],
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
    conn = db.get_conn()

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        limit = min(100, max(1, int(request.args.get('limit', 20))))
    except (TypeError, ValueError):
        limit = 20
    offset = (page - 1) * limit
    search = (request.args.get('search') or '').strip()
    topic = (request.args.get('topic') or '').strip()

    where = ["c.topic != 'admin_reply_draft'"]
    params = []
    if topic and topic != 'all':
        where.append('c.topic = ?')
        params.append(topic)
    if search:
        where.append(
            "EXISTS (SELECT 1 FROM ai_messages m WHERE m.conversation_id = c.id "
            "AND m.role='user' AND m.content LIKE ?)"
        )
        params.append(f'%{search}%')
    where_sql = ' AND '.join(where)

    total = conn.execute(f'SELECT COUNT(*) c FROM ai_conversations c WHERE {where_sql}', params).fetchone()['c']

    rows = conn.execute(f'''
        SELECT
            c.id, c.user_id, c.ip, c.started_at, c.topic,
            (SELECT content FROM ai_messages m WHERE m.conversation_id = c.id AND m.role='user'
             ORDER BY m.ts ASC LIMIT 1) AS first_question,
            (SELECT COUNT(*) FROM ai_messages m WHERE m.conversation_id = c.id) AS message_count,
            (SELECT MAX(ts) FROM ai_messages m WHERE m.conversation_id = c.id) AS last_ts,
            (SELECT MAX(has_image) FROM ai_messages m WHERE m.conversation_id = c.id AND m.role='user') AS has_image,
            (SELECT SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) FROM ai_messages m
             WHERE m.conversation_id = c.id AND m.role='model') AS error_count
        FROM ai_conversations c
        WHERE {where_sql}
        ORDER BY last_ts DESC
        LIMIT ? OFFSET ?
    ''', params + [limit, offset]).fetchall()

    topics = conn.execute(
        "SELECT COALESCE(NULLIF(topic,''),'(chưa gắn nguồn)') topic, COUNT(*) c FROM ai_conversations "
        "WHERE topic != 'admin_reply_draft' GROUP BY topic ORDER BY c DESC"
    ).fetchall()

    return jsonify({
        'items': [dict(r) for r in rows],
        'total': total,
        'page': page,
        'limit': limit,
        'topics': [dict(r) for r in topics],
    })


# ── Admin: chi tiết đầy đủ 1 cuộc hội thoại — dùng khi admin bấm "Xem" hoặc
# "Phản hồi" để đọc lại toàn bộ ngữ cảnh trước khi soạn email.
@app.route('/api/admin/ai-conversations/<int:cid>', methods=['GET'])
def admin_ai_conversation_detail(cid):
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    conn = db.get_conn()
    conv = conn.execute('SELECT * FROM ai_conversations WHERE id = ?', (cid,)).fetchone()
    if not conv:
        return jsonify({'error': 'Không tìm thấy cuộc hội thoại.'}), 404
    msgs = conn.execute(
        'SELECT role, content, has_image, ok, error_msg, latency_ms, ts FROM ai_messages '
        'WHERE conversation_id = ? ORDER BY ts ASC', (cid,)
    ).fetchall()
    return jsonify({'conversation': dict(conv), 'messages': [dict(m) for m in msgs]})


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
}


def _events_snapshot():
    """Trả về toàn bộ sự kiện (đã phẳng hoá) từ SQLite, dùng cho các hàm
    tổng hợp (xếp hạng, hoạt động, hồ sơ...). Trước đây dùng biến in-memory
    `_events_mem`/`_event_lock` nhưng 2 biến này chưa từng được khởi tạo ở
    đâu trong file, gây NameError (lỗi 500) mỗi khi endpoint liên quan được
    gọi — nay đọc thẳng từ database.py (nguồn dữ liệu thật, đã ghi bởi
    log_event()/db.record_event())."""
    return db.all_events()


def _uid_of(ev):
    return ev.get('user_id') or ev.get('userId') or ''


# ── /api/admin-online-uids ────────────────────────────────────────────────
@app.route('/api/admin-online-uids', methods=['GET'])
def admin_online_uids():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    now = time.time()
    window = float(request.args.get('window', 300))  # 5 phút
    events = _events_snapshot()
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
    events = _events_snapshot()

    # index events theo uid để tránh O(N*M)
    per_uid = defaultdict(list)
    for ev in events:
        uid = _uid_of(ev)
        if uid:
            per_uid[uid].append(ev)

    result = {}
    for uid in uids:
        subset = per_uid.get(uid, [])
        # tận dụng lại _activity_for_uid nhưng truyền subset đã filter sẵn
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

    events = _events_snapshot()
    user_evs = [e for e in events if _uid_of(e) == uid]
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
    conn = db.get_conn()
    now = time.time()
    day_sec = 86400

    def _distinct_users(sql, params=()):
        rows = conn.execute(sql, params).fetchall()
        return {r[0] for r in rows if r[0]}

    # ── KPI tổng quan ────────────────────────────────────────────────────
    total_students = len(_distinct_users("SELECT DISTINCT user_id FROM events WHERE user_id != ''"))
    active_7d = len(_distinct_users(
        "SELECT DISTINCT user_id FROM events WHERE user_id != '' AND ts >= ?", (now - 7 * day_sec,)
    ))
    active_30d = len(_distinct_users(
        "SELECT DISTINCT user_id FROM events WHERE user_id != '' AND ts >= ?", (now - 30 * day_sec,)
    ))
    lessons_completed = db.count_events('lesson_complete')
    quiz_completed = conn.execute(
        'SELECT COUNT(*) c FROM quiz_attempts WHERE finished_at IS NOT NULL'
    ).fetchone()['c']
    lab_opened = db.count_events('lab_open')
    lab_completed = db.count_events('lab_complete')

    # ── Funnel theo user thật (không phải % giả định) ───────────────────
    users_logged_in = _distinct_users("SELECT DISTINCT user_id FROM events WHERE user_id != ''")
    users_lesson = _distinct_users(
        "SELECT DISTINCT user_id FROM events WHERE user_id != '' AND type IN ('lesson_start','lesson_open','lesson_complete')"
    )
    users_quiz = _distinct_users("SELECT DISTINCT user_id FROM quiz_attempts WHERE user_id != ''")
    users_lab = _distinct_users("SELECT DISTINCT user_id FROM lab_sessions WHERE user_id != ''")
    users_done = _distinct_users(
        "SELECT DISTINCT user_id FROM events WHERE user_id != '' AND type = 'lab_complete'"
    )
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
    first_seen_rows = conn.execute(
        "SELECT user_id, MIN(ts) first_ts FROM events WHERE user_id != '' GROUP BY user_id"
    ).fetchall()
    users_before_window = 0
    new_by_day = [0] * growth_days
    window_start = now - growth_days * day_sec
    for r in first_seen_rows:
        if r['first_ts'] < window_start:
            users_before_window += 1
            continue
        age = now - r['first_ts']
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
    session_rows = conn.execute(
        "SELECT user_id, ts FROM events WHERE user_id != '' AND type != 'heartbeat' AND ts >= ? "
        "ORDER BY user_id, ts", (now - 60 * day_sec,)
    ).fetchall()
    GAP = 30 * 60
    durations_min = []
    cur_uid, seg_start, prev_ts = None, None, None
    for r in session_rows:
        if r['user_id'] != cur_uid:
            if cur_uid is not None and prev_ts is not None and seg_start is not None:
                durations_min.append((prev_ts - seg_start) / 60.0)
            cur_uid, seg_start, prev_ts = r['user_id'], r['ts'], r['ts']
            continue
        if r['ts'] - prev_ts > GAP:
            durations_min.append((prev_ts - seg_start) / 60.0)
            seg_start = r['ts']
        prev_ts = r['ts']
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
    heat_rows = conn.execute(
        "SELECT ts FROM events WHERE ts >= ? AND type != 'heartbeat'", (now - 28 * day_sec,)
    ).fetchall()
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

    events = _events_snapshot()
    if cutoff:
        events = [e for e in events if e['ts'] >= cutoff]

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

    events = [e for e in _events_snapshot() if e.get('type') == 'bug_report']
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
