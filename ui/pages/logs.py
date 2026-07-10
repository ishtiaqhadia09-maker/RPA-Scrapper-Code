"""Job history and log files."""

import runpy
from pathlib import Path

_path = Path(__file__).resolve()
_ui_dir = _path.parent if (_path.parent / "load_setup.py").is_file() else _path.parents[1]
runpy.run_path(str(_ui_dir / "load_setup.py"), run_name="_load_setup")

from datetime import datetime

import streamlit as st

from apps.core.database import JOBS_DB_PATH
from apps.core.job_logging import list_log_files, read_log_tail
from apps.core.paths import LOGS_DIR, ensure_data_dirs
from apps.engines.pipeline import get_job_events, get_job_stats, list_recent_jobs


def _job_dropdown_label(job: dict[str, object]) -> str:
    created = str(job.get("created_at") or "")
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        stamp = dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        stamp = created[:19].replace("T", " ") if created else "unknown time"
    job_type = job.get("job_type", "?")
    status = job.get("status", "?")
    short_id = str(job.get("job_id", ""))[:8]
    return f"{stamp} — {job_type} — {status} ({short_id})"


def _render_log_file_viewer(
    log_files: list[Path],
    *,
    empty_message: str,
) -> None:
    if not log_files:
        st.info(empty_message)
        return

    selected_log = st.selectbox(
        "Select log file",
        options=log_files,
        format_func=lambda path: path.name,
    )
    tail_lines = st.slider("Lines to show", min_value=50, max_value=500, value=200)
    if selected_log:
        st.caption(f"`{selected_log}`")
        st.text_area(
            "Log content",
            value=read_log_tail(selected_log, lines=tail_lines),
            height=400,
            label_visibility="collapsed",
        )


st.set_page_config(page_title="Logs", layout="wide")
st.title("Job history & logs")

ensure_data_dirs()

st.caption(f"Job history database: `{JOBS_DB_PATH}`")

stats = get_job_stats()
stat_cols = st.columns(2)
with stat_cols[0]:
    st.write("**Jobs by status**")
    if stats["by_status"]:
        st.json(stats["by_status"])
    else:
        st.write("No jobs yet.")
with stat_cols[1]:
    st.write("**Jobs by type**")
    if stats["by_type"]:
        st.json(stats["by_type"])
    else:
        st.write("No jobs yet.")

log_tab, history_tab = st.tabs(["Log files", "Job history"])

with log_tab:
    log_type = st.radio(
        "Log category",
        options=["download", "convert", "pipeline", "all"],
        horizontal=True,
        format_func=lambda value: value.title(),
    )
    job_type_filter = None if log_type == "all" else log_type
    log_files = list_log_files(job_type_filter)

    if log_type == "download":
        st.subheader("Download logs")
        _render_log_file_viewer(
            log_files,
            empty_message=(
                f"No download log files in `{LOGS_DIR}` yet. "
                "Start a download from the Download page."
            ),
        )
    elif log_type == "convert":
        st.subheader("Convert logs")
        _render_log_file_viewer(
            log_files,
            empty_message=f"No convert log files in `{LOGS_DIR}` yet.",
        )
    elif log_type == "pipeline":
        st.subheader("Pipeline logs")
        _render_log_file_viewer(
            log_files,
            empty_message=f"No pipeline log files in `{LOGS_DIR}` yet.",
        )
    else:
        st.subheader("All log files")
        _render_log_file_viewer(
            log_files,
            empty_message=f"No log files in `{LOGS_DIR}` yet. Logs appear when jobs run.",
        )

with history_tab:
    filter_cols = st.columns(2)
    with filter_cols[0]:
        history_type_filter = st.selectbox(
            "Filter by type",
            options=["All", "convert", "download", "pipeline"],
            index=0,
        )
    with filter_cols[1]:
        status_filter = st.selectbox(
            "Filter by status",
            options=["All", "pending", "running", "success", "failed"],
            index=0,
        )

    job_type = None if history_type_filter == "All" else history_type_filter
    status = None if status_filter == "All" else status_filter

    st.subheader("Job history (SQLite)")
    jobs = list_recent_jobs(limit=50, job_type=job_type, status=status)
    if jobs:
        st.dataframe(jobs, use_container_width=True)
    else:
        st.write("No jobs recorded yet.")

    job_lookup = {str(job["job_id"]): job for job in jobs}

    def _format_job_option(job_id: str) -> str:
        if not job_id:
            return "Select a job…"
        job = job_lookup.get(job_id)
        if job is None:
            return job_id
        return _job_dropdown_label(job)

    selected_job_id = st.selectbox(
        "Inspect job events",
        options=[""] + list(job_lookup.keys()),
        format_func=_format_job_option,
    )

    if selected_job_id:
        st.caption(f"Job ID: `{selected_job_id}`")
        events = get_job_events(selected_job_id)
        if events:
            st.dataframe(events, use_container_width=True)
        else:
            st.write("No events for this job.")

        selected_job = job_lookup.get(selected_job_id, {})
        selected_type = str(selected_job.get("job_type") or "")
        matching_logs = list_log_files(selected_type or None)
        matching_logs = [
            path
            for path in matching_logs
            if selected_job_id in path.name
        ]
        if matching_logs:
            st.subheader("Log file for selected job")
            st.text_area(
                "Job log",
                value=read_log_tail(matching_logs[0], lines=200),
                height=300,
                label_visibility="collapsed",
            )
