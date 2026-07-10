"""Download control page — triggers IQVIA download worker."""

import runpy
from datetime import datetime
from pathlib import Path

_path = Path(__file__).resolve()
_ui_dir = _path.parent if (_path.parent / "load_setup.py").is_file() else _path.parents[1]
runpy.run_path(str(_ui_dir / "load_setup.py"), run_name="_load_setup")

import streamlit as st

from apps.core.paths import DEFAULT_IQVIA_DOWNLOAD_DIR
from apps.engines.pipeline import get_data_summary, get_job_status
from ui.components import (
    get_active_job,
    render_job_log_panel,
    render_live_job_status,
    store_active_job,
)
from ui.automation_lock import render_automation_lock
from ui.job_helpers import is_active_job_status, is_job_controls_locked
from ui.report_sources_panel import render_report_sources_panel


def _reload_module(name: str):
    import importlib

    return importlib.reload(importlib.import_module(name))


@st.fragment(run_every=1)
def _unlock_download_form_when_done() -> None:
    if not st.session_state.get("_download_running"):
        return
    job_id = get_active_job()
    if not job_id:
        st.session_state["_download_running"] = False
        return
    payload = get_job_status(job_id)
    if payload and not is_active_job_status(payload.get("status")):
        st.session_state["_download_running"] = False
        st.rerun()


st.set_page_config(page_title="Download", layout="wide")
st.title("IQVIA download")
st.caption(
    "Runs the IQVIA Playwright bot (`apps.scrapers.iqvia.iqvia_bot`) and saves "
    "the exported file under the download folder."
)

if st.session_state.get("_download_running"):
    _unlock_download_form_when_done()

summary = get_data_summary()
download_dir_path = Path(DEFAULT_IQVIA_DOWNLOAD_DIR)
recent_files = sorted(
    download_dir_path.glob("*"),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
) if download_dir_path.is_dir() else []

metric_cols = st.columns(2)
metric_cols[0].metric("Files in download folder", summary["downloads"])
metric_cols[1].metric(
    "Latest download",
    recent_files[0].name if recent_files else "—",
)

controls_locked = is_job_controls_locked(get_active_job()) or st.session_state.get(
    "_download_running", False
)

render_automation_lock(controls_locked)

render_report_sources_panel(disabled=controls_locked)

with st.form("download_form"):
    download_dir = st.text_input(
        "Download directory",
        value=str(DEFAULT_IQVIA_DOWNLOAD_DIR),
        disabled=controls_locked,
    )
    headless = st.checkbox(
        "Headless browser",
        value=False,
        disabled=controls_locked,
        help="Run Chromium without a visible window.",
    )
    keep_open = st.checkbox(
        "Keep browser open after download",
        value=False,
        disabled=controls_locked,
        help="If unchecked, the browser closes automatically once the file is saved.",
    )
    run_pipeline = st.checkbox(
        "After download, run DBF → CSV pipeline step",
        value=False,
        disabled=controls_locked,
    )
    submitted = st.form_submit_button(
        "Download file",
        type="primary",
        disabled=controls_locked,
    )

if submitted and not controls_locked:
    if run_pipeline:
        pipeline = _reload_module("apps.engines.pipeline")
        job_id = pipeline.start_full_workflow(
            headless=headless,
            keep_open=keep_open,
            download_dir=download_dir,
        )
        st.success(f"Full pipeline started — job `{job_id}`")
    else:
        download_worker = _reload_module("apps.workers.download_worker")
        job_id = download_worker.start_download_job(
            headless=headless,
            keep_open=keep_open,
            download_dir=download_dir,
        )
        st.success(f"Download job started — job `{job_id}`")
    store_active_job(job_id)
    st.session_state["_download_running"] = True
    st.rerun()

st.subheader("Active job (live)")
render_live_job_status(get_active_job(), controls_locked=controls_locked)

active_job_id = get_active_job()
active_payload = get_job_status(active_job_id) if active_job_id else None
if active_payload and active_payload.get("job_type") == "download":
    saved_file = (active_payload.get("result") or {}).get("saved_file")
    if saved_file:
        st.success(f"Saved file: `{saved_file}`")

render_job_log_panel(active_job_id, "download", title="Download log (live)")

st.divider()
st.subheader("Recent downloads")
if recent_files:
    st.dataframe(
        [
            {
                "file": path.name,
                "size_bytes": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
            for path in recent_files[:20]
        ],
        use_container_width=True,
    )
else:
    st.info(f"No files in `{DEFAULT_IQVIA_DOWNLOAD_DIR}` yet.")
