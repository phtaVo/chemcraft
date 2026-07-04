"""
database.py — Lớp lưu trữ SQLite cho ChemCraft backend.

Module này được `server.py` import dưới tên `db` (import database as db) và
cung cấp toàn bộ các hàm mà server.py gọi tới:

    db.init_db()
    db.migrate_jsonl_events(path) -> int
    db.tx()                        -> context manager (transaction, ghi)
    db.get_conn()                  -> sqlite3.Connection (đọc trực tiếp)
    db.record_event(type, payload, user_id='', ip='')
    db.count_events(type) -> int
    db.bucket_by_day(type, days) -> [int, ...]
    db.query_events(type, since, limit) -> [dict, ...]
    db.all_events() -> [dict, ...]   (dùng cho các hàm tổng hợp/xếp hạng)
    db.DB_PATH
"""

import os
import json
import time
import sqlite3
from threading import Lock
from contextlib import contextmanager

DB_PATH = os.getenv('DB_PATH', 'chemcraft.db')
_lock = Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def init_db():
    """Tạo toàn bộ bảng nếu chưa tồn tại. An toàn khi gọi nhiều lần."""
    with _lock:
        conn = _connect()
        try:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    type     TEXT NOT NULL,
                    payload  TEXT NOT NULL DEFAULT '{}',
                    user_id  TEXT DEFAULT '',
                    ip       TEXT DEFAULT '',
                    ts       REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
                CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);

                CREATE TABLE IF NOT EXISTS ai_conversations (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT DEFAULT '',
                    ip         TEXT DEFAULT '',
                    started_at REAL,
                    topic      TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS ai_messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER,
                    role            TEXT,
                    content         TEXT DEFAULT '',
                    has_image       INTEGER DEFAULT 0,
                    ok              INTEGER DEFAULT 1,
                    error_msg       TEXT,
                    latency_ms      INTEGER,
                    ts              REAL
                );

                CREATE TABLE IF NOT EXISTS quiz_attempts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      TEXT DEFAULT '',
                    started_at   REAL,
                    total_q      INTEGER,
                    finished_at  REAL,
                    correct_q    INTEGER,
                    duration_sec REAL
                );

                CREATE TABLE IF NOT EXISTS quiz_answers (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id    INTEGER,
                    question_id   TEXT,
                    question_text TEXT,
                    is_correct    INTEGER,
                    retry_count   INTEGER DEFAULT 0,
                    answered_at   REAL
                );

                CREATE TABLE IF NOT EXISTS lab_sessions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      TEXT DEFAULT '',
                    started_at   REAL,
                    ended_at     REAL,
                    duration_sec REAL
                );

                CREATE TABLE IF NOT EXISTS lab_reaction_runs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id   INTEGER,
                    user_id      TEXT DEFAULT '',
                    reaction_eq  TEXT,
                    chemicals    TEXT,
                    equipment    TEXT,
                    outcome      TEXT,
                    error_reason TEXT,
                    duration_sec REAL,
                    ts           REAL
                );
            ''')
            conn.commit()
        finally:
            conn.close()


@contextmanager
def tx():
    """Context manager cho 1 transaction ghi. Tự commit/rollback."""
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_conn():
    """Trả về 1 connection mới để đọc (SELECT). Không cần đóng thủ công —
    connection sẽ được giải phóng khi hết tham chiếu, nhưng có thể đóng bằng
    conn.close() nếu muốn chủ động giải phóng sớm."""
    return _connect()


def record_event(ev_type, payload=None, user_id='', ip=''):
    """Ghi 1 sự kiện vào bảng events."""
    payload = payload or {}
    ts = time.time()
    with tx() as conn:
        conn.execute(
            'INSERT INTO events (type, payload, user_id, ip, ts) VALUES (?, ?, ?, ?, ?)',
            (ev_type, json.dumps(payload, ensure_ascii=False), user_id or '', ip or '', ts)
        )


def count_events(ev_type):
    conn = get_conn()
    try:
        row = conn.execute('SELECT COUNT(*) c FROM events WHERE type = ?', (ev_type,)).fetchone()
        return row['c'] if row else 0
    finally:
        conn.close()


def bucket_by_day(ev_type, days=7):
    """Số lượng event `ev_type` theo từng ngày trong `days` ngày gần nhất
    (phần tử đầu = xa nhất, phần tử cuối = hôm nay)."""
    now = time.time()
    day_sec = 86400
    buckets = [0] * days
    conn = get_conn()
    try:
        rows = conn.execute(
            'SELECT ts FROM events WHERE type = ? AND ts >= ?',
            (ev_type, now - days * day_sec)
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        age = now - r['ts']
        idx = days - 1 - int(age // day_sec)
        if 0 <= idx < days:
            buckets[idx] += 1
    return buckets


def _row_to_event(row):
    """Chuyển 1 row bảng events thành dict phẳng: {ts, type, user_id, ip, **payload}."""
    d = {'ts': row['ts'], 'type': row['type'], 'user_id': row['user_id'], 'ip': row['ip']}
    try:
        payload = json.loads(row['payload'] or '{}')
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k not in d:
                d[k] = v
    return d


def query_events(ev_type='', since=0, limit=200):
    conn = get_conn()
    try:
        if ev_type:
            rows = conn.execute(
                'SELECT * FROM events WHERE type = ? AND ts >= ? ORDER BY ts DESC LIMIT ?',
                (ev_type, since, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?',
                (since, limit)
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_event(r) for r in rows]


def all_events():
    """Trả về TOÀN BỘ sự kiện (payload đã "phẳng hoá") — dùng cho các hàm
    tổng hợp cần quét toàn bộ event theo field tuỳ ý (xếp hạng, hoạt động
    người dùng, hồ sơ học sinh...). Thay thế cho _events_mem/_event_lock
    kiểu in-memory list vốn không tồn tại trước đây."""
    conn = get_conn()
    try:
        rows = conn.execute('SELECT * FROM events ORDER BY ts ASC').fetchall()
    finally:
        conn.close()
    return [_row_to_event(r) for r in rows]


def migrate_jsonl_events(path):
    """Di chuyển các sự kiện cũ lưu ở dạng JSONL (mỗi dòng 1 JSON object,
    kiểu {"type": ..., "ts": ..., "userId": ..., ...}) vào SQLite, nếu file
    đó tồn tại. An toàn (trả về 0, không lỗi) nếu file không tồn tại hoặc
    rỗng. Sau khi migrate xong, đổi tên file thành `<path>.migrated` để
    tránh chạy lại lần sau."""
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return 0
    if not lines:
        return 0

    migrated = 0
    with tx() as conn:
        for line in lines:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            ev_type = obj.pop('type', None) or obj.pop('event', None)
            if not ev_type:
                continue
            ts = obj.pop('ts', None) or obj.pop('timestamp', None) or time.time()
            user_id = obj.pop('userId', None) or obj.pop('user_id', '') or ''
            ip = obj.pop('ip', '') or ''
            try:
                ts = float(ts)
            except Exception:
                ts = time.time()
            conn.execute(
                'INSERT INTO events (type, payload, user_id, ip, ts) VALUES (?, ?, ?, ?, ?)',
                (ev_type, json.dumps(obj, ensure_ascii=False), user_id, ip, ts)
            )
            migrated += 1

    if migrated:
        try:
            os.rename(path, path + '.migrated')
        except Exception:
            pass
    return migrated
