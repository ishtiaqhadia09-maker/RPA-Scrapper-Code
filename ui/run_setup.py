"""Entry-point helper for Streamlit scripts — call before any ui/apps imports."""

from __future__ import annotations

import runpy
from pathlib import Path


def ensure_project_root(caller_file: str) -> None:
    start = Path(caller_file).resolve()
    ui_dir = (
        start.parent
        if (start.parent / "load_setup.py").is_file()
        else start.parents[1]
    )
    runpy.run_path(str(ui_dir / "load_setup.py"), run_name="_load_setup")
