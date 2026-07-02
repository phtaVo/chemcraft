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

load_dotenv()

app = Flask(__name__, static_folder='.')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

db.init_db()
admin_auth.bootstrap_default_admin()
_migrated = db.migrate_jsonl_events(os.getenv('EVENT_LOG_PATH', 'events.jsonl'))
if _migrated:
    print(f'📦 Migrated {_migrated} legacy events from events.jsonl into SQLite.')

# ── Config ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_URL = (
    'https://generativelanguage.googleapis.com/v1beta/models/'
    'gemini-2.5-flash:generateContent'
)

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
                    'retry_count, answered_at) VALUES (?, ?, ?, ?, ?, ?)',
                    (attempt_id, payload.get('questionId', ''), payload.get('questionText', ''),
                     int(bool(payload.get('correct'))), payload.get('retryCount', 0), ts)
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


# ── NEW: dedicated analytics endpoints (previously impossible — no data) ───
@app.route('/api/admin-analytics/quiz', methods=['GET'])
def analytics_quiz():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    conn = db.get_conn()
    total_attempts = conn.execute('SELECT COUNT(*) c FROM quiz_attempts').fetchone()['c']
    avg_score = conn.execute(
        'SELECT AVG(1.0 * correct_q / NULLIF(total_q, 0)) a FROM quiz_attempts WHERE finished_at IS NOT NULL'
    ).fetchone()['a']
    hardest_questions = conn.execute(
        "SELECT question_id, question_text, "
        "SUM(CASE WHEN is_correct=0 THEN 1 ELSE 0 END) wrong, COUNT(*) total "
        "FROM quiz_answers GROUP BY question_id ORDER BY (1.0*wrong/total) DESC LIMIT 15"
    ).fetchall()
    return jsonify({
        'total_attempts': total_attempts,
        'avg_score_pct': round((avg_score or 0) * 100, 1),
        'hardest_questions': [dict(r) for r in hardest_questions],
    })


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
    total_conversations = conn.execute('SELECT COUNT(*) c FROM ai_conversations').fetchone()['c']
    avg_latency = conn.execute(
        "SELECT AVG(latency_ms) a FROM ai_messages WHERE role='model' AND ok=1"
    ).fetchone()['a']
    error_rate = conn.execute(
        "SELECT 1.0 * SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) / COUNT(*) r FROM ai_messages WHERE role='model'"
    ).fetchone()['r']
    by_topic = conn.execute(
        "SELECT COALESCE(NULLIF(topic,''),'(chưa gắn chủ đề)') topic, COUNT(*) c "
        "FROM ai_conversations GROUP BY topic ORDER BY c DESC LIMIT 15"
    ).fetchall()
    return jsonify({
        'total_conversations': total_conversations,
        'avg_latency_ms': round(avg_latency or 0),
        'error_rate_pct': round((error_rate or 0) * 100, 2),
        'by_topic': [dict(r) for r in by_topic],
    })


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


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    print(f'🚀 ChemCraft backend đang chạy tại http://localhost:{port}')
    print(f'   → Database: {db.DB_PATH}')
    app.run(host='0.0.0.0', port=port, debug=debug)
