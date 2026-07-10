"""File logging helpers for background jobs."""

from __future__ import annotations

import logging
from pathlib import Path

from apps.core.paths import LOGS_DIR, ensure_data_dirs


def job_log_path(job_id: str, job_type: str) -> Path:
    ensure_data_dirs()
    return LOGS_DIR / f"{job_type}_{job_id}.log"


def list_log_files(job_type: str | None = None) -> list[Path]:
    ensure_data_dirs()
    files = sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if job_type:
        prefix = f"{job_type}_"
        files = [path for path in files if path.name.startswith(prefix)]
    return files


def read_log_tail(path: Path, *, lines: int = 200) -> str:
    """Return the last *lines* lines of a log file without reading the whole thing."""
    if not path.is_file():
        return ""
    # Read in chunks from the end to avoid loading huge files entirely.
    chunk = 1 << 14  # 16 KB per read
    collected: list[str] = []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        remaining = fh.tell()
        buffer = b""
        while remaining > 0 and len(collected) <= lines:
            read_size = min(chunk, remaining)
            remaining -= read_size
            fh.seek(remaining)
            buffer = fh.read(read_size) + buffer
            collected = buffer.decode("utf-8", errors="replace").splitlines()
    return "\n".join(collected[-lines:])


def attach_logger_to_module(module_name: str, job_logger: logging.Logger) -> None:
    """Mirror job log output into a module logger (e.g. scraper bot)."""
    module_logger = logging.getLogger(module_name)
    module_logger.setLevel(logging.INFO)
    for handler in job_logger.handlers:
        if handler not in module_logger.handlers:
            module_logger.addHandler(handler)


def setup_job_logger(job_id: str, job_type: str) -> logging.Logger:
    ensure_data_dirs()
    logger = logging.getLogger(f"rpa.job.{job_type}.{job_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    log_path = job_log_path(job_id, job_type)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.info("Job %s started (%s)", job_id, job_type)
    return logger


def count_files(directory: Path, pattern: str = "*") -> int:
    """Count files matching *pattern* in *directory* (non-recursive for flat dirs)."""
    if not directory.is_dir():
        return 0
    if pattern == "*":
        return sum(1 for p in directory.iterdir() if p.is_file())
    return sum(1 for p in directory.rglob(pattern) if p.is_file())
