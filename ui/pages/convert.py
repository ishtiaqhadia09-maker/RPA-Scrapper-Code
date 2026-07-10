"""DBF conversion control page — triggers backend workers only."""

import runpy
from pathlib import Path

_path = Path(__file__).resolve()
_ui_dir = _path.parent if (_path.parent / "load_setup.py").is_file() else _path.parents[1]
runpy.run_path(str(_ui_dir / "load_setup.py"), run_name="_load_setup")

import streamlit as st

from apps.core.paths import DEFAULT_DBF_INPUT_DIR, PROCESSED_DIR, PROCESSED_RUN_DIR_FORMAT
from apps.engines.pipeline import get_data_summary, get_job_status
from apps.workers.convert_worker import start_convert_job
from ui.components import (
    get_active_job,
    render_live_job_status,
    store_active_job,
)
from ui.job_helpers import is_active_job_status, is_job_controls_locked


@st.fragment(run_every=1)
def _unlock_convert_form_when_done() -> None:
    """Re-enable the form after the background job finishes."""
    if not st.session_state.get("_convert_running"):
        return
    job_id = get_active_job()
    if not job_id:
        st.session_state["_convert_running"] = False
        return
    payload = get_job_status(job_id)
    if payload and not is_active_job_status(payload.get("status")):
        st.session_state["_convert_running"] = False
        st.rerun()


st.set_page_config(page_title="Convert", layout="wide")
st.title("DBF → CSV conversion")
st.caption("Runs `apps.workers.convert_worker` (wraps `conv_dbf_to_csv`).")

if st.session_state.get("_convert_running"):
    _unlock_convert_form_when_done()

summary = get_data_summary()
col1, col2 = st.columns(2)
col1.metric("DBF files (raw)", summary["dbf_raw"])
col2.metric("Processed CSV files", summary["processed_csv"])

st.info(
    f"Each run creates a new folder under `{PROCESSED_DIR}` "
    f"named like `{PROCESSED_RUN_DIR_FORMAT}` (date and time)."
)

controls_locked = is_job_controls_locked(get_active_job()) or st.session_state.get(
    "_convert_running", False
)

input_path = st.text_input(
    "Input path (file or directory)",
    value=str(DEFAULT_DBF_INPUT_DIR),
    disabled=controls_locked,
    key="convert_input_path",
)

custom_output_dir: str | None = None
with st.container(border=True):
    recursive = st.checkbox(
        "Search directories recursively",
        value=True,
        disabled=controls_locked,
        key="convert_recursive",
    )
    overwrite = st.checkbox(
        "Overwrite existing CSV files",
        value=False,
        disabled=controls_locked,
        key="convert_overwrite",
    )
    encoding = st.text_input(
        "DBF encoding (optional)",
        value="",
        disabled=controls_locked,
        key="convert_encoding",
    )

    use_custom_output = st.checkbox(
        "Use custom output folder (optional)",
        value=False,
        disabled=controls_locked,
        key="convert_use_custom_output",
    )
    if use_custom_output:
        custom_output_dir = st.text_input(
            "Custom output directory",
            value=str(PROCESSED_DIR),
            help="CSV files will be written into this folder.",
            disabled=controls_locked,
            key="convert_custom_output_dir",
        )

    start_conversion = st.button(
        "Start conversion",
        type="primary",
        disabled=controls_locked,
    )

if start_conversion and not controls_locked:
    job_id = start_convert_job(
        inputs=[input_path],
        output_dir=custom_output_dir or None,
        use_timestamped_output=not use_custom_output,
        recursive=recursive,
        overwrite=overwrite,
        encoding=encoding or None,
    )
    store_active_job(job_id)
    st.session_state["_convert_running"] = True
    st.success(f"Convert job started — job `{job_id}`")
    st.rerun()

st.subheader("Active job (live)")
render_live_job_status(get_active_job())
