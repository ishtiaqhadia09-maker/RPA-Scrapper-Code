"""Orchestration layer between UI/workers and automation backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.core.job_logging import setup_job_logger
from apps.core.job_models import JobRecord
from apps.core.job_registry import get_registry
from apps.core.job_logging import count_files
from apps.core.paths import (
    DEFAULT_DBF_INPUT_DIR,
    DEFAULT_IQVIA_DOWNLOAD_DIR,
    LOGS_DIR,
    PROCESSED_DIR,
    ensure_data_dirs,
)
from apps.workers.convert_worker import run_convert


def _execute_full_workflow(job: JobRecord, **kwargs: Any) -> None:
    import importlib

    import apps.workers.download_worker as download_worker

    download_worker = importlib.reload(download_worker)
    registry = get_registry()
    logger = setup_job_logger(job.job_id, job.job_type)
    logger.info("Pipeline: download phase")
    registry.update_progress(
        job.job_id,
        phase="download",
        detail="Pipeline step 1/2 — download",
    )
    download_result = download_worker.run_download(
        headless=kwargs.get("headless", False),
        keep_open=kwargs.get("keep_open", False),
        download_dir=kwargs.get("download_dir"),
        job=job,
    )
    logger.info("Pipeline: convert phase")
    registry.update_progress(
        job.job_id,
        phase="convert",
        detail="Pipeline step 2/2 — convert",
    )
    convert_inputs = kwargs.get("convert_inputs") or [str(DEFAULT_DBF_INPUT_DIR)]
    convert_result = run_convert(
        inputs=convert_inputs,
        output_dir=kwargs.get("output_dir"),
        recursive=kwargs.get("recursive", True),
        encoding=kwargs.get("encoding"),
        csv_encoding=kwargs.get("csv_encoding", "utf-8"),
        delimiter=kwargs.get("delimiter", ","),
        overwrite=kwargs.get("overwrite", False),
        job=job,
    )
    job.result = {
        "download": download_result,
        "convert": convert_result,
        "progress": {
            "phase": "complete",
            "total": convert_result.get("input_count"),
            "completed": convert_result.get("converted_count"),
            "remaining": 0,
            "detail": "Pipeline finished",
        },
    }
    job.message = (
        f"Pipeline complete — {download_result['file_count']} download(s), "
        f"{convert_result['converted_count']} conversion(s)"
    )
    logger.info(job.message)


def start_full_workflow(
    *,
    headless: bool = False,
    keep_open: bool = False,
    download_dir: Path | str | None = None,
    convert_inputs: list[str | Path] | None = None,
    output_dir: Path | str | None = None,
    recursive: bool = True,
    encoding: str | None = None,
    csv_encoding: str = "utf-8",
    delimiter: str = ",",
    overwrite: bool = False,
) -> str:
    """Run download then convert in one background job; returns job_id."""
    ensure_data_dirs()
    kwargs: dict[str, Any] = {
        "headless": headless,
        "keep_open": keep_open,
        "download_dir": Path(download_dir) if download_dir else None,
        "convert_inputs": [str(p) for p in (convert_inputs or [DEFAULT_DBF_INPUT_DIR])],
        "output_dir": output_dir,
        "recursive": recursive,
        "encoding": encoding,
        "csv_encoding": csv_encoding,
        "delimiter": delimiter,
        "overwrite": overwrite,
    }
    return get_registry().start_background(
        "pipeline",
        lambda job: _execute_full_workflow(job, **kwargs),
        params=kwargs,
    )


def get_job_status(job_id: str) -> dict[str, Any] | None:
    return get_registry().get_status_snapshot(job_id)


def get_job_events(job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    return get_registry().list_events(job_id, limit=limit)


def get_job_stats() -> dict[str, Any]:
    return get_registry().get_stats()


def list_recent_jobs(
    limit: int = 20,
    *,
    job_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    return [
        job.to_dict()
        for job in get_registry().list_jobs(
            limit=limit,
            job_type=job_type,
            status=status,
        )
    ]


def get_data_summary() -> dict[str, int]:
    """Lightweight counts for dashboard display."""
    ensure_data_dirs()
    return {
        "downloads": count_files(DEFAULT_IQVIA_DOWNLOAD_DIR),
        "dbf_raw": count_files(DEFAULT_DBF_INPUT_DIR, "*.dbf"),
        "processed_csv": count_files(PROCESSED_DIR, "*.csv"),
        "processed_runs": sum(
            1 for path in PROCESSED_DIR.iterdir() if path.is_dir()
        )
        if PROCESSED_DIR.is_dir()
        else 0,
        "log_files": count_files(LOGS_DIR, "*.log"),
    }
