"""Convert DBF exports to UTF-8 CSV.

When no input paths are given on the command line, DBF files are scanned from
``DBF_INPUT_DIR`` in the project ``.env`` (default: ``data/raw/DBF_FILES``).

Examples:
    python3 apps/workers/conv_dbf_to_csv.py
    python3 apps/workers/conv_dbf_to_csv.py data/raw/DBF_FILES/COLAR.dbf
    python3 apps/workers/conv_dbf_to_csv.py data/raw/DBF_FILES --recursive
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Iterable

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dbfread import DBF

from apps.core.paths import DEFAULT_DBF_INPUT_DIR

_CSV_BATCH_SIZE = 1000
# Excel / dBase tools only display 255 DBF fields. IMS files store extra
# MTH/MAT measure fields after that; keep CSV aligned with the Excel view.
EXCEL_DBF_FIELD_LIMIT = 255


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert DBF files to UTF-8 CSV."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help=(
            "DBF files or directories. If omitted, scans DBF_INPUT_DIR from .env "
            f"(currently {DEFAULT_DBF_INPUT_DIR})."
        ),
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Optional directory where CSV files will be written.",
    )
    parser.add_argument(
        "--encoding",
        default=None,
        help="Optional DBF text encoding override, for example cp1252.",
    )
    parser.add_argument(
        "--csv-encoding",
        default="utf-8",
        help="Encoding to use for generated CSV files.",
    )
    parser.add_argument(
        "--delimiter",
        default=",",
        help="CSV delimiter character.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively search directories for .dbf files.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing CSV files.",
    )
    parser.add_argument(
        "--max-fields",
        type=int,
        default=EXCEL_DBF_FIELD_LIMIT,
        help=(
            "Write at most this many DBF fields (default 255, matching Excel). "
            "Use 0 to export every field."
        ),
    )
    return parser.parse_args()


def resolve_inputs(raw_inputs: Iterable[str], recursive: bool) -> list[Path]:
    dbf_files: set[Path] = set()

    for raw_input in raw_inputs:
        path = Path(raw_input).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Input path does not exist: {path}")

        if path.is_file():
            if path.suffix.lower() != ".dbf":
                raise ValueError(f"Expected a .dbf file, got: {path}")
            dbf_files.add(path)
            continue

        pattern = "**/*.dbf" if recursive else "*.dbf"
        dbf_files.update(file_path.resolve() for file_path in path.glob(pattern))

    if not dbf_files:
        raise FileNotFoundError("No DBF files were found in the provided inputs.")

    return sorted(dbf_files)


def build_output_path(dbf_path: Path, output_dir: Path | None) -> Path:
    if output_dir is None:
        return dbf_path.with_suffix(".csv")

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{dbf_path.stem}.csv"


def select_csv_field_names(
    field_names: list[str],
    max_fields: int = EXCEL_DBF_FIELD_LIMIT,
) -> list[str]:
    """Keep CSV columns aligned with Excel's 255-field DBF view."""
    names = list(field_names)
    if max_fields <= 0:
        return names
    return names[:max_fields]


def _csv_value(value: Any) -> Any:
    """Write DBF values to CSV without reformatting parsed field data."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _open_dbf(dbf_path: Path, encoding: str | None) -> DBF:
    kwargs: dict[str, Any] = {
        "ignore_missing_memofile": True,
        "char_decode_errors": "replace",
    }
    if encoding:
        kwargs["encoding"] = encoding
    return DBF(str(dbf_path), **kwargs)


def convert_dbf_to_csv(
    dbf_path: Path,
    output_dir: Path | None,
    encoding: str | None,
    csv_encoding: str,
    delimiter: str,
    overwrite: bool,
    max_fields: int = EXCEL_DBF_FIELD_LIMIT,
) -> Path:
    output_path = build_output_path(dbf_path, output_dir)
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output file already exists: {output_path}. Use --overwrite to replace it."
        )

    table = _open_dbf(dbf_path, encoding)
    field_names = select_csv_field_names(list(table.field_names), max_fields)
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with temp_path.open(
            "w",
            newline="",
            encoding=csv_encoding,
            errors="strict",
        ) as handle:
            writer = csv.writer(handle, delimiter=delimiter)
            writer.writerow(field_names)
            batch: list[list[Any]] = []
            for record in table:
                batch.append([_csv_value(record.get(name)) for name in field_names])
                if len(batch) >= _CSV_BATCH_SIZE:
                    writer.writerows(batch)
                    batch.clear()
            if batch:
                writer.writerows(batch)
        temp_path.replace(output_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    return output_path


def render_progress(current: int, total: int) -> None:
    bar_width = 32
    filled = 0 if total == 0 else int(bar_width * current / total)
    bar = "#" * filled + "-" * (bar_width - filled)
    message = f"\rProgress: [{bar}] {current}/{total}"
    sys.stdout.write(message)
    sys.stdout.flush()


def resolve_scan_inputs(cli_inputs: list[str]) -> list[str]:
    if cli_inputs:
        return cli_inputs
    return [str(DEFAULT_DBF_INPUT_DIR)]


def main() -> int:
    args = parse_args()
    scan_inputs = resolve_scan_inputs(args.inputs)
    dbf_files = resolve_inputs(scan_inputs, args.recursive)
    total_files = len(dbf_files)

    print(f"Scanned files are: {total_files}")
    render_progress(0, total_files)

    converted_count = 0
    for dbf_file in dbf_files:
        convert_dbf_to_csv(
            dbf_path=dbf_file,
            output_dir=args.output_dir,
            encoding=args.encoding,
            csv_encoding=args.csv_encoding,
            delimiter=args.delimiter,
            overwrite=args.overwrite,
            max_fields=args.max_fields,
        )
        converted_count += 1
        render_progress(converted_count, total_files)

    if total_files > 0:
        render_progress(converted_count, total_files)
        print()
    print(f"Finished converting {converted_count} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
