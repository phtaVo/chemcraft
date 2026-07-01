import os
import time
import json
import secrets
import threading
from collections import defaultdict, Counter, deque
from datetime import datetime, timedelta
from threading import Lock

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='.')
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

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

# ── Rate limiting ─────────────────────────────────────────────────────────────
RATE_WINDOW = 60
RATE_MAX    = 20
COOLDOWN    = 3

ip_requests: dict[str, list[float]] = defaultdict(list)
ip_last_req:  dict[str, float]       = {}
rate_lock = Lock()

# ── Admin accounts ────────────────────────────────────────────────────────────
ADMIN_ACCOUNTS = {
    'admin': {
        'password': 'admin@11235',
        'name':     'Admin ChemCraft',
        'role':     'super_admin',
    },
}
_admin_sessions: dict[str, dict] = {}
_SESSION_TTL = 8 * 3600

# ── Metrics / Event log ───────────────────────────────────────────────────────
# Ghi sự kiện thật vào file JSONL (mỗi dòng 1 JSON) — bền vững sau restart.
EVENT_LOG_PATH = os.getenv('EVENT_LOG_PATH', 'events.jsonl')
EVENT_MEM_MAX  = 20000   # giữ tối đa 20k event gần nhất trong RAM để tổng hợp nhanh
_events_mem: deque = deque(maxlen=EVENT_MEM_MAX)
_event_lock = Lock()

# Metrics runtime (reset khi server restart, dùng cho health/live counters)
_metrics = {
    'chat_total':      0,
    'chat_ok':         0,
    'chat_errors':     0,
    'gemini_errors':   0,
    'rate_limit_hits': 0,
    'started_at':      time.time(),
    # request log ring buffer (dùng cho biểu đồ 7 ngày)
    'chat_log':        deque(maxlen=5000),   # (timestamp, ip, ok/err)
    'error_log':       deque(maxlen=500),    # (timestamp, code, msg)
    'active_ips':      {},                    # ip -> last_seen_ts
}
_metrics_lock = Lock()


def _load_events_from_disk():
    if not os.path.exists(EVENT_LOG_PATH):
        return
    try:
        with open(EVENT_LOG_PATH, 'r', encoding='utf-8') as f:
            # đọc tối đa EVENT_MEM_MAX dòng cuối
            lines = f.readlines()[-EVENT_MEM_MAX:]
            for line in lines:
                try:
                    _events_mem.append(json.loads(line))
                except Exception:
                    pass
    except Exception as e:
        app.logger.warning('Load events failed: %s', e)


_load_events_from_disk()


def _persist_event(ev: dict):
    try:
        with open(EVENT_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(ev, ensure_ascii=False) + '\n')
    except Exception as e:
        app.logger.warning('Persist event failed: %s', e)


def _record_event(ev_type: str, payload: dict, user_id: str = '', ip: str = ''):
    ev = {
        'ts':      time.time(),
        'type':    ev_type,
        'user_id': user_id or '',
        'ip':      ip or '',
        **(payload or {}),
    }
    with _event_lock:
        _events_mem.append(ev)
    _persist_event(ev)
    with _metrics_lock:
        if ip:
            _metrics['active_ips'][ip] = ev['ts']
    return ev


def _get_client_ip() -> str:
    return request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()


def _require_admin() -> dict | None:
    token = request.headers.get('X-Admin-Token', '')
    session = _admin_sessions.get(token)
    if not session or (time.time() - session['created_at'] > _SESSION_TTL):
        return None
    session['created_at'] = time.time()
    return session


def check_rate_limit(ip: str) -> str | None:
    now = time.time()
    with rate_lock:
        last = ip_last_req.get(ip, 0)
        if now - last < COOLDOWN:
            with _metrics_lock:
                _metrics['rate_limit_hits'] += 1
            return 'Bạn đang gửi yêu cầu quá nhanh. Vui lòng chờ vài giây.'
        window_start = now - RATE_WINDOW
        ip_requests[ip] = [t for t in ip_requests[ip] if t > window_start]
        if len(ip_requests[ip]) >= RATE_MAX:
            with _metrics_lock:
                _metrics['rate_limit_hits'] += 1
            return 'Bạn đã gửi quá nhiều yêu cầu. Vui lòng thử lại sau ít phút.'
        ip_requests[ip].append(now)
        ip_last_req[ip] = now
    return None


