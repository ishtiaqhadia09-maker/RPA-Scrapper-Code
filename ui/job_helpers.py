"""Job status helpers for UI (no Streamlit imports)."""

from __future__ import annotations

from apps.core.job_models import JobStatus
from apps.engines.pipeline import get_job_status


def is_active_job_status(status: str | None) -> bool:
    return status in (JobStatus.PENDING.value, JobStatus.RUNNING.value)


def is_job_controls_locked(job_id: str | None) -> bool:
    """True while the given job is pending or running."""
    if not job_id:
        return False
    payload = get_job_status(job_id)
    if payload is None:
        return False
    return is_active_job_status(payload.get("status"))
