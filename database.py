"""
ChemCraft Admin — Database layer
==================================
Replaces the old events.jsonl + in-memory deque with a real SQLite database.

Why SQLite (not Postgres/Mongo) for this iteration:
  - Zero extra infra to run/deploy — matches the current single-process Flask app.
  - Real indexes + SQL aggregation instead of scanning a 20k-item Python deque
    on every /api/admin-metrics call.
  - Trivial migration path to Postgres later (swap the connection factory;
    the schema below avoids SQLite-only syntax where practical).
  - WAL mode gives safe concurrent reads while the event-writer thread appends.

This module owns:
  - Schema creation / migration (idempotent, safe to call on every boot)
  - Admin account + session storage (bcrypt-hashed passwords)
  - Event ingestion (replaces _record_event / _persist_event in server.py)
  - Query helpers used by /api/admin-metrics, /api/admin-events, and the
    new analytics endpoints (lesson/quiz/lab/AI)

NOTE on migration from events.jsonl:
  Call migrate_jsonl_events(path) once at startup. It is idempotent — it
  tracks the last migrated line count in a `_migration_state` table so
  re-running the server doesn't duplicate old events.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

DB_PATH = os.getenv('CHEMCRAFT_DB_PATH', 'chemcraft.db')

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """One connection per thread (Flask's dev server / gunicorn workers are
    threaded); sqlite3 connections are not safe to share across threads."""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        _local.conn = conn
    return conn


@contextmanager
def tx():
    """Simple transaction context manager."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ─────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────