# ── Helper: Gemini call ───────────────────────────────────────────────────────
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


# ── /api/chat ─────────────────────────────────────────────────────────────────
@app.route('/api/chat', methods=['POST'])
def chat():
    ip = _get_client_ip()
    with _metrics_lock:
        _metrics['chat_total'] += 1
        _metrics['active_ips'][ip] = time.time()

    rate_err = check_rate_limit(ip)
    if rate_err:
        with _metrics_lock:
            _metrics['chat_errors'] += 1
            _metrics['chat_log'].append((time.time(), ip, 'rate_limit'))
        return jsonify({'error': rate_err}), 429

    body = request.get_json(silent=True) or {}
    messages      = body.get('messages', [])
    system_prompt = body.get('systemPrompt', '')
    user_id       = body.get('userId', '')
    topic         = body.get('topic', '')

    if not messages:
        return jsonify({'error': 'Thiếu nội dung tin nhắn.'}), 400
    if not GEMINI_API_KEY:
        return jsonify({'error': 'Server chưa cấu hình API key.'}), 500

    try:
        last_msg = messages[-1]
        user_content = last_msg.get('content', '')
        parts = []
        question_text = ''
        if isinstance(user_content, str):
            parts.append({'text': user_content})
            question_text = user_content
        elif isinstance(user_content, list):
            for item in user_content:
                if item.get('type') == 'text':
                    parts.append({'text': item['text']})
                    question_text += ' ' + item['text']
                elif item.get('type') == 'image_url':
                    data_url = item.get('image_url', {}).get('url', '')
                    if data_url.startswith('data:'):
                        import re
                        m = re.match(r'data:([^;]+);base64,', data_url)
                        mime_type = m.group(1) if m else 'image/jpeg'
                        b64 = data_url.split(',', 1)[1]
                        parts.append({'inlineData': {'mimeType': mime_type, 'data': b64}})

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

        with _metrics_lock:
            _metrics['chat_ok'] += 1
            _metrics['chat_log'].append((time.time(), ip, 'ok'))

        _record_event('ai_chat', {
            'question': question_text.strip()[:500],
            'topic':    topic,
            'ok':       True,
        }, user_id=user_id, ip=ip)

        return jsonify({'text': full_text})

    except ValueError as e:
        msg = str(e)
        with _metrics_lock:
            _metrics['chat_errors'] += 1
            _metrics['gemini_errors'] += 1
            _metrics['error_log'].append((time.time(), 'gemini', msg))
            _metrics['chat_log'].append((time.time(), ip, 'err'))
        if msg == 'RATE_LIMIT':
            return jsonify({'error': 'Hệ thống AI đang bận. Vui lòng thử lại sau vài giây.'}), 429
        return jsonify({'error': msg}), 500
    except requests.exceptions.Timeout:
        with _metrics_lock:
            _metrics['chat_errors'] += 1
            _metrics['error_log'].append((time.time(), 'timeout', 'gemini timeout'))
        return jsonify({'error': 'Yêu cầu mất quá nhiều thời gian. Vui lòng thử lại.'}), 504
    except Exception as e:
        app.logger.error('Chat error: %s', e)
        with _metrics_lock:
            _metrics['chat_errors'] += 1
            _metrics['error_log'].append((time.time(), 'server', str(e)))
        return jsonify({'error': 'Đã xảy ra lỗi. Vui lòng thử lại.'}), 500


# ── Admin login/verify/logout ────────────────────────────────────────────────
@app.route('/api/admin-login', methods=['POST'])
def admin_login():
    body     = request.get_json(silent=True) or {}
    username = body.get('username', '').strip().lower()
    password = body.get('password', '')
    account  = ADMIN_ACCOUNTS.get(username)
    if not account or account['password'] != password:
        time.sleep(0.5)
        return jsonify({'error': 'Sai tên đăng nhập hoặc mật khẩu.'}), 401
    token = secrets.token_hex(32)
    _admin_sessions[token] = {
        'username': username, 'name': account['name'],
        'role': account['role'], 'created_at': time.time(),
    }
    return jsonify({'token': token, 'name': account['name'], 'role': account['role']})


