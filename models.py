"""SQLite persistence (stdlib sqlite3, no ORM) + Fernet password encryption."""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from contextlib import contextmanager

from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.environ.get("HCEM_DB", "/data/hcem.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    host         TEXT NOT NULL,
    username     TEXT NOT NULL,
    password_enc TEXT NOT NULL,
    verify_tls   INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id    INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    vm_uuid       TEXT NOT NULL,
    vm_name       TEXT NOT NULL,
    path_uri_base TEXT NOT NULL,       -- credential-free, e.g. smb://nas.local/share/path
    smb_user      TEXT,                -- optional; SMB share username (NULL for NFS)
    smb_pass_enc  TEXT,                -- optional; Fernet-encrypted share password
    prune_path    TEXT,
    retention     INTEGER NOT NULL DEFAULT 7,
    run_time      TEXT NOT NULL DEFAULT '03:00',
    compress      INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (cluster_id, vm_uuid)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'RUNNING',  -- RUNNING | COMPLETE | ERROR
    task_tag    TEXT,
    export_path TEXT,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS auth (
    id            INTEGER PRIMARY KEY CHECK (id = 1),   -- single admin row
    username      TEXT NOT NULL DEFAULT 'admin',
    password_hash TEXT NOT NULL,
    must_change   INTEGER NOT NULL DEFAULT 1,           -- force rotation off the default
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS imports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id   INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    source_uri   TEXT NOT NULL,       -- credential-free, for display
    smb_user     TEXT,                -- optional; SMB share username (NULL for NFS)
    smb_pass_enc TEXT,                -- optional; Fernet-encrypted share password
    target_name  TEXT,                -- requested name on the target ('' = keep original)
    status       TEXT NOT NULL DEFAULT 'RUNNING',  -- RUNNING | COMPLETE | ERROR
    task_tag     TEXT,
    created_uuid TEXT,                -- new VM's UUID once the import completes
    message      TEXT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT
);
"""


# --------------------------------------------------------------------- crypto
def _fernet() -> Fernet:
    secret = os.environ.get("HCEM_SECRET")
    if not secret:
        raise RuntimeError(
            "HCEM_SECRET environment variable is not set. "
            "Set it to any long random string; it protects stored cluster passwords."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_password(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_password(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


# ------------------------------------------------------------------------- db
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    ensure_admin()


def _migrate(conn):
    """Add columns introduced after the first release to pre-existing DBs.

    CREATE TABLE IF NOT EXISTS won't alter an already-created table, so new
    columns need explicit ALTERs guarded by a check on the current schema.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(schedules)")}
    if "smb_user" not in cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN smb_user TEXT")
    if "smb_pass_enc" not in cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN smb_pass_enc TEXT")


# ----------------------------------------------------------------------- auth
# pbkdf2:sha256 is used explicitly rather than the Werkzeug default (scrypt),
# which needs an OpenSSL build not guaranteed on python:alpine.
_PW_METHOD = "pbkdf2:sha256"


def ensure_admin():
    """Seed the single admin account (admin/admin, must-change) on first run."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM auth WHERE id = 1").fetchone() is None:
            conn.execute(
                "INSERT INTO auth (id, username, password_hash, must_change) "
                "VALUES (1, 'admin', ?, 1)",
                (generate_password_hash("admin", method=_PW_METHOD),))


def get_auth():
    with db() as conn:
        return conn.execute("SELECT * FROM auth WHERE id = 1").fetchone()


def check_credentials(username, password) -> bool:
    row = get_auth()
    return bool(row) and username == row["username"] \
        and check_password_hash(row["password_hash"], password)


def set_admin_password(new_password):
    """Store a new password and clear the must-change flag."""
    with db() as conn:
        conn.execute(
            "UPDATE auth SET password_hash = ?, must_change = 0, "
            "updated_at = datetime('now') WHERE id = 1",
            (generate_password_hash(new_password, method=_PW_METHOD),))


# ------------------------------------------------------------------- clusters
def add_cluster(name, host, username, password, verify_tls):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO clusters (name, host, username, password_enc, verify_tls) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, host, username, encrypt_password(password), int(verify_tls)),
        )
        return cur.lastrowid


def get_clusters():
    with db() as conn:
        return conn.execute("SELECT * FROM clusters ORDER BY name").fetchall()


def get_cluster(cluster_id):
    with db() as conn:
        return conn.execute("SELECT * FROM clusters WHERE id = ?", (cluster_id,)).fetchone()


def delete_cluster(cluster_id):
    with db() as conn:
        conn.execute("DELETE FROM clusters WHERE id = ?", (cluster_id,))


# ------------------------------------------------------------------ schedules
def add_schedule(cluster_id, vm_uuid, vm_name, path_uri_base, prune_path,
                 retention, run_time, compress, smb_user=None, smb_password=None):
    smb_user = (smb_user or "").strip() or None
    smb_pass_enc = encrypt_password(smb_password) if smb_password else None
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO schedules (cluster_id, vm_uuid, vm_name, path_uri_base, "
            "smb_user, smb_pass_enc, prune_path, retention, run_time, compress) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cluster_id, vm_uuid, vm_name, path_uri_base.rstrip("/"),
             smb_user, smb_pass_enc,
             (prune_path or "").rstrip("/") or None, retention, run_time, int(compress)),
        )
        return cur.lastrowid


def get_schedules(cluster_id=None):
    q = ("SELECT s.*, c.name AS cluster_name, c.host AS cluster_host "
         "FROM schedules s JOIN clusters c ON c.id = s.cluster_id ")
    with db() as conn:
        if cluster_id:
            return conn.execute(q + "WHERE s.cluster_id = ? ORDER BY s.vm_name",
                                (cluster_id,)).fetchall()
        return conn.execute(q + "ORDER BY c.name, s.vm_name").fetchall()


def get_schedule(schedule_id):
    with db() as conn:
        return conn.execute(
            "SELECT s.*, c.name AS cluster_name, c.host AS cluster_host "
            "FROM schedules s JOIN clusters c ON c.id = s.cluster_id "
            "WHERE s.id = ?", (schedule_id,)).fetchone()


def get_known_smb_locations():
    """Distinct SMB export destinations (base + user) already saved on
    schedules, so imports can reuse them instead of re-entering credentials.

    ref_id points at a schedule holding the stored password for that location;
    thanks to SQLite's MAX() bare-column rule it's the newest matching row."""
    with db() as conn:
        return conn.execute(
            "SELECT path_uri_base, smb_user, MAX(id) AS ref_id "
            "FROM schedules "
            "WHERE path_uri_base LIKE 'smb://%' AND smb_user IS NOT NULL "
            "GROUP BY path_uri_base, smb_user "
            "ORDER BY path_uri_base").fetchall()


def toggle_schedule(schedule_id):
    with db() as conn:
        conn.execute("UPDATE schedules SET enabled = 1 - enabled WHERE id = ?",
                     (schedule_id,))


def delete_schedule(schedule_id):
    with db() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# ----------------------------------------------------------------------- runs
def start_run(schedule_id):
    with db() as conn:
        cur = conn.execute("INSERT INTO runs (schedule_id) VALUES (?)", (schedule_id,))
        return cur.lastrowid


def update_run(run_id, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))


def get_runs(schedule_id=None, limit=50):
    q = ("SELECT r.*, s.vm_name, c.name AS cluster_name "
         "FROM runs r JOIN schedules s ON s.id = r.schedule_id "
         "JOIN clusters c ON c.id = s.cluster_id ")
    with db() as conn:
        if schedule_id:
            return conn.execute(q + "WHERE r.schedule_id = ? ORDER BY r.id DESC LIMIT ?",
                                (schedule_id, limit)).fetchall()
        return conn.execute(q + "ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()


def get_run(run_id):
    with db() as conn:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def last_run_for(schedule_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE schedule_id = ? ORDER BY id DESC LIMIT 1",
            (schedule_id,)).fetchone()


def get_active_runs():
    """Runs still marked RUNNING -- used on startup to recover ones whose
    monitoring thread died when the process restarted."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE status = 'RUNNING' ORDER BY id").fetchall()


