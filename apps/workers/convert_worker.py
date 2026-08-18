# `apps/workers/convert_worker.py`

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

    command = [
        sys.executable,
        str(CONVERTER_SCRIPT),
        *[str(path) for path in inputs],
    ]

    if job is not None:
        get_registry().update_progress(
            job.job_id,
            phase="convert",
            detail="Running DBF conversion...",
        )

    subprocess.run(
        command,
        check=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )

    return {
        "return_code": 0,
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

    input_paths = inputs or [str(DEFAULT_DBF_INPUT_DIR)]

    kwargs = {
        "inputs": input_paths,
        "output_dir": output_dir,
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
