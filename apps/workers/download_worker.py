"""Download job worker — wraps scrapers without UI dependencies."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from apps.core.job_logging import attach_logger_to_module, count_files, setup_job_logger
from apps.core.job_models import JobRecord
from apps.core.job_registry import get_registry
from apps.core.paths import ensure_data_dirs, get_iqvia_download_dir
from apps.core.utils.read_utils import ReadConfig
from apps.scrapers.iqvia.report_sources import resolve_report_sources_path

SCRAPER_LOGGER_NAME = "apps.scrapers.iqvia"


def _load_iqvia_bot_class():
    """Reload scraper modules so code changes apply without restarting Streamlit."""
    import apps.core.utils.read_utils as read_utils
    import apps.scrapers.iqvia.page_locators as page_locators
    import apps.scrapers.iqvia.automation_guard as automation_guard
    import apps.scrapers.iqvia.report_sources as report_sources
    import apps.scrapers.iqvia.iqvia_bot as iqvia_bot

    importlib.reload(read_utils)
    importlib.reload(automation_guard)
    importlib.reload(report_sources)
    importlib.reload(page_locators)
    importlib.reload(iqvia_bot)
    return iqvia_bot.IqviaBot


def run_download(
    *,
    headless: bool = False,
    keep_open: bool = False,
    download_dir: Path | None = None,
    job: JobRecord | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Run IQVIA download synchronously (used by workers and pipeline)."""
    ensure_data_dirs()
    target_dir = download_dir or get_iqvia_download_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    registry = get_registry()

    def _progress(detail: str) -> None:
        if job is None:
            return
        registry.update_progress(
            job.job_id,
            phase="download",
            total=None,
            completed=None,
            current_file=None,
            detail=detail,
        )
        if logger is not None:
            logger.info(detail)

    _progress("Starting IQVIA browser automation…")

    IqviaBot = _load_iqvia_bot_class()
    bot = IqviaBot(headless=headless, download_dir=target_dir)
    try:
        bot.run(keep_open=keep_open)
    finally:
        bot.close()

    file_count = count_files(target_dir)
    result: dict[str, Any] = {
        "download_dir": str(target_dir.resolve()),
        "file_count": file_count,
    }
    if bot.saved_download_path is not None:
        result["saved_file"] = str(bot.saved_download_path)
        _progress(f"Saved download: {bot.saved_download_path.name}")
    return result


def _execute_download_job(job: JobRecord, **kwargs: Any) -> None:
    logger = setup_job_logger(job.job_id, job.job_type)
    attach_logger_to_module(SCRAPER_LOGGER_NAME, logger)
    logger.info("Starting download job")
    logger.info("Download folder: %s", kwargs.get("download_dir") or get_iqvia_download_dir())
    report_sources_path = resolve_report_sources_path()
    logger.info("Report sources file: %s", report_sources_path)
    ReadConfig.reload()
    only_row = ReadConfig.getReportRow()
    if only_row is not None:
        logger.info("IQVIA_ROW=%d — single-product mode", only_row)
    result = run_download(**kwargs, job=job, logger=logger)
    progress = job.result.get("progress", {})
    if progress:
        job.result = {**result, "progress": progress}
    else:
        job.result = result
    saved_file = result.get("saved_file")
    if saved_file:
        job.message = f"Download finished — saved to {saved_file}"
    else:
        job.message = (
            f"Download finished — {result['file_count']} file(s) in {result['download_dir']}"
        )
    logger.info(job.message)


def start_download_job(
    *,
    headless: bool = False,
    keep_open: bool = False,
    download_dir: Path | str | None = None,
) -> str:
    """Start download in a background thread; returns job_id."""
    path = Path(download_dir) if download_dir else None
    kwargs = {
        "headless": headless,
        "keep_open": keep_open,
        "download_dir": path,
    }
    return get_registry().start_background(
        "download",
        lambda job: _execute_download_job(job, **kwargs),
        params=kwargs,
    )
