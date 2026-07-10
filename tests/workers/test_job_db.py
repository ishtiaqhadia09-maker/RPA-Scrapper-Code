"""Tests for SQLite job persistence."""

import time
from pathlib import Path

from apps.core.job_db import JobDatabase
from apps.core.job_models import JobStatus
from apps.core.job_registry import JobRegistry


def test_job_persisted_and_reloaded(tmp_path: Path) -> None:
    db = JobDatabase(tmp_path / "test_jobs.db")
    registry = JobRegistry(db)

    def work(job):
        job.result["ok"] = True

    job_id = registry.start_background("test", work, params={"source": "unit-test"})
    record = None
    for _ in range(50):
        record = registry.get(job_id)
        if record and record.status in (JobStatus.SUCCESS, JobStatus.FAILED):
            break
        time.sleep(0.05)

    assert record is not None
    assert record.status == JobStatus.SUCCESS

    reloaded = JobRegistry(db).get(job_id)
    assert reloaded is not None
    assert reloaded.status == JobStatus.SUCCESS
    assert reloaded.params.get("source") == "unit-test"
    assert reloaded.result.get("ok") is True

    events = registry.list_events(job_id)
    event_types = [event["event_type"] for event in events]
    assert "created" in event_types
    assert "started" in event_types
    assert "completed" in event_types
