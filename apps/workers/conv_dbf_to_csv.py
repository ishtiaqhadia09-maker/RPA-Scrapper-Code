# pyright: reportMissingImports=false
"""Standalone DBF to CSV converter using dbfread and pandas.

When no input paths are given on the command line, DBF files are scanned from
``DBF_INPUT_DIR`` in the project ``.env`` (default: ``data/raw/DBF_FILES``).

Examples:
    python3 apps/workers/conv_dbf_to_csv.py
    python3 apps/workers/conv_dbf_to_csv.py data/raw/DBF_FILES/COLAR.dbf
    python3 apps/workers/conv_dbf_to_csv.py data/raw/DBF_FILES --recursive
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

try:
    import pandas as pd
    from dbfread import DBF
except ImportError as exc:  # pragma: no cover - depends on local environment
    missing_package = exc.name or "required package"
    raise SystemExit(
        "Missing dependency: "
        f"{missing_package}. Install with `pip install pandas dbfread`."
    ) from exc

from apps.core.paths import DEFAULT_DBF_INPUT_DIR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert DBF files into CSV.")
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


def _iter_all_dbf_records(table: DBF):
    """Yield every record counted in the DBF header.

    dbfread's default iterator only keeps rows whose deletion flag is a
    space and stops at the first ``0x1A`` byte. Many real files use ``\\x00``
    for active rows, include deleted rows in ``numrecords``, or have an
    early EOF marker — all of which silently drop records from the CSV.
    """
    header = table.header
    record_len = header.recordlen
    num_records = header.numrecords

    with open(table.filename, "rb") as infile, table._open_memofile() as memofile:
        parse = table.parserclass(table, memofile).parse
        infile.seek(header.headerlen)

        remaining = num_records if num_records > 0 else None
        while remaining is None or remaining > 0:
            raw = infile.read(record_len)
            if len(raw) < record_len:
                break
            # When the header count is missing, a lone EOF marker ends the file.
            if remaining is None and raw[:1] == b"\x1a":
                break

            payload = raw[1:]
            offset = 0
            items = []
            for field in table.fields:
                length = field.length
                chunk = payload[offset : offset + length]
                offset += length
                if len(chunk) < length:
                    chunk = chunk + b"\x00" * (length - len(chunk))
                items.append((field.name, parse(field, chunk)))
            yield table.recfactory(items)
            if remaining is not None:
                remaining -= 1


def load_dbf_as_dataframe(dbf_path: Path, encoding: str | None) -> pd.DataFrame:
    table = DBF(
        str(dbf_path),
        load=False,
        encoding=encoding,
        ignore_missing_memofile=True,
        char_decode_errors="ignore",
    )
    records = list(_iter_all_dbf_records(table))
    if not records:
        return pd.DataFrame(columns=list(table.field_names))
    return pd.DataFrame.from_records(records, columns=list(table.field_names))


def convert_dbf_to_csv(
    dbf_path: Path,
    output_dir: Path | None,
    encoding: str | None,
    csv_encoding: str,
    delimiter: str,
    overwrite: bool,
) -> Path:
    output_path = build_output_path(dbf_path, output_dir)
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output file already exists: {output_path}. Use --overwrite to replace it."
        )

    dataframe = load_dbf_as_dataframe(dbf_path, encoding)
    dataframe.to_csv(
        output_path,
        index=False,
        encoding=csv_encoding,
        sep=delimiter,
    )
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
