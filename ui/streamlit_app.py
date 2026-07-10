"""
RPA control dashboard — entry point for the Streamlit UI.

Run from project root:

    streamlit run ui/streamlit_app.py
"""

import runpy
from pathlib import Path

_path = Path(__file__).resolve()
_ui_dir = _path.parent if (_path.parent / "load_setup.py").is_file() else _path.parents[1]
runpy.run_path(str(_ui_dir / "load_setup.py"), run_name="_load_setup")

import streamlit as st

from apps.core.database import JOBS_DB_PATH
from apps.core.paths import (
    DEFAULT_DBF_INPUT_DIR,
    DEFAULT_IQVIA_DOWNLOAD_DIR,
    LOGS_DIR,
    PROCESSED_DIR,
    PROCESSED_RUN_DIR_FORMAT,
    ensure_data_dirs,
)
from apps.engines.pipeline import get_data_summary, get_job_stats, list_recent_jobs
from ui.components import render_metric_row

st.set_page_config(
    page_title="RPA Scraper Dashboard",
    page_icon="🤖",
    layout="wide",
)

ensure_data_dirs()

st.title("RPA Scraper Dashboard")
st.caption(
    "Control plane only — automation runs in `apps/workers` and `apps/scrapers`."
)

summary = get_data_summary()
render_metric_row(summary)

st.divider()
st.subheader("Data locations")
st.code(
    "\n".join(
        [
            f"Downloads:  {DEFAULT_IQVIA_DOWNLOAD_DIR}",
            f"DBF input:  {DEFAULT_DBF_INPUT_DIR}",
            f"Processed:  {PROCESSED_DIR}/<{PROCESSED_RUN_DIR_FORMAT}>/",
            f"Job DB:     {JOBS_DB_PATH}",
            f"Logs:       {LOGS_DIR}",
        ]
    )
)

st.divider()
st.subheader("Job statistics")
st.json(get_job_stats())

st.divider()
st.subheader("Recent jobs")
jobs = list_recent_jobs(limit=10)
if jobs:
    st.dataframe(jobs, use_container_width=True)
else:
    st.write("No jobs yet. Use **Download**, **Convert**, or run a full pipeline from those pages.")
