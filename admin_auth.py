"""
ChemCraft Admin — Authentication
==================================
Replaces the hardcoded ADMIN_ACCOUNTS dict + in-memory _admin_sessions dict
in the old server.py with:
  - Hashed passwords (werkzeug PBKDF2 — no new dependency; Flask ships it)
  - Sessions persisted in SQLite (survive server restarts)
  - A `role` column so multi-admin/RBAC is a data change, not a schema change
  - A login audit trail (admin_login_audit)

Bootstrapping: on first run, if admin_accounts is empty, we create the
original 'admin' account from env vars (or the old default, with a loud
warning to change it) so existing deployments don't get locked out.
"""
import os
import secrets
import time

from werkzeug.security import generate_password_hash, check_password_hash

import database as db

SESSION_TTL = 8 * 3600  # 8 hours, same as before


def bootstrap_default_admin():
    conn = db.get_conn()
    count = conn.execute('SELECT COUNT(*) c FROM admin_accounts').fetchone()['c']
    if count > 0:
        return
    username = os.getenv('CHEMCRAFT_ADMIN_USER', 'admin')
    password = os.getenv('CHEMCRAFT_ADMIN_PASSWORD', 'admin@11235')
    name = os.getenv('CHEMCRAFT_ADMIN_NAME', 'Admin ChemCraft')
    with db.tx() as conn:
        conn.execute(
            'INSERT INTO admin_accounts (username, password_hash, name, role, active, created_at) '
            'VALUES (?, ?, ?, ?, 1, ?)',
            (username, generate_password_hash(password), name, 'super_admin', time.time())
        )
    print(
        f"⚠️  Bootstrapped default admin account '{username}'. "
        f"Set CHEMCRAFT_ADMIN_PASSWORD in the environment and change it — "
        f"this default is not safe for production."
    )


def login(username: str, password: str, ip: str) -> dict | None:
    username = (username or '').strip().lower()
    conn = db.get_conn()
    row = conn.execute(
        'SELECT * FROM admin_accounts WHERE lower(username) = ? AND active = 1', (username,)
    ).fetchone()

    ok = bool(row) and check_password_hash(row['password_hash'], password)
    with db.tx() as conn:
        conn.execute(
            'INSERT INTO admin_login_audit (username, ip, success, ts) VALUES (?, ?, ?, ?)',
            (username, ip, 1 if ok else 0, time.time())
        )
        if ok:
            conn.execute(
                'UPDATE admin_accounts SET last_login_at = ? WHERE id = ?',
                (time.time(), row['id'])
            )

    if not ok:
        return None

    token = secrets.token_hex(32)
    with db.tx() as conn:
        conn.execute(
            'INSERT INTO admin_sessions (token, admin_id, created_at, last_seen_at, ip) '
            'VALUES (?, ?, ?, ?, ?)',
            (token, row['id'], time.time(), time.time(), ip)
        )
    return {'token': token, 'username': row['username'], 'name': row['name'], 'role': row['role']}


def verify(token: str) -> dict | None:
    if not token:
        return None
    conn = db.get_conn()
    row = conn.execute(
        'SELECT s.token, s.created_at, a.username, a.name, a.role, a.id as admin_id '
        'FROM admin_sessions s JOIN admin_accounts a ON a.id = s.admin_id '
        'WHERE s.token = ? AND a.active = 1', (token,)
    ).fetchone()
    if not row:
        return None
    if time.time() - row['created_at'] > SESSION_TTL:
        with db.tx() as conn:
            conn.execute('DELETE FROM admin_sessions WHERE token = ?', (token,))
        return None
    with db.tx() as conn:
        conn.execute('UPDATE admin_sessions SET last_seen_at = ? WHERE token = ?', (time.time(), token))
    return dict(row)


def logout(token: str):
    with db.tx() as conn:
        conn.execute('DELETE FROM admin_sessions WHERE token = ?', (token,))


def create_admin(username: str, password: str, name: str, role: str = 'admin') -> bool:
    """For future multi-admin UI. Requires caller to already be authorized as super_admin."""
    try:
        with db.tx() as conn:
            conn.execute(
                'INSERT INTO admin_accounts (username, password_hash, name, role, active, created_at) '
                'VALUES (?, ?, ?, ?, 1, ?)',
                (username.strip().lower(), generate_password_hash(password), name, role, time.time())
            )
        return True
    except Exception:
        return False
