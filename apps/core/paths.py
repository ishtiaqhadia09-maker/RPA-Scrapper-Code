"""Canonical data directory paths for the RPA platform."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
AUTH_DIR = PROJECT_ROOT / "auth"
DOWNLOADS_DIR = DATA_DIR / "downloads"
EXPORTS_DIR = DATA_DIR / "exports"
LOGS_DIR = DATA_DIR / "logs"
RAW_DIR = DATA_DIR / "raw"
_DEFAULT_PROCESSED_DIR = DATA_DIR / "processed"
_DEFAULT_DBF_INPUT_DIR = RAW_DIR / "DBF_FILES"
_DEFAULT_IQVIA_DOWNLOAD_DIR = DOWNLOADS_DIR / "iqvia"
IQVIA_DATA_DIR = DATA_DIR / "iqvia"
DEFAULT_REPORT_SOURCES_PATH = IQVIA_DATA_DIR / "report_sources.tsv"
ACTIVE_REPORT_SOURCES_PATH = IQVIA_DATA_DIR / "active_report_sources.tsv"
ACTIVE_REPORT_SOURCES_META_PATH = IQVIA_DATA_DIR / "active_report_sources.meta.json"


def _resolve_path_from_env(key: str, default: Path) -> Path:
    """Read a path from .env (ReadConfig) or OS env; supports relative paths."""
    raw = os.getenv(key, "")
    if not raw:
        try:
            from apps.core.utils.read_utils import ReadConfig

            raw = ReadConfig.get(key, "")
        except FileNotFoundError:
            raw = ""
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


PROCESSED_DIR = _resolve_path_from_env("PROCESSED_OUTPUT_DIR", _DEFAULT_PROCESSED_DIR)
DEFAULT_IQVIA_DOWNLOAD_DIR = _resolve_path_from_env(
    "IQVIA_DOWNLOAD_DIR", _DEFAULT_IQVIA_DOWNLOAD_DIR
)
DEFAULT_DBF_INPUT_DIR = _resolve_path_from_env("DBF_INPUT_DIR", _DEFAULT_DBF_INPUT_DIR)
# Each conversion run writes to a new timestamped folder under PROCESSED_DIR.
PROCESSED_RUN_DIR_FORMAT = "%Y-%m-%d_%H-%M-%S"


def create_processed_run_dir(timestamp: datetime | None = None) -> Path:
    """Create data/processed/<date_time>/ for one conversion run."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = (timestamp or datetime.now()).strftime(PROCESSED_RUN_DIR_FORMAT)
    run_dir = PROCESSED_DIR / stamp
    suffix = 1
    while run_dir.exists():
        run_dir = PROCESSED_DIR / f"{stamp}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def remove_dir_if_empty(path: Path) -> bool:
    """Remove an output folder when a job failed before writing any files."""
    if not path.is_dir():
        return False
    if any(path.iterdir()):
        return False
    path.rmdir()
    return True


def ensure_data_dirs() -> None:
    """Create all runtime folders on first start (safe to call repeatedly)."""
    for path in (
        DATA_DIR,
        AUTH_DIR,
        DOWNLOADS_DIR,
        EXPORTS_DIR,
        LOGS_DIR,
        RAW_DIR,
        PROCESSED_DIR,
        DEFAULT_IQVIA_DOWNLOAD_DIR,
        IQVIA_DATA_DIR,
        DEFAULT_DBF_INPUT_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
