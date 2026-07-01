import os
import time
import json
import requests
from collections import defaultdict
from threading import Lock
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
RATE_WINDOW   = 60    # seconds
RATE_MAX      = 20    # requests per window per IP
COOLDOWN      = 3     # seconds between requests per IP

ip_requests: dict[str, list[float]] = defaultdict(list)
ip_last_req:  dict[str, float]       = {}
rate_lock = Lock()

@app.route('/admin')
def serve_admin():
    return send_from_directory('.', 'admin.html')

def check_rate_limit(ip: str) -> str | None:
    """Return an error message string if rate-limited, else None."""
    now = time.time()
    with rate_lock:
        # Cooldown check
        last = ip_last_req.get(ip, 0)
        if now - last < COOLDOWN:
            return 'Bạn đang gửi yêu cầu quá nhanh. Vui lòng chờ vài giây.'

        # Window check
        window_start = now - RATE_WINDOW
        ip_requests[ip] = [t for t in ip_requests[ip] if t > window_start]
        if len(ip_requests[ip]) >= RATE_MAX:
            return 'Bạn đã gửi quá nhiều yêu cầu. Vui lòng thử lại sau ít phút.'

        ip_requests[ip].append(now)
        ip_last_req[ip] = now
    return None


# ── Helper: single Gemini call ────────────────────────────────────────────────
def _call_gemini_once(contents, system_prompt: str, max_tokens: int = 65536):
    payload = {
        'contents': contents,
        'systemInstruction': {'parts': [{'text': system_prompt}]},
        'generationConfig': {
            'temperature': 0.3,
            'maxOutputTokens': max_tokens,
        },
    }
    resp = requests.post(
        GEMINI_URL,
        params={'key': GEMINI_API_KEY},
        json=payload,
        timeout=120,
    )

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

    candidate     = candidates[0]
    text          = ''.join(p.get('text', '') for p in candidate.get('content', {}).get('parts', []))
    finish_reason = candidate.get('finishReason', '')
    return text, finish_reason


# ── /api/chat ─────────────────────────────────────────────────────────────────
@app.route('/api/chat', methods=['POST'])
def chat():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

    rate_err = check_rate_limit(ip)
    if rate_err:
        return jsonify({'error': rate_err}), 429

    body = request.get_json(silent=True) or {}
    messages       = body.get('messages', [])
    system_prompt  = body.get('systemPrompt', '')

    if not messages:
        return jsonify({'error': 'Thiếu nội dung tin nhắn.'}), 400

    if not GEMINI_API_KEY:
        return jsonify({'error': 'Server chưa cấu hình API key.'}), 500

    try:
        last_msg = messages[-1]
        user_content = last_msg.get('content', '')
        parts = []
        if isinstance(user_content, str):
            parts.append({'text': user_content})
        elif isinstance(user_content, list):
            for item in user_content:
                if item.get('type') == 'text':
                    parts.append({'text': item['text']})
                elif item.get('type') == 'image_url':
                    data_url = item.get('image_url', {}).get('url', '')
                    if data_url.startswith('data:'):
                        import re
                        m = re.match(r'data:([^;]+);base64,', data_url)
                        mime_type = m.group(1) if m else 'image/jpeg'
                        b64 = data_url.split(',', 1)[1]
                        parts.append({'inlineData': {'mimeType': mime_type, 'data': b64}})

        contents = [{'role': 'user', 'parts': parts}]

        # First call
        full_text, finish_reason = _call_gemini_once(contents, system_prompt)

        # Auto-continue if MAX_TOKENS (up to 3 extra rounds)
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

        return jsonify({'text': full_text})

    except ValueError as e:
        msg = str(e)
        if msg == 'RATE_LIMIT':
            return jsonify({'error': 'Hệ thống AI đang bận. Vui lòng thử lại sau vài giây.'}), 429
        return jsonify({'error': msg}), 500
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Yêu cầu mất quá nhiều thời gian. Vui lòng thử lại.'}), 504
    except Exception as e:
        app.logger.error('Chat error: %s', e)
        return jsonify({'error': 'Đã xảy ra lỗi. Vui lòng thử lại.'}), 500


# ── /api/firebase-config ──────────────────────────────────────────────────────
@app.route('/api/firebase-config', methods=['GET'])
def firebase_config():
    return jsonify(FIREBASE_CONFIG)


# ── Static file serving ───────────────────────────────────────────────────────
# Serve index.html at root
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

# Serve lab.html explicitly (supports both /lab and /lab.html)
@app.route('/lab')
@app.route('/lab.html')
@app.route('/lab__28_.html')
def serve_lab():
    return send_from_directory('.', 'lab.html')

# Catch-all for other static assets (js, css, images, etc.)
@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    print(f'🚀 ChemCraft backend đang chạy tại http://localhost:{port}')
    print(f'   → index.html : http://localhost:{port}/')
    print(f'   → lab.html   : http://localhost:{port}/lab')
    print(f'   → API chat   : http://localhost:{port}/api/chat')
    app.run(host='0.0.0.0', port=port, debug=debug)
