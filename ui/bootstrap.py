"""Backward-compatible bootstrap — use ensure_project_root() from Streamlit entry files."""

from __future__ import annotations

from ui.run_setup import ensure_project_root

__all__ = ["ensure_project_root"]
