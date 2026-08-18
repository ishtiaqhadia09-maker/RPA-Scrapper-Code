# Updated `apps/workers/convert_worker.py`

"""DBF-to-CSV conversion worker — runs conv_dbf_to_csv.py directly."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from apps.core.job_logging import setup_job_logger
from apps.core.job_models import JobRecord
from apps.core.job_registry import get_registry
from apps.core.paths import DEFAULT_DBF_INPUT_DIR


CONVERTER_SCRIPT = Path(__file__).with_name("conv_dbf_to_csv.py")


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
    """Run conv_dbf_to_csv.py directly."""

    command = [
        sys.executable,
        str(CONVERTER_SCRIPT),
        *[str(path) for path in inputs],
    ]

    if output_dir:
        command.extend(["--output-dir", str(output_dir)])

    if recursive:
        command.append("--recursive")

    if overwrite:
        command.append("--overwrite")

    if encoding:
        command.extend(["--encoding", encoding])

    if csv_encoding != "utf-8":
        command.extend(["--csv-encoding", csv_encoding])

    if delimiter != ",":
        command.extend(["--delimiter", delimiter])

    if job is not None:
        get_registry().update_progress(
            job.job_id,
            phase="convert",
            detail="Running DBF conversion...",
        )

    result = subprocess.run(
        command,
        check=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )

    return {
        "return_code": result.returncode,
        "converter_script": str(CONVERTER_SCRIPT.resolve()),
    }


def _execute_convert_job(job: JobRecord, **kwargs: Any) -> None:
    logger = setup_job_logger(job.job_id, job.job_type)

    logger.info(
        "Running converter: %s",
        CONVERTER_SCRIPT.resolve(),
    )

    result = run_convert(**kwargs, job=job)

    job.result = result
    job.message = "DBF conversion completed."

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
    """Start the converter in the existing background job."""

    input_paths = inputs or [str(DEFAULT_DBF_INPUT_DIR)]

    # Keep the existing behavior:
    # timestamped output is handled by the UI only when custom output
    # is not requested.
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