@app.route('/api/admin-verify', methods=['GET'])
def admin_verify():
    session = _require_admin()
    if not session:
        return jsonify({'error': 'Token không hợp lệ hoặc đã hết hạn.'}), 401
    return jsonify({'username': session['username'], 'name': session['name'], 'role': session['role']})


@app.route('/api/admin-logout', methods=['POST'])
def admin_logout():
    token = request.headers.get('X-Admin-Token', '')
    _admin_sessions.pop(token, None)
    return jsonify({'ok': True})


@app.route('/api/firebase-config', methods=['GET'])
def firebase_config():
    return jsonify(FIREBASE_CONFIG)


# ── /api/log-event : client ghi sự kiện thật ─────────────────────────────────
# Không yêu cầu admin token (vì mọi client gọi), nhưng có rate-limit theo IP.
ALLOWED_EVENT_TYPES = {
    'session_start', 'heartbeat',
    'lesson_open', 'lesson_complete',
    'quiz_start', 'quiz_complete',
    'lab_open', 'lab_step', 'lab_complete', 'lab_error',
    'page_view',
}

@app.route('/api/log-event', methods=['POST'])
def log_event():
    ip = _get_client_ip()
    body = request.get_json(silent=True) or {}
    ev_type = (body.get('type') or '').strip()
    if ev_type not in ALLOWED_EVENT_TYPES:
        return jsonify({'error': 'Loại sự kiện không hợp lệ.'}), 400
    # Chống spam log
    now = time.time()
    with _metrics_lock:
        last = _metrics.get('_last_log_' + ip, 0)
        if now - last < 0.2:   # tối đa 5 event/giây/ip
            return jsonify({'ok': False, 'error': 'too_fast'}), 429
        _metrics['_last_log_' + ip] = now
    payload = {k: v for k, v in body.items() if k not in ('type', 'userId')}
    _record_event(ev_type, payload, user_id=body.get('userId', ''), ip=ip)
    return jsonify({'ok': True})


