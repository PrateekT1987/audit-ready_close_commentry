"""SQLite persistence for close runs, the source dataset and the sign-off chain."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from app.engine.audit import signoff_sequence

DB_PATH = os.environ.get("FINCOM_DB", "data/close.db")
DATASET_PATH = os.environ.get("FINCOM_DATASET", "data/dataset.json")

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
CREATE INDEX IF NOT EXISTS idx_signoffs_run ON signoffs(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""


@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA)
        yield connection
        connection.commit()
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def load_dataset() -> dict:
    if not os.path.exists(DATASET_PATH):
        from app.seed import write_dataset

        write_dataset(DATASET_PATH)
    with open(DATASET_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)
