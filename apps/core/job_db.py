"""SQLite persistence for job history and audit events."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apps.core.job_models import JobRecord, JobStatus
from apps.core.paths import PROJECT_ROOT, ensure_data_dirs

JOBS_DB_PATH = PROJECT_ROOT / "rpa_jobs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}',
    params_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id);
CREATE INDEX IF NOT EXISTS idx_job_events_created_at ON job_events(created_at DESC);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class JobDatabase:
    """Thread-safe SQLite store for jobs and history events."""

    def __init__(self, db_path: Path | None = None) -> None:
        ensure_data_dirs()
        self._path = Path(db_path or JOBS_DB_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                conn.commit()

    def save_job(self, job: JobRecord, params: dict[str, Any] | None = None) -> None:
        progress = job.result.get("progress", {})
        params_json = json.dumps(params or {}, default=str)
        result_json = json.dumps(job.result, default=str)
        progress_json = json.dumps(progress, default=str)
        row = (
            job.job_id,
            job.job_type,
            job.status.value,
            job.message,
            _dt_to_iso(job.created_at),
            _dt_to_iso(job.started_at),
            _dt_to_iso(job.finished_at),
            result_json,
            progress_json,
            params_json,
        )
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, job_type, status, message,
                        created_at, started_at, finished_at,
                        result_json, progress_json, params_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        status = excluded.status,
                        message = excluded.message,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        result_json = excluded.result_json,
                        progress_json = excluded.progress_json,
                        params_json = excluded.params_json
                    """,
                    row,
                )
                conn.commit()

    def add_event(
        self,
        job_id: str,
        event_type: str,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO job_events (job_id, event_type, message, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        event_type,
                        message,
                        json.dumps(payload or {}, default=str),
                        _dt_to_iso(_utc_now()),
                    ),
                )
                conn.commit()

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
        if row is None:
            return None
        return _row_to_job_record(row)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        job_type: str | None = None,
        status: str | None = None,
    ) -> list[JobRecord]:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if job_type:
            query += " AND job_type = ?"
            params.append(job_type)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
        return [_row_to_job_record(row) for row in rows]

    def list_events(self, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM job_events
                    WHERE job_id = ?
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (job_id, limit),
                ).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status"
                ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    def count_by_type(self) -> dict[str, int]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT job_type, COUNT(*) AS total FROM jobs GROUP BY job_type"
                ).fetchall()
        return {row["job_type"]: int(row["total"]) for row in rows}


def _row_to_job_record(row: sqlite3.Row) -> JobRecord:
    result = json.loads(row["result_json"] or "{}")
    progress = json.loads(row["progress_json"] or "{}")
    if progress:
        result["progress"] = progress
    return JobRecord(
        job_id=row["job_id"],
        job_type=row["job_type"],
        status=JobStatus(row["status"]),
        message=row["message"] or "",
        created_at=_iso_to_dt(row["created_at"]) or _utc_now(),
        started_at=_iso_to_dt(row["started_at"]),
        finished_at=_iso_to_dt(row["finished_at"]),
        result=result,
        params=json.loads(row["params_json"] or "{}"),
    )


def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "event_type": row["event_type"],
        "message": row["message"],
        "payload": json.loads(row["payload_json"] or "{}"),
        "created_at": row["created_at"],
    }


_db: JobDatabase | None = None


def get_job_database(db_path: Path | None = None) -> JobDatabase:
    global _db
    if db_path is not None:
        return JobDatabase(db_path)
    if _db is None:
        _db = JobDatabase(JOBS_DB_PATH)
    return _db
