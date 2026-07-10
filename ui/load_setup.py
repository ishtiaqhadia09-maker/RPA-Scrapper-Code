"""Add project root to sys.path and create runtime folders on first start."""

from __future__ import annotations

import sys
from pathlib import Path


def install() -> Path:
    root = next(
        p
        for p in Path(__file__).resolve().parents
        if (p / "apps").is_dir() and (p / "ui").is_dir()
    )
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    from apps.core.paths import ensure_data_dirs
    from apps.scrapers.iqvia.report_sources import ensure_active_report_sources

    ensure_data_dirs()
    ensure_active_report_sources()
    return root


install()
