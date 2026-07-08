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
                    duration_sec  REAL,
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

                -- ── Bảng cho admin_auth.py ────────────────────────────
                CREATE TABLE IF NOT EXISTS admin_accounts (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    name          TEXT DEFAULT '',
                    role          TEXT DEFAULT 'admin',
                    active        INTEGER DEFAULT 1,
                    created_at    REAL,
                    last_login_at REAL
                );

                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token        TEXT PRIMARY KEY,
                    admin_id     INTEGER NOT NULL,
                    created_at   REAL,
                    last_seen_at REAL,
                    ip           TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS admin_login_audit (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    ip       TEXT DEFAULT '',
                    success  INTEGER DEFAULT 0,
                    ts       REAL
                );

                -- Trạng thái "đã xem thông báo" của từng admin (cho chuông 🔔)
                CREATE TABLE IF NOT EXISTS admin_notif_state (
                    admin_id      INTEGER PRIMARY KEY,
                    last_seen_ts  REAL DEFAULT 0
                );

                -- ── Ngân hàng câu hỏi Quiz (quiz_bank.py) ──────────────
                -- Thay thế mảng `quizData` hardcode trong lesson.html: admin
                -- có thể thêm/sửa/xóa câu hỏi trắc nghiệm, hệ thống random
                -- ra một bộ đề mỗi lần học sinh bấm "Bắt đầu thi".
                CREATE TABLE IF NOT EXISTS quiz_questions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    question     TEXT NOT NULL,
                    options      TEXT NOT NULL DEFAULT '[]',   -- JSON array các phương án
                    answer_index INTEGER NOT NULL DEFAULT 0,   -- vị trí đáp án đúng trong options
                    difficulty   TEXT DEFAULT 'TB',            -- 'Dễ' | 'TB' | 'Khó'
                    active       INTEGER DEFAULT 1,            -- 0 = ẩn khỏi random, không xóa hẳn
                    created_by   TEXT DEFAULT '',
                    created_at   REAL,
                    updated_at   REAL
                );
                CREATE INDEX IF NOT EXISTS idx_quiz_questions_active ON quiz_questions(active);

                -- ── Lab 3D: Ngân hàng hoá chất (mô hình 3D) & phản ứng (lab_bank.py) ──
                -- Thay thế LAB_MOLECULE_DATA (~13 phân tử có toạ độ atoms/bonds 3D) và
                -- REACTIONS (~233 phản ứng tĩnh) vốn nằm cứng trong lab.html. Admin có
                -- thể thêm/sửa/xoá hoá chất và phản ứng mới; mỗi bản ghi có status
                -- 'draft' (đang soạn, chưa hiện cho học sinh) hoặc 'published' (hiện
                -- đầy đủ trong lab.html, học sinh dùng được ngay).
                CREATE TABLE IF NOT EXISTS lab_molecules (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    chem_id       TEXT UNIQUE NOT NULL,
                    name          TEXT NOT NULL,
                    formula       TEXT NOT NULL,
                    mol_weight    REAL,
                    polar         INTEGER DEFAULT 0,
                    bond_angle    REAL,
                    bonds_desc    TEXT DEFAULT '',
                    desc          TEXT DEFAULT '',
                    atoms         TEXT NOT NULL DEFAULT '[]',
                    bonds         TEXT NOT NULL DEFAULT '[]',
                    status        TEXT DEFAULT 'draft',
                    created_by    TEXT DEFAULT '',
                    created_at    REAL,
                    updated_at    REAL
                );
                CREATE INDEX IF NOT EXISTS idx_lab_molecules_status ON lab_molecules(status);

                CREATE TABLE IF NOT EXISTS lab_reactions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    eq          TEXT NOT NULL,
                    type        TEXT DEFAULT '',
                    conditions  TEXT DEFAULT '',
                    tools       TEXT DEFAULT '',
                    steps       TEXT DEFAULT '',
                    obs         TEXT DEFAULT '',
                    product     TEXT DEFAULT '',
                    grp         TEXT DEFAULT '',
                    -- ── Dữ liệu có cấu trúc để engine 3D (evaluateReaction()) thực sự
                    -- kích hoạt animation, thay vì chỉ hiển thị dạng "thẻ nhiệm vụ" ──
                    participants        TEXT DEFAULT '[]',  -- JSON [{chemId, catalyst}, ...]
                    needs_heat          INTEGER DEFAULT 0,
                    before_color        TEXT DEFAULT '#ffffff',
                    before_phenomenon   TEXT DEFAULT '',     -- 'sủi' | 'khí' | 'kết tủa' | ''
                    during_color        TEXT DEFAULT '#ffffff',
                    during_phenomenon   TEXT DEFAULT '',
                    after_color         TEXT DEFAULT '#ffffff',
                    after_phenomenon    TEXT DEFAULT '',
                    status      TEXT DEFAULT 'draft',
                    created_by  TEXT DEFAULT '',
                    created_at  REAL,
                    updated_at  REAL
                );
                CREATE INDEX IF NOT EXISTS idx_lab_reactions_status ON lab_reactions(status);

                -- ── Lab 3D: Hóa chất trên KỆ (chemDB trong lab.html) ──────────────
                -- Danh sách hóa chất thật học sinh chọn để pha trộn trong phòng thí
                -- nghiệm 3D (~163 chất ban đầu, chia 3 kệ qua cột `cat`: 'don' = đơn
                -- chất, 'voco' = hợp chất vô cơ, 'huuco' = hợp chất hữu cơ). Khác với
                -- lab_molecules (mô hình 3D phân tử, tính năng riêng).
                CREATE TABLE IF NOT EXISTS lab_shelf_chemicals (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    chem_id        TEXT UNIQUE NOT NULL,
                    name           TEXT NOT NULL,
                    desc           TEXT DEFAULT '',
                    type           TEXT DEFAULT '',   -- axit | bazo | muoi | oxit | kimloai | huuco | chithi | khác
                    cat            TEXT DEFAULT 'voco', -- don | voco | huuco — quyết định kệ hiển thị
                    color          TEXT DEFAULT '#ffffff',
                    ph             REAL,
                    solid          INTEGER DEFAULT 0,
                    is_gas         INTEGER DEFAULT 0,
                    is_paper       INTEGER DEFAULT 0,
                    opacity        REAL,
                    allowed_states TEXT DEFAULT '[]',  -- JSON, VD ["solid","liquid"]
                    status         TEXT DEFAULT 'draft',
                    created_by     TEXT DEFAULT '',
                    created_at     REAL,
                    updated_at     REAL
                );
                CREATE INDEX IF NOT EXISTS idx_lab_shelf_chemicals_status ON lab_shelf_chemicals(status);
                CREATE INDEX IF NOT EXISTS idx_lab_shelf_chemicals_cat ON lab_shelf_chemicals(cat);
            ''')
            # Migration for DBs created before duration_sec existed on quiz_answers
            # (Quiz Analytics needs real per-question timing — see quiz_answer
            # fanout in server.py). SQLite has no "ADD COLUMN IF NOT EXISTS",
            # so check PRAGMA table_info first.
            cols = [r['name'] for r in conn.execute('PRAGMA table_info(quiz_answers)').fetchall()]
            if 'duration_sec' not in cols:
                conn.execute('ALTER TABLE quiz_answers ADD COLUMN duration_sec REAL')

            # Migration: lab_reactions may pre-date the structured participants/phase
            # columns (added for the Lab 3D reaction engine hookup).
            react_cols = [r['name'] for r in conn.execute('PRAGMA table_info(lab_reactions)').fetchall()]
            _new_react_cols = [
                ('participants', "TEXT DEFAULT '[]'"), ('needs_heat', 'INTEGER DEFAULT 0'),
                ('before_color', "TEXT DEFAULT '#ffffff'"), ('before_phenomenon', "TEXT DEFAULT ''"),
                ('during_color', "TEXT DEFAULT '#ffffff'"), ('during_phenomenon', "TEXT DEFAULT ''"),
                ('after_color', "TEXT DEFAULT '#ffffff'"), ('after_phenomenon', "TEXT DEFAULT ''"),
            ]
            for col_name, col_def in _new_react_cols:
                if col_name not in react_cols:
                    conn.execute(f'ALTER TABLE lab_reactions ADD COLUMN {col_name} {col_def}')
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
