"""Job tracking with in-memory state and SQLite history."""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from apps.core.job_db import JobDatabase, get_job_database
from apps.core.job_models import JobRecord, JobStatus


class JobRegistry:
    """Thread-safe job store backed by SQLite for history."""

    def __init__(self, db: JobDatabase | None = None) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._db = db or get_job_database()

    def _persist(self, job: JobRecord) -> None:
        self._db.save_job(job, job.params)

    def create(self, job_type: str, params: dict[str, Any] | None = None) -> JobRecord:
        job = JobRecord(
            job_id=str(uuid.uuid4()),
            job_type=job_type,
            status=JobStatus.PENDING,
            params=params or {},
        )
        with self._lock:
            self._jobs[job.job_id] = job
        self._persist(job)
        self._db.add_event(job.job_id, "created", f"Job {job.job_id} created")
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job
        return self._db.get_job(job_id)

    def get_status_snapshot(self, job_id: str) -> dict[str, Any] | None:
        """Thread-safe copy of job state for UI polling."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                return copy.deepcopy(job.to_dict())
        loaded = self._db.get_job(job_id)
        return loaded.to_dict() if loaded else None

    def update_progress(self, job_id: str, **fields: Any) -> None:
        """Update live progress fields (thread-safe) for UI polling."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            progress = job.result.setdefault("progress", {})
            progress.update(fields)
            total = progress.get("total")
            completed = progress.get("completed")
            if total is not None and completed is not None:
                progress["remaining"] = max(0, int(total) - int(completed))
        self._persist(job)

    def list_jobs(
        self,
        limit: int = 50,
        *,
        job_type: str | None = None,
        status: str | None = None,
    ) -> list[JobRecord]:
        db_jobs = self._db.list_jobs(limit=limit, job_type=job_type, status=status)
        with self._lock:
            # O(n) index of DB results for fast in-place merge.
            index_map = {job.job_id: i for i, job in enumerate(db_jobs)}
            for job_id, active in self._jobs.items():
                if job_id in index_map:
                    db_jobs[index_map[job_id]] = active
                else:
                    if job_type and active.job_type != job_type:
                        continue
                    if status and active.status.value != status:
                        continue
                    db_jobs.insert(0, active)
        db_jobs.sort(key=lambda item: item.created_at, reverse=True)
        return db_jobs[:limit]

    def list_events(self, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.list_events(job_id, limit=limit)

    def get_stats(self) -> dict[str, Any]:
        return {
            "by_status": self._db.count_by_status(),
            "by_type": self._db.count_by_type(),
        }

    def start_background(
        self,
        job_type: str,
        target: Callable[[JobRecord], None],
        params: dict[str, Any] | None = None,
    ) -> str:
        job = self.create(job_type, params=params)

        def _runner() -> None:
            with self._lock:
                job.status = JobStatus.RUNNING
                job.started_at = datetime.now(timezone.utc)
            self._persist(job)
            self._db.add_event(job.job_id, "started", "Job execution started")
            try:
                target(job)
                with self._lock:
                    job.status = JobStatus.SUCCESS
                    if not job.message:
                        job.message = "Completed successfully"
                self._persist(job)
                self._db.add_event(job.job_id, "completed", job.message)
            except Exception as exc:
                with self._lock:
                    job.status = JobStatus.FAILED
                    job.message = str(exc)
                    job.result["error"] = str(exc)
                self._persist(job)
                self._db.add_event(
                    job.job_id,
                    "failed",
                    job.message,
                    payload={"error": str(exc)},
                )
            finally:
                with self._lock:
                    job.finished_at = datetime.now(timezone.utc)
                self._persist(job)

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        return job.job_id


_registry = JobRegistry()


def get_registry() -> JobRegistry:
    return _registry


__all__ = ["JobRecord", "JobRegistry", "JobStatus", "get_registry"]