# ── /api/admin-metrics : tổng hợp KPI cho dashboard ──────────────────────────
def _bucket_by_day(events, days: int, key_filter=None):
    """Trả về [count/ngày] cho `days` ngày gần nhất (cũ → mới)."""
    now = time.time()
    buckets = [0] * days
    day_sec = 86400
    for ev in events:
        if key_filter and not key_filter(ev):
            continue
        age = now - ev['ts']
        if age < 0 or age > days * day_sec:
            continue
        idx = days - 1 - int(age // day_sec)
        if 0 <= idx < days:
            buckets[idx] += 1
    return buckets


def _bucket_by_hour_dow(events, key_filter=None):
    """Ma trận 7 ngày x 24 giờ cho 4 tuần gần nhất."""
    now = time.time()
    matrix = [[0] * 24 for _ in range(7)]
    for ev in events:
        if key_filter and not key_filter(ev):
            continue
        if now - ev['ts'] > 28 * 86400:
            continue
        d = datetime.fromtimestamp(ev['ts'])
        matrix[d.weekday()][d.hour] += 1
    return matrix


@app.route('/api/admin-metrics', methods=['GET'])
def admin_metrics():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    now = time.time()
    with _event_lock:
        events = list(_events_mem)
    with _metrics_lock:
        active_ips = {ip: t for ip, t in _metrics['active_ips'].items() if now - t < 300}
        chat_total      = _metrics['chat_total']
        chat_ok         = _metrics['chat_ok']
        chat_errors     = _metrics['chat_errors']
        gemini_errors   = _metrics['gemini_errors']
        rate_limit_hits = _metrics['rate_limit_hits']
        started_at      = _metrics['started_at']
        recent_errors   = list(_metrics['error_log'])[-20:]

    ai_events     = [e for e in events if e['type'] == 'ai_chat']
    lesson_done   = [e for e in events if e['type'] == 'lesson_complete']
    quiz_done     = [e for e in events if e['type'] == 'quiz_complete']
    lab_open      = [e for e in events if e['type'] == 'lab_open']
    lab_done      = [e for e in events if e['type'] == 'lab_complete']

    # Growth 7 ngày (mọi hoạt động)
    daily_activity = _bucket_by_day(events, 7)
    daily_ai       = _bucket_by_day(ai_events, 7)
    daily_lab      = _bucket_by_day(lab_open, 7)
    daily_lesson   = _bucket_by_day(lesson_done, 7)

    # Bar chart theo tuần (4 tuần)
    weekly_labs = [0, 0, 0, 0]
    for e in lab_open:
        age_days = (now - e['ts']) / 86400
        if age_days < 28:
            wi = 3 - int(age_days // 7)
            if 0 <= wi < 4:
                weekly_labs[wi] += 1

    # Tỉ lệ hoàn thành lab
    lab_completion = round(100 * len(lab_done) / max(1, len(lab_open))) if lab_open else 0

    # Donut: phân bổ hoạt động
    donut = {
        'Bài học': len(lesson_done),
        'Quiz':    len(quiz_done),
        'Lab 3D':  len(lab_open),
        'AI':      len(ai_events),
    }

    # Top câu hỏi AI (top 10 theo tần suất câu ngắn)
    q_counter = Counter()
    for e in ai_events:
        q = (e.get('question') or '').strip()
        if len(q) > 8:
            q_counter[q[:120]] += 1
    top_questions = q_counter.most_common(10)

    # Word cloud từ câu hỏi AI
    stop = set('và của là ở có cho một các những này đó khi được với thì để làm như từ trong về hay hoặc thế nào tại sao gì bao nhiêu the a an of is are and or to in on for how why what which'.split())
    words = Counter()
    for e in ai_events:
        for w in (e.get('question') or '').split():
            w = w.strip('.,?!:;()[]"\'').lower()
            if len(w) >= 3 and w not in stop:
                words[w] += 1
    word_cloud = words.most_common(30)

    # Heatmap giờ x ngày (mọi hoạt động 4 tuần)
    heatmap = _bucket_by_hour_dow(events)

    return jsonify({
        'server': {
            'uptime_sec':      int(now - started_at),
            'active_ips':      len(active_ips),
            'chat_total':      chat_total,
            'chat_ok':         chat_ok,
            'chat_errors':     chat_errors,
            'gemini_errors':   gemini_errors,
            'rate_limit_hits': rate_limit_hits,
        },
        'counts': {
            'ai_chats':        len(ai_events),
            'lessons_done':    len(lesson_done),
            'quiz_done':       len(quiz_done),
            'lab_open':        len(lab_open),
            'lab_completed':   len(lab_done),
            'lab_completion':  lab_completion,
            'events_total':    len(events),
        },
        'daily': {
            'activity_7d': daily_activity,
            'ai_7d':       daily_ai,
            'lab_7d':      daily_lab,
            'lesson_7d':   daily_lesson,
        },
        'weekly_labs':   weekly_labs,
        'donut':         donut,
        'top_questions': top_questions,
        'word_cloud':    word_cloud,
        'heatmap':       heatmap,
        'recent_errors': [
            {'ts': t, 'code': c, 'msg': m} for (t, c, m) in recent_errors
        ],
    })


# ── /api/admin-events : truy vấn event log thô ───────────────────────────────
@app.route('/api/admin-events', methods=['GET'])
def admin_events():
    if not _require_admin():
        return jsonify({'error': 'unauthorized'}), 401
    ev_type = request.args.get('type', '')
    limit   = min(int(request.args.get('limit', 200)), 2000)
    since   = float(request.args.get('since', 0))
    with _event_lock:
        events = list(_events_mem)
    if ev_type:
        events = [e for e in events if e['type'] == ev_type]
    if since:
        events = [e for e in events if e['ts'] >= since]
    events = events[-limit:]
    return jsonify({'events': events, 'total': len(events)})


# ── Static file serving ───────────────────────────────────────────────────────
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/admin')
@app.route('/admin.html')
def serve_admin():
    return send_from_directory('.', 'admin.html')

@app.route('/lab')
@app.route('/lab.html')
@app.route('/lab__28_.html')
def serve_lab():
    return send_from_directory('.', 'lab.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    print(f'🚀 ChemCraft backend đang chạy tại http://localhost:{port}')
    print(f'   → Event log: {EVENT_LOG_PATH}')
    app.run(host='0.0.0.0', port=port, debug=debug)
