"""Streamlit helpers — block page interaction while automation runs."""

from __future__ import annotations

import streamlit as st


def render_automation_lock(locked: bool) -> None:
    """Show a notice and disable non-essential controls while a job is active."""
    if not locked:
        return

    st.warning(
        "Automation in progress — do not click in the browser or start another job. "
        "Controls are locked until the run finishes."
    )
    st.markdown(
        """
        <style>
        div[data-testid="stForm"] button:not([kind="primary"]) {
            pointer-events: none;
            opacity: 0.55;
        }
        section[data-testid="stSidebar"] a,
        section[data-testid="stSidebar"] button {
            pointer-events: none;
            opacity: 0.55;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