SCHEMA = """
-- ── Admin accounts & sessions ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',   -- super_admin | admin | viewer
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL,
    last_login_at REAL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token         TEXT PRIMARY KEY,
    admin_id      INTEGER NOT NULL REFERENCES admin_accounts(id),
    created_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    ip            TEXT
);

CREATE TABLE IF NOT EXISTS admin_login_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    ip          TEXT,
    success     INTEGER NOT NULL,
    ts          REAL NOT NULL
);

-- ── Users (mirrors Firebase Auth uid; adds admin-controlled fields) ────
-- Firebase remains the source of truth for auth + profile; this table is
-- what the Admin panel actually reads/writes so it stops depending on
-- direct client-side Firestore queries from the browser.
CREATE TABLE IF NOT EXISTS users (
    uid           TEXT PRIMARY KEY,
    email         TEXT,
    display_name  TEXT,
    locked        INTEGER NOT NULL DEFAULT 0,
    admin_notes   TEXT,
    first_seen_at REAL,
    last_seen_at  REAL,
    created_at    REAL NOT NULL
);

-- ── Generic event stream (replaces events.jsonl) ───────────────────────
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    type        TEXT NOT NULL,
    user_id     TEXT,
    ip          TEXT,
    payload     TEXT NOT NULL DEFAULT '{}'   -- JSON blob of type-specific fields
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts);

-- ── Lessons & progress ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lesson_progress (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    lesson_id    INTEGER NOT NULL,
    lesson_name  TEXT,
    completed    INTEGER NOT NULL DEFAULT 0,
    score        INTEGER,
    started_at   REAL,
    completed_at REAL,
    duration_sec INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lessonprog_user ON lesson_progress(user_id);
CREATE INDEX IF NOT EXISTS idx_lessonprog_lesson ON lesson_progress(lesson_id);

-- ── Quiz analytics (does not exist at all today) ────────────────────────
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    total_q       INTEGER,
    correct_q     INTEGER,
    duration_sec  INTEGER
);
CREATE TABLE IF NOT EXISTS quiz_answers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id    INTEGER NOT NULL REFERENCES quiz_attempts(id),
    question_id   TEXT NOT NULL,
    question_text TEXT,
    is_correct    INTEGER NOT NULL,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    answered_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quizans_attempt ON quiz_answers(attempt_id);
CREATE INDEX IF NOT EXISTS idx_quizans_question ON quiz_answers(question_id);

-- ── Lab analytics (does not exist at all today) ─────────────────────────
CREATE TABLE IF NOT EXISTS lab_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    duration_sec INTEGER
);
CREATE TABLE IF NOT EXISTS lab_reaction_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES lab_sessions(id),
    user_id       TEXT,
    reaction_eq   TEXT NOT NULL,     -- the reaction equation string (FK to reactions.eq once migrated)
    chemicals     TEXT,              -- JSON array of chemical ids used
    equipment     TEXT,              -- JSON array of equipment used
    outcome       TEXT NOT NULL,     -- 'success' | 'failure' | 'error'
    error_reason  TEXT,
    duration_sec  INTEGER,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labrun_reaction ON lab_reaction_runs(reaction_eq);
CREATE INDEX IF NOT EXISTS idx_labrun_outcome ON lab_reaction_runs(outcome);

-- ── AI Assistant analytics ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    ip          TEXT,
    started_at  REAL NOT NULL,
    topic       TEXT
);
CREATE TABLE IF NOT EXISTS ai_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES ai_conversations(id),
    role            TEXT NOT NULL,     -- 'user' | 'model'
    content         TEXT,
    has_image       INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER,
    ok              INTEGER NOT NULL DEFAULT 1,
    error_msg       TEXT,
    ts              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aimsg_conv ON ai_messages(conversation_id);

-- ── Reactions / Chemicals / Equipment (Phase 4b target — not yet wired) ─
CREATE TABLE IF NOT EXISTS reactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    eq          TEXT NOT NULL,
    grp         TEXT,
    type        TEXT,
    conditions  TEXT,
    tools       TEXT,
    steps       TEXT,
    obs         TEXT,
    product     TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    body        TEXT,
    target      TEXT NOT NULL DEFAULT 'all',  -- 'all' | uid
    created_by  TEXT,
    created_at  REAL NOT NULL,
    read_by     TEXT NOT NULL DEFAULT '[]'    -- JSON array of uids that opened it
);

CREATE TABLE IF NOT EXISTS _migration_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db():
    with tx() as conn:
        conn.executescript(SCHEMA)


# ─────────────────────────────────────────────────────────────────────────
# Migration from the old events.jsonl file (idempotent)
# ─────────────────────────────────────────────────────────────────────────
def migrate_jsonl_events(path: str):
    if not os.path.exists(path):
        return 0
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM _migration_state WHERE key = 'jsonl_lines_migrated'"
    ).fetchone()
    already = int(row['value']) if row else 0

    inserted = 0
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    with tx() as conn:
        for line in lines[already:]:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            payload = {k: v for k, v in ev.items() if k not in ('ts', 'type', 'user_id', 'ip')}
            conn.execute(
                'INSERT INTO events (ts, type, user_id, ip, payload) VALUES (?, ?, ?, ?, ?)',
                (ev.get('ts', time.time()), ev.get('type', 'unknown'),
                 ev.get('user_id', ''), ev.get('ip', ''), json.dumps(payload, ensure_ascii=False))
            )
            inserted += 1
        conn.execute(
            "INSERT INTO _migration_state (key, value) VALUES ('jsonl_lines_migrated', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(len(lines)),)
        )
    return inserted


# ─────────────────────────────────────────────────────────────────────────
# Event ingestion (replaces _record_event)
# ─────────────────────────────────────────────────────────────────────────
def record_event(ev_type: str, payload: dict, user_id: str = '', ip: str = '') -> dict:
    ts = time.time()
    with tx() as conn:
        conn.execute(
            'INSERT INTO events (ts, type, user_id, ip, payload) VALUES (?, ?, ?, ?, ?)',
            (ts, ev_type, user_id or '', ip or '', json.dumps(payload or {}, ensure_ascii=False))
        )
        if user_id:
            conn.execute(
                'INSERT INTO users (uid, first_seen_at, last_seen_at, created_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(uid) DO UPDATE SET last_seen_at = excluded.last_seen_at',
                (user_id, ts, ts, ts)
            )
    return {'ts': ts, 'type': ev_type, 'user_id': user_id, 'ip': ip, **(payload or {})}


def query_events(ev_type: str = '', since: float = 0, limit: int = 200):
    conn = get_conn()
    q = 'SELECT * FROM events WHERE 1=1'
    args = []
    if ev_type:
        q += ' AND type = ?'
        args.append(ev_type)
    if since:
        q += ' AND ts >= ?'
        args.append(since)
    q += ' ORDER BY ts DESC LIMIT ?'
    args.append(min(limit, 2000))
    rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d.update(json.loads(d.pop('payload') or '{}'))
        except Exception:
            d.pop('payload', None)
        out.append(d)
    return out


def count_events(ev_type: str, since: float = 0) -> int:
    conn = get_conn()
    q = 'SELECT COUNT(*) c FROM events WHERE type = ?'
    args = [ev_type]
    if since:
        q += ' AND ts >= ?'
        args.append(since)
    return conn.execute(q, args).fetchone()['c']


def bucket_by_day(ev_type: str, days: int = 7):
    """Real SQL day-bucketing instead of scanning a Python deque."""
    conn = get_conn()
    now = time.time()
    start = now - days * 86400
    rows = conn.execute(
        "SELECT CAST((? - ts) / 86400 AS INTEGER) AS age_day, COUNT(*) c "
        "FROM events WHERE type = ? AND ts >= ? GROUP BY age_day",
        (now, ev_type, start)
    ).fetchall()
    buckets = [0] * days
    for r in rows:
        idx = days - 1 - r['age_day']
        if 0 <= idx < days:
            buckets[idx] = r['c']
    return buckets
