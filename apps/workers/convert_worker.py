"""DBF-to-CSV conversion worker — reuses conversion logic from conv_dbf_to_csv."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.core.job_logging import count_files, setup_job_logger
from apps.core.job_models import JobRecord
from apps.core.job_registry import get_registry
from apps.core.paths import (
    DEFAULT_DBF_INPUT_DIR,
    create_processed_run_dir,
    ensure_data_dirs,
    remove_dir_if_empty,
)
from apps.workers.conv_dbf_to_csv import (
    EXCEL_DBF_FIELD_LIMIT,
    convert_dbf_to_csv,
    resolve_inputs,
)


def run_convert(
    *,
    inputs: list[str | Path],
    output_dir: Path | str | None = None,
    recursive: bool = True,
    encoding: str | None = None,
    csv_encoding: str = "utf-8",
    delimiter: str = ",",
    overwrite: bool = False,
    job: JobRecord | None = None,
) -> dict[str, Any]:
    """Convert DBF files to CSV synchronously."""
    ensure_data_dirs()
    raw_inputs = [str(path) for path in inputs]
    registry = get_registry()
    if job is not None:
        registry.update_progress(
            job.job_id,
            phase="convert",
            detail="Scanning for DBF files…",
        )
    dbf_files = resolve_inputs(raw_inputs, recursive)
    if not dbf_files:
        raise FileNotFoundError("No DBF files were found in the provided inputs.")

    out_dir: Path | None = None
    use_timestamped = output_dir is None
    try:
        if output_dir:
            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        else:
            out_dir = create_processed_run_dir()

        total_files = len(dbf_files)
        if job is not None:
            registry.update_progress(
                job.job_id,
                phase="convert",
                total=total_files,
                completed=0,
                current_file=None,
                detail=f"Writing CSV files to {out_dir.name}",
            )

        converted: list[str] = []
        for index, dbf_path in enumerate(dbf_files, start=1):
            if job is not None:
                registry.update_progress(
                    job.job_id,
                    phase="convert",
                    total=total_files,
                    completed=index - 1,
                    current_file=dbf_path.name,
                    detail=f"Converting {dbf_path.name}",
                )
            csv_path = convert_dbf_to_csv(
                dbf_path=dbf_path,
                output_dir=out_dir,
                encoding=encoding,
                csv_encoding=csv_encoding,
                delimiter=delimiter,
                overwrite=overwrite,
            )
            converted.append(str(csv_path))
            if job is not None:
                registry.update_progress(
                    job.job_id,
                    phase="convert",
                    total=total_files,
                    completed=index,
                    current_file=dbf_path.name,
                    detail=f"Finished {dbf_path.name}",
                )

        return {
            "input_count": len(dbf_files),
            "converted_count": len(converted),
            "output_dir": str(out_dir.resolve()),
            "output_files": converted,
            "export_file_count": count_files(out_dir, "*.csv"),
        }
    except Exception:
        if use_timestamped and out_dir is not None:
            remove_dir_if_empty(out_dir)
        raise


def _execute_convert_job(job: JobRecord, **kwargs: Any) -> None:
    logger = setup_job_logger(job.job_id, job.job_type)
    logger.info("Starting convert job with inputs=%s", kwargs.get("inputs"))
    result = run_convert(**kwargs, job=job)
    progress = job.result.get("progress", {})
    job.result = {**result, "progress": progress}
    job.message = (
        f"Converted {result['converted_count']} file(s) "
        f"into {result['output_dir']}"
    )
    logger.info(job.message)


def start_convert_job(
    *,
    inputs: list[str | Path] | None = None,
    output_dir: Path | str | None = None,
    use_timestamped_output: bool = True,
    recursive: bool = True,
    encoding: str | None = None,
    csv_encoding: str = "utf-8",
    delimiter: str = ",",
    overwrite: bool = False,
) -> str:
    """Start DBF conversion in a background thread; returns job_id."""
    input_paths = inputs or [str(DEFAULT_DBF_INPUT_DIR)]
    resolved_output = None if use_timestamped_output else output_dir
    kwargs = {
        "inputs": input_paths,
        "output_dir": resolved_output,
        "recursive": recursive,
        "encoding": encoding,
        "csv_encoding": csv_encoding,
        "delimiter": delimiter,
        "overwrite": overwrite,
    }
    return get_registry().start_background(
        "convert",
        lambda job: _execute_convert_job(job, **kwargs),
        params=kwargs,
    )
