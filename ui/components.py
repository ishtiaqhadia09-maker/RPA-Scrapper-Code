"""Presentation-only helpers for Streamlit pages (no business logic)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from apps.core.job_logging import job_log_path, read_log_tail
from apps.core.job_models import JobStatus
from apps.engines.pipeline import get_job_status

from ui.job_helpers import is_active_job_status


def store_active_job(job_id: str, key: str = "active_job_id") -> None:
    st.session_state[key] = job_id


def get_active_job(key: str = "active_job_id") -> str | None:
    return st.session_state.get(key)


def _normalize_progress(payload: dict[str, Any]) -> dict[str, Any]:
    progress: dict[str, Any] = dict(payload.get("progress") or {})
    result = payload.get("result")
    if isinstance(result, dict):
        nested = result.get("progress")
        if isinstance(nested, dict):
            progress = {**nested, **progress}
        if progress.get("total") is None and result.get("input_count") is not None:
            progress.setdefault("total", result["input_count"])
            progress.setdefault("completed", result.get("converted_count", 0))
    total = progress.get("total")
    completed = progress.get("completed")
    if total is not None and completed is not None:
        progress["remaining"] = max(0, int(total) - int(completed))
    return progress


def _draw_job_status(job_id: str) -> None:
    payload = get_job_status(job_id)
    if payload is None:
        st.warning(f"Job `{job_id}` was not found (process may have restarted).")
        return

    status = payload["status"]
    progress = _normalize_progress(payload)
    phase = progress.get("phase", "—")
    detail = progress.get("detail", "")
    total = progress.get("total")
    completed = progress.get("completed")
    remaining = progress.get("remaining")
    current_file = progress.get("current_file")

    if status == JobStatus.RUNNING.value:
        st.status(detail or "Running…", state="running")
    elif status == JobStatus.SUCCESS.value:
        st.status(payload.get("message", "Success"), state="complete")
    elif status == JobStatus.FAILED.value:
        st.status(payload.get("message", "Failed"), state="error")
    else:
        st.status("Waiting to start…", state="running")

    if total is not None and completed is not None:
        total_i = int(total)
        completed_i = int(completed)
        if total_i > 0:
            ratio = min(1.0, completed_i / total_i)
            st.write(f"**Progress:** {completed_i} / {total_i} files")
            st.progress(ratio)
            col1, col2, col3 = st.columns(3)
            col1.metric("Processed", completed_i)
            col2.metric(
                "Remaining",
                int(remaining) if remaining is not None else max(0, total_i - completed_i),
            )
            col3.metric("Total", total_i)
            if current_file:
                st.caption(f"Current file: `{current_file}`")
        elif is_active_job_status(status):
            st.write(f"**Progress:** {completed_i} / {total_i} files")
            st.progress(0.0)
    elif is_active_job_status(status):
        st.info(detail or f"Phase: {phase}")

    st.caption(f"Job `{job_id}` · type `{payload.get('job_type')}` · phase `{phase}`")

    with st.expander("Full job payload", expanded=False):
        st.json(payload)


@st.fragment(run_every=1)
def _auto_refresh_job_panel() -> None:
    """Reruns every 1s; reads job id from session state (no fragment arguments)."""
    job_id = st.session_state.get("active_job_id")
    if not job_id:
        st.info("No active job.")
        return
    _draw_job_status(job_id)


def render_live_job_status(
    job_id: str | None,
    *,
    controls_locked: bool = False,
) -> None:
    """Live job panel with manual refresh + optional auto-refresh."""
    if job_id:
        st.session_state["active_job_id"] = job_id

    controls = st.columns([1, 1, 2])
    with controls[0]:
        if st.button(
            "Refresh status",
            use_container_width=True,
            disabled=controls_locked,
        ):
            st.rerun()
    with controls[1]:
        st.session_state["auto_refresh_jobs"] = st.checkbox(
            "Auto-refresh (1s)",
            value=st.session_state.get("auto_refresh_jobs", True),
        )
    with controls[2]:
        st.caption("Updates automatically while a job is running.")

    active_id = st.session_state.get("active_job_id")
    if not active_id:
        st.info("No job selected. Start a job to track live progress here.")
        return

    payload = get_job_status(active_id)
    if payload is None:
        st.warning(f"Job `{active_id}` was not found (process may have restarted).")
        return

    auto_refresh = st.session_state.get("auto_refresh_jobs", True)
    if auto_refresh and is_active_job_status(payload.get("status")):
        _auto_refresh_job_panel()
    else:
        _draw_job_status(active_id)


def render_job_log_panel(
    job_id: str | None,
    job_type: str,
    *,
    title: str = "Job log",
    tail_lines: int = 200,
    auto_refresh: bool = True,
) -> None:
    """Show the log file for a background job."""
    st.subheader(title)
    if not job_id:
        st.info("Start a job to view its log output here.")
        return

    log_path = job_log_path(job_id, job_type)
    if auto_refresh and st.session_state.get("auto_refresh_jobs", True):
        _auto_refresh_job_log_panel(job_id, job_type, tail_lines)
    else:
        _draw_job_log(job_id, job_type, tail_lines)


@st.fragment(run_every=2)
def _auto_refresh_job_log_panel(
    job_id: str,
    job_type: str,
    tail_lines: int,
) -> None:
    _draw_job_log(job_id, job_type, tail_lines)


def _draw_job_log(job_id: str, job_type: str, tail_lines: int) -> None:
    log_path = job_log_path(job_id, job_type)
    st.caption(f"Log file: `{log_path}`")
    if not log_path.is_file():
        st.info("Log file will appear once the job starts writing output.")
        return
    st.text_area(
        "Log output",
        value=read_log_tail(log_path, lines=tail_lines),
        height=320,
        label_visibility="collapsed",
    )


def render_metric_row(summary: dict[str, int]) -> None:
    cols = st.columns(len(summary))
    for col, (label, value) in zip(cols, summary.items(), strict=True):
        col.metric(label.replace("_", " ").title(), value)
