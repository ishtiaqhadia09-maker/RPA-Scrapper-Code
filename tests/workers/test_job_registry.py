"""Tests for background job registry."""

import time

from apps.core.job_registry import JobStatus, get_registry


def test_background_job_completes():
    registry = get_registry()

    def work(job):
        job.result["ok"] = True

    job_id = registry.start_background("test", work)
    record = registry.get(job_id)
    assert record is not None

    for _ in range(50):
        record = registry.get(job_id)
        if record and record.status in (JobStatus.SUCCESS, JobStatus.FAILED):
            break
        time.sleep(0.05)

    assert record is not None
    assert record.status == JobStatus.SUCCESS
    assert record.result.get("ok") is True
