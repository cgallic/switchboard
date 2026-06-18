"""switchboard/blackboard.py — the SQLite run-log (audit trail).

Every Switchboard run and every stage transition is written here. The blackboard
makes the gated DAG *visible and queryable* — it is the table the demo's run-log
viewer polls so stages light up on screen, and it is the durable proof that the
agent completed the loop autonomously.

Two tables:
    runs         — one row per call -> record run (status, stage, autonomy, record)
    stage_log    — one row per stage transition (the DAG advancing)

The blackboard is intentionally dependency-free (stdlib sqlite3 only) so the
whole agent runs offline with no install beyond Python.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

SCHEMA_VERSION = 1

RUN_STATUSES = {"running", "blocked", "done", "failed", "killed"}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    caller      TEXT,
    transcript  TEXT,
    autonomy    TEXT NOT NULL DEFAULT 'gated',   -- 'gated' | 'auto' | 'dryrun'
    stage       TEXT NOT NULL DEFAULT 'intake',
    status      TEXT NOT NULL DEFAULT 'running',  -- running|blocked|done|failed|killed
    record_json TEXT,                             -- the extracted/filed job record
    gate_json   TEXT,                             -- the pending gate request, if blocked
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    stage       TEXT NOT NULL,
    gate        TEXT NOT NULL,                    -- 'safe' | 'irreversible' | terminal
    outcome     TEXT NOT NULL,                    -- advanced|blocked|done|failed|executed
    executed    INTEGER NOT NULL DEFAULT 0,       -- did it actually act on the world?
    note        TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status     ON runs(status);
CREATE INDEX IF NOT EXISTS idx_stage_log_run   ON stage_log(run_id);
CREATE INDEX IF NOT EXISTS idx_stage_log_time  ON stage_log(created_at);
"""


@dataclass
class Run:
    id: str
    caller: Optional[str]
    transcript: Optional[str]
    autonomy: str
    stage: str
    status: str
    record: dict
    gate: Optional[dict]
    error: Optional[str]
    created_at: float
    updated_at: float


def _now() -> float:
    return time.time()


def connect(db_path: Union[str, Path] = ":memory:") -> sqlite3.Connection:
    """Open the blackboard, creating tables on first use."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_CREATE_SQL)
    conn.commit()
    return conn


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:10]


def create_run(
    conn: sqlite3.Connection,
    *,
    caller: Optional[str],
    transcript: Optional[str],
    autonomy: str = "gated",
    run_id: Optional[str] = None,
) -> str:
    """Insert a new run at the first stage and return its id."""
    rid = run_id or new_run_id()
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO runs (id, caller, transcript, autonomy, stage, status,
                                 record_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'intake', 'running', '{}', ?, ?)""",
            (rid, caller, transcript, autonomy, now, now),
        )
    return rid


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[Run]:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return Run(
        id=row["id"],
        caller=row["caller"],
        transcript=row["transcript"],
        autonomy=row["autonomy"],
        stage=row["stage"],
        status=row["status"],
        record=json.loads(row["record_json"] or "{}"),
        gate=json.loads(row["gate_json"]) if row["gate_json"] else None,
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def update_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    stage: Optional[str] = None,
    status: Optional[str] = None,
    record: Optional[dict] = None,
    gate: Optional[dict] = None,
    clear_gate: bool = False,
    error: Optional[str] = None,
) -> None:
    """Patch a run row; only the provided fields change."""
    fields: list[str] = []
    params: list[Any] = []
    if stage is not None:
        fields.append("stage = ?"); params.append(stage)
    if status is not None:
        if status not in RUN_STATUSES:
            raise ValueError(f"bad status {status!r}")
        fields.append("status = ?"); params.append(status)
    if record is not None:
        fields.append("record_json = ?"); params.append(json.dumps(record))
    if clear_gate:
        fields.append("gate_json = NULL")
    elif gate is not None:
        fields.append("gate_json = ?"); params.append(json.dumps(gate))
    if error is not None:
        fields.append("error = ?"); params.append(error)
    if not fields:
        return
    fields.append("updated_at = ?"); params.append(_now())
    params.append(run_id)
    with conn:
        conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", params)


def log_stage(
    conn: sqlite3.Connection,
    run_id: str,
    stage: str,
    gate: str,
    outcome: str,
    *,
    executed: bool = False,
    note: str = "",
) -> None:
    """Append a stage transition to the visible DAG audit trail."""
    with conn:
        conn.execute(
            """INSERT INTO stage_log (run_id, stage, gate, outcome, executed, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, stage, gate, outcome, 1 if executed else 0, note, _now()),
        )


def stage_history(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """Return every logged stage transition for a run, oldest first."""
    rows = conn.execute(
        "SELECT * FROM stage_log WHERE run_id = ? ORDER BY id ASC", (run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def blocked_runs(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM runs WHERE status = 'blocked' ORDER BY created_at"
    ).fetchall()
    return [r["id"] for r in rows]