def get_active_imports():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM imports WHERE status = 'RUNNING' ORDER BY id").fetchall()


def is_run_active(schedule_id) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE schedule_id = ? AND status = 'RUNNING'",
            (schedule_id,)).fetchone()
        return row["n"] > 0


# --------------------------------------------------------------------- imports
def add_import(cluster_id, source_uri, smb_user, smb_password, target_name):
    smb_user = (smb_user or "").strip() or None
    smb_pass_enc = encrypt_password(smb_password) if smb_password else None
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO imports (cluster_id, source_uri, smb_user, smb_pass_enc, "
            "target_name) VALUES (?,?,?,?,?)",
            (cluster_id, source_uri.rstrip("/"), smb_user, smb_pass_enc,
             (target_name or "").strip() or None),
        )
        return cur.lastrowid


def get_import(import_id):
    with db() as conn:
        return conn.execute(
            "SELECT i.*, c.name AS cluster_name FROM imports i "
            "JOIN clusters c ON c.id = i.cluster_id WHERE i.id = ?",
            (import_id,)).fetchone()


def get_imports(limit=50):
    with db() as conn:
        return conn.execute(
            "SELECT i.*, c.name AS cluster_name FROM imports i "
            "JOIN clusters c ON c.id = i.cluster_id ORDER BY i.id DESC LIMIT ?",
            (limit,)).fetchall()


def update_import(import_id, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE imports SET {sets} WHERE id = ?",
                     (*fields.values(), import_id))
