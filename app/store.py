"""SQLite persistence for close runs, the source dataset and the sign-off chain."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.engine.audit import signoff_sequence

DB_PATH = os.environ.get("FINCOM_DB", "data/close.db")
DATASET_PATH = os.environ.get("FINCOM_DATASET", "data/dataset.json")
_DATASET_STATUS_CACHE: tuple | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    period TEXT NOT NULL,
    mode TEXT NOT NULL,
    label TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signoffs (
    signoff_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    signed_at TEXT NOT NULL,
    signer TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    issuer TEXT NOT NULL,
    email TEXT,
    display_name TEXT,
    roles TEXT NOT NULL,
    auth_time TEXT,
    method TEXT NOT NULL,
    csrf_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT
);
CREATE TABLE IF NOT EXISTS dual_control_approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_subject TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_subject TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT,
    revoked_subject TEXT
);
CREATE INDEX IF NOT EXISTS idx_signoffs_run ON signoffs(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_sessions_subject ON sessions(subject);
CREATE INDEX IF NOT EXISTS idx_dual_control_run ON dual_control_approvals(run_id);
"""


class DatasetUnavailable(RuntimeError):
    """The close dataset is missing, stale or deliberately not seeded."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA)
        # A run may carry at most one signature. This is also enforced by the
        # sign-off lock in the API; the index protects the database if a future
        # caller bypasses it. Existing pilot databases may contain duplicates,
        # so a failed index creation must not break the application.
        try:
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_signoffs_run_unique ON signoffs(run_id)"
            )
        except sqlite3.IntegrityError:
            pass
        yield connection
        connection.commit()
    finally:
        connection.close()


def _now() -> str:
    return now_iso()


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:10]


def save_run(run_id: str, payload: dict, *, status: str, period: str, mode: str, label: str) -> None:
    now = _now()
    blob = json.dumps(payload)
    with connect() as connection:
        existing = connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if existing:
            connection.execute(
                "UPDATE runs SET updated_at = ?, status = ?, mode = ?, payload = ? WHERE id = ?",
                (now, status, mode, blob, run_id),
            )
        else:
            connection.execute(
                "INSERT INTO runs (id, created_at, updated_at, status, period, mode, label, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, now, now, status, period, mode, label, blob),
            )


def load_run(run_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": row["status"],
        "period": row["period"],
        "mode": row["mode"],
        "label": row["label"],
        "payload": json.loads(row["payload"]),
    }


def list_runs(limit: int = 25) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, created_at, updated_at, status, period, mode, label FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_signoff(record: dict) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO signoffs (signoff_id, run_id, signed_at, signer, record) VALUES (?, ?, ?, ?, ?)",
            (record["signoff_id"], record["run_id"], record["signed_at"], record["signer"], json.dumps(record)),
        )


def load_signoffs(run_id: str | None = None) -> list[dict]:
    with connect() as connection:
        if run_id:
            rows = connection.execute(
                "SELECT record FROM signoffs WHERE run_id = ?", (run_id,)
            ).fetchall()
        else:
            rows = connection.execute("SELECT record FROM signoffs").fetchall()
    records = [json.loads(row["record"]) for row in rows]
    # Chain order is the persisted sequence, never the second-resolution
    # timestamp. Sorting here keeps every caller (new signoffs, views, the
    # audit endpoint) on the same order.
    records.sort(key=lambda r: (signoff_sequence(r), str(r.get("signoff_id", ""))))
    return records


def next_sequence() -> int:
    with connect() as connection:
        rows = connection.execute("SELECT record FROM signoffs").fetchall()
    return max((signoff_sequence(json.loads(row["record"])) for row in rows), default=0) + 1


def log_event(run_id: str, actor: str, action: str, detail: dict | None = None) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO events (run_id, at, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
            (run_id, _now(), actor, action, json.dumps(detail or {})),
        )


def load_events(run_id: str) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT at, actor, action, detail FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    return [{"at": r["at"], "actor": r["actor"], "action": r["action"], "detail": json.loads(r["detail"])} for r in rows]


def run_governance(run_id: str) -> dict:
    """The preparer and identity policy recorded when the run was opened.

    The `run.created` event is the system of record: its actor is the preparer
    of record, and its detail carries the immutable subject that created it.
    Reading governance from the append-only event stream means it cannot be
    changed by the signer.
    """
    with connect() as connection:
        row = connection.execute(
            "SELECT actor, detail FROM events WHERE run_id = ? AND action = 'run.created' ORDER BY id LIMIT 1",
            (run_id,),
        ).fetchone()
    if row is None:
        return {
            "preparer": "",
            "preparer_subject": "",
            "preparer_issuer": "",
            "dual_control": False,
            "session_id": "",
        }
    detail = json.loads(row["detail"] or "{}")
    return {
        "preparer": row["actor"],
        "preparer_subject": str(detail.get("preparer_subject") or detail.get("actor_subject") or ""),
        "preparer_issuer": str(detail.get("preparer_issuer") or detail.get("actor_issuer") or ""),
        # Legacy runs recorded a caller-supplied boolean. New runs never do;
        # dual control is an admin-approved record read from its own table.
        "dual_control": bool(detail.get("dual_control", False)),
        "session_id": str(detail.get("session_id") or ""),
    }


# ------------------------------------------------------------------ sessions
def create_session(
    *,
    session_id: str,
    subject: str,
    issuer: str,
    email: str,
    display_name: str,
    roles: list[str],
    auth_time: str,
    method: str,
    csrf_hash: str,
    idle_seconds: int,
    absolute_seconds: int,
) -> dict:
    now = datetime.now(timezone.utc)
    idle_expires = now + timedelta(seconds=idle_seconds)
    expires = now + timedelta(seconds=absolute_seconds)
    created = _iso(now)
    with connect() as connection:
        connection.execute(
            "INSERT INTO sessions (id, subject, issuer, email, display_name, roles, auth_time, "
            "method, csrf_hash, created_at, last_seen_at, idle_expires_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, subject, issuer, email, display_name, json.dumps(roles),
                auth_time, method, csrf_hash, created, created,
                _iso(idle_expires), _iso(expires),
            ),
        )
    return {
        "id": session_id,
        "subject": subject,
        "issuer": issuer,
        "email": email,
        "display_name": display_name,
        "roles": roles,
        "auth_time": auth_time,
        "method": method,
        "csrf_hash": csrf_hash,
        "created_at": created,
        "last_seen_at": created,
        "idle_expires_at": _iso(idle_expires),
        "expires_at": _iso(expires),
    }


def _session_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "subject": row["subject"],
        "issuer": row["issuer"],
        "email": row["email"] or "",
        "display_name": row["display_name"] or row["subject"],
        "roles": json.loads(row["roles"] or "[]"),
        "auth_time": row["auth_time"] or "",
        "method": row["method"],
        "csrf_hash": row["csrf_hash"],
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "idle_expires_at": row["idle_expires_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
    }


def load_session(session_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    session = _session_row(row)
    if session["revoked_at"]:
        return None
    now = datetime.now(timezone.utc)
    for key in ("idle_expires_at", "expires_at"):
        expires = parse_ts(session[key])
        if expires is None or expires <= now:
            return None
    return session


def session_needs_touch(session: dict, interval_seconds: int = 60) -> bool:
    last_seen = parse_ts(session.get("last_seen_at"))
    if last_seen is None:
        return True
    return (datetime.now(timezone.utc) - last_seen).total_seconds() >= interval_seconds


def touch_session(session_id: str, idle_seconds: int) -> None:
    now = datetime.now(timezone.utc)
    # `expires_at` (absolute lifetime) is still enforced on load, so extending
    # the idle window here cannot make a session outlive its absolute cap.
    idle_expires = now + timedelta(seconds=idle_seconds)
    with connect() as connection:
        connection.execute(
            "UPDATE sessions SET last_seen_at = ?, idle_expires_at = ? WHERE id = ?",
            (_iso(now), _iso(idle_expires), session_id),
        )


def revoke_session(session_id: str, reason: str = "logout") -> None:
    with connect() as connection:
        connection.execute(
            "UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE id = ? AND revoked_at IS NULL",
            (_now(), reason[:200], session_id),
        )


def purge_expired_sessions() -> int:
    now = _now()
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM sessions WHERE expires_at <= ? OR idle_expires_at <= ? OR revoked_at IS NOT NULL",
            (now, now),
        )
        return cursor.rowcount


# ------------------------------------------------------- dual control records
def _approval_row(row: sqlite3.Row) -> dict:
    return {
        "approval_id": row["approval_id"],
        "run_id": row["run_id"],
        "requested_by": row["requested_by"],
        "requested_subject": row["requested_subject"],
        "approved_by": row["approved_by"],
        "approved_subject": row["approved_subject"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "revoked_by": row["revoked_by"],
        "revoked_subject": row["revoked_subject"],
    }


def create_dual_control_approval(
    *,
    run_id: str,
    requested_by: str,
    requested_subject: str,
    approved_by: str,
    approved_subject: str,
    reason: str,
    expires_at: str,
) -> dict:
    approval_id = "DC-" + uuid.uuid4().hex[:10]
    created = _now()
    with connect() as connection:
        connection.execute(
            "INSERT INTO dual_control_approvals (approval_id, run_id, requested_by, "
            "requested_subject, approved_by, approved_subject, reason, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval_id, run_id, requested_by, requested_subject,
                approved_by, approved_subject, reason, created, expires_at,
            ),
        )
    return {
        "approval_id": approval_id,
        "run_id": run_id,
        "requested_by": requested_by,
        "requested_subject": requested_subject,
        "approved_by": approved_by,
        "approved_subject": approved_subject,
        "reason": reason,
        "created_at": created,
        "expires_at": expires_at,
        "revoked_at": None,
        "revoked_by": None,
        "revoked_subject": None,
    }


def list_dual_control(run_id: str) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM dual_control_approvals WHERE run_id = ? ORDER BY created_at DESC",
            (run_id,),
        ).fetchall()
    return [_approval_row(row) for row in rows]


def active_dual_control(run_id: str) -> dict | None:
    now = datetime.now(timezone.utc)
    for approval in list_dual_control(run_id):
        if approval["revoked_at"]:
            continue
        expires = parse_ts(approval["expires_at"])
        if expires is not None and expires > now:
            return approval
    return None


def revoke_dual_control(approval_id: str, revoked_by: str, revoked_subject: str) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE dual_control_approvals SET revoked_at = ?, revoked_by = ?, revoked_subject = ? "
            "WHERE approval_id = ? AND revoked_at IS NULL",
            (_now(), revoked_by, revoked_subject, approval_id),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------- readiness
def check_database() -> tuple[bool, str]:
    try:
        with connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True, "connected"
    except Exception as exc:  # noqa: BLE001 - readiness reports the failure
        return False, f"{type(exc).__name__}: {exc}"


def dataset_status() -> dict:
    global _DATASET_STATUS_CACHE
    if not os.path.exists(DATASET_PATH):
        return {
            "present": False,
            "path": DATASET_PATH,
            "modified_at": None,
            "age_hours": None,
            "size_bytes": 0,
            "sha256": None,
        }
    stat = os.stat(DATASET_PATH)
    cache_key = (DATASET_PATH, stat.st_mtime_ns, stat.st_size)
    if _DATASET_STATUS_CACHE and _DATASET_STATUS_CACHE[0] == cache_key:
        cached = dict(_DATASET_STATUS_CACHE[1])
        modified = parse_ts(cached["modified_at"])
        if modified is not None:
            cached["age_hours"] = round(
                (datetime.now(timezone.utc) - modified).total_seconds() / 3600, 2
            )
        return cached
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    digest = hashlib.sha256()
    with open(DATASET_PATH, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    status = {
        "present": True,
        "path": DATASET_PATH,
        "modified_at": _iso(modified),
        "age_hours": round(
            (datetime.now(timezone.utc) - modified).total_seconds() / 3600, 2
        ),
        "size_bytes": stat.st_size,
        "sha256": digest.hexdigest(),
    }
    _DATASET_STATUS_CACHE = (cache_key, dict(status))
    return status


def load_dataset(allow_seed: bool = True) -> dict:
    if not os.path.exists(DATASET_PATH):
        if not allow_seed:
            raise DatasetUnavailable(
                "The close dataset is missing and synthetic seeding is disabled in this environment."
            )
        from app.seed import write_dataset

        write_dataset(DATASET_PATH)
    with open(DATASET_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)
