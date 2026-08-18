"""DBF-to-CSV conversion worker.

Runs the actual conversion through apps/workers/conv_dbf_to_csv.py so the
Streamlit Start Conversion button uses the exact same converter as running:

    python apps/workers/conv_dbf_to_csv.py ...
"""

from __future__ import annotations

import subprocess
import sys
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


# Always resolve the converter relative to this worker.
# This guarantees we execute:
# apps/workers/conv_dbf_to_csv.py
_CONVERTER_SCRIPT = Path(__file__).with_name("conv_dbf_to_csv.py")


def _count_dbf_files(
    inputs: list[str | Path],
    recursive: bool,
) -> int:
    """Count DBF files for job progress without importing the converter."""
    files: set[Path] = set()

    for raw_input in inputs:
        path = Path(raw_input).expanduser().resolve()

        if not path.exists():
            raise FileNotFoundError(f"Input path does not exist: {path}")

        if path.is_file():
            if path.suffix.lower() == ".dbf":
                files.add(path)
            continue

        pattern = "**/*.dbf" if recursive else "*.dbf"
        files.update(
            file_path.resolve()
            for file_path in path.glob(pattern)
            if file_path.is_file()
        )

    return len(files)


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
    """Run conv_dbf_to_csv.py as the actual converter."""
    ensure_data_dirs()

    converter = _CONVERTER_SCRIPT.resolve()

    if not converter.is_file():
        raise FileNotFoundError(
            f"DBF converter script was not found: {converter}"
        )

    input_paths = [str(Path(path).expanduser().resolve()) for path in inputs]

    # Count files only for progress/status display.
    total_files = _count_dbf_files(input_paths, recursive)

    if total_files == 0:
        raise FileNotFoundError(
            "No DBF files were found in the provided inputs."
        )

    registry = get_registry()

    if job is not None:
        registry.update_progress(
            job.job_id,
            phase="convert",
            total=total_files,
            completed=0,
            current_file=None,
            detail=(
                f"Running {converter.name} "
                f"for {total_files} DBF file(s)…"
            ),
        )

    # If no custom output was supplied, preserve the existing behavior:
    # create a timestamped processed folder.
    use_timestamped = output_dir is None

    out_dir: Path | None = None

    try:
        if output_dir is not None:
            out_dir = Path(output_dir).expanduser().resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
        else:
            out_dir = create_processed_run_dir()

        # Build the exact command that would normally be run manually.
        #
        # Example:
        # python apps/workers/conv_dbf_to_csv.py
        #     data/raw/DBF_FILES
        #     --overwrite
        #     --recursive
        #     --output-dir processed/2026-08-18_17-00-00
        command: list[str] = [
            sys.executable,
            str(converter),
            *input_paths,
            "--output-dir",
            str(out_dir),
        ]

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
            registry.update_progress(
                job.job_id,
                phase="convert",
                total=total_files,
                completed=0,
                current_file=None,
                detail=(
                    f"Executing {converter} "
                    f"with {total_files} DBF file(s)…"
                ),
            )

        # IMPORTANT:
        # This directly executes conv_dbf_to_csv.py.
        # No conversion function is imported/called here.
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(converter.parents[2]),
        )

        output_lines: list[str] = []

        if process.stdout is not None:
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue

                output_lines.append(line)

                if job is not None:
                    registry.update_progress(
                        job.job_id,
                        phase="convert",
                        total=total_files,
                        completed=0,
                        current_file=None,
                        detail=line,
                    )

        return_code = process.wait()

        if return_code != 0:
            command_text = " ".join(command)
            raise RuntimeError(
                f"DBF conversion failed with exit code "
                f"{return_code}.\n"
                f"Command: {command_text}\n"
                f"Output:\n"
                + "\n".join(output_lines[-20:])
            )

        # Count the CSV files generated by the exact converter.
        output_file_count = count_files(out_dir, "*.csv")

        if job is not None:
            registry.update_progress(
                job.job_id,
                phase="convert",
                total=total_files,
                completed=total_files,
                current_file=None,
                detail=(
                    f"Finished conversion: "
                    f"{output_file_count} CSV file(s) created."
                ),
            )

        return {
            "input_count": total_files,
            "converted_count": output_file_count,
            "output_dir": str(out_dir.resolve()),
            "output_files": [
                str(path.resolve())
                for path in sorted(out_dir.glob("*.csv"))
            ],
            "export_file_count": output_file_count,
            "converter_script": str(converter),
        }

    except Exception:
        if use_timestamped and out_dir is not None:
            remove_dir_if_empty(out_dir)
        raise


def _execute_convert_job(
    job: JobRecord,
    **kwargs: Any,
) -> None:
    logger = setup_job_logger(job.job_id, job.job_type)

    logger.info(
        "Starting convert job with inputs=%s",
        kwargs.get("inputs"),
    )

    logger.info(
        "Using converter script: %s",
        _CONVERTER_SCRIPT.resolve(),
    )

    result = run_convert(**kwargs, job=job)

    progress = job.result.get("progress", {})

    job.result = {
        **result,
        "progress": progress,
    }

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
    """Start DBF conversion in a background thread."""

    input_paths = inputs or [str(DEFAULT_DBF_INPUT_DIR)]

    resolved_output = (
        None
        if use_timestamped_output
        else output_dir
    )

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
        lambda job: _execute_convert_job(
            job,
            **kwargs,
        ),
        params=kwargs,
    )