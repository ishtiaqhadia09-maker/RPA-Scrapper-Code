"""Load IQVIA report source rows from the active or default TSV file."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from apps.core.paths import (
    ACTIVE_REPORT_SOURCES_META_PATH,
    ACTIVE_REPORT_SOURCES_PATH,
    DEFAULT_REPORT_SOURCES_PATH,
    IQVIA_DATA_DIR,
)

# IQVIA "Database/Catalog" dropdown value prefix (Cube No. column in the TSV).
DATABASE_CATALOG_PREFIX = "DDD_PK_M_MERCK_"
REQUIRED_COLUMNS = ("Data Source", "Cube No.", "MARKET", "PRODUCT")
SUPPORTED_UPLOAD_EXTENSIONS = {".tsv", ".txt", ".csv", ".xlsx", ".xls"}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportSourceRow:
    data_source: str
    cube_no: str
    market: str
    product: str

    @property
    def database_catalog(self) -> str:
        """Same as cube_no — maps to the Database/Catalog dropdown in IQVIA."""
        return self.cube_no


def normalize_database_catalog(value: str) -> str:
    """Ensure Cube No. values match IQVIA Database/Catalog dropdown format."""
    value = value.strip()
    if not value:
        return value
    if value.startswith(DATABASE_CATALOG_PREFIX):
        return value
    if value.startswith("DDD_PK_MERCK_"):
        return DATABASE_CATALOG_PREFIX + value.removeprefix("DDD_PK_MERCK_")
    match = re.search(r"(\d{10,})$", value)
    if match:
        return DATABASE_CATALOG_PREFIX + match.group(1)
    return value


def _cell_text(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_upload_dataframe(content: bytes, *, original_filename: str) -> pd.DataFrame:
    suffix = Path(original_filename).suffix.lower()
    if suffix not in SUPPORTED_UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_UPLOAD_EXTENSIONS))
        raise ValueError(
            f"Unsupported file type {suffix!r}. Upload one of: {allowed}."
        )

    buffer = io.BytesIO(content)
    if suffix in {".tsv", ".txt"}:
        df = pd.read_csv(buffer, sep="\t", dtype=str, encoding="utf-8-sig")
    elif suffix == ".csv":
        df = pd.read_csv(
            buffer,
            sep=None,
            engine="python",
            dtype=str,
            encoding="utf-8-sig",
        )
    else:
        df = pd.read_excel(buffer, dtype=str)

    if df.empty:
        raise ValueError(f"No rows found in {original_filename}")

    df.columns = [str(col).strip() for col in df.columns]
    for column in df.columns:
        df[column] = df[column].map(_cell_text)
    df = df.replace("", pd.NA).dropna(how="all")
    if df.empty:
        raise ValueError(f"No data rows found in {original_filename}")
    return df


def coerce_upload_to_tsv_bytes(content: bytes, *, original_filename: str) -> bytes:
    """Normalize .tsv / .csv / .xlsx uploads into tab-separated bytes."""
    df = _read_upload_dataframe(content, original_filename=original_filename)
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required column(s) in {original_filename}: "
            f"{', '.join(missing)}. Expected columns: {', '.join(REQUIRED_COLUMNS)}"
        )

    normalized = df[list(REQUIRED_COLUMNS)].copy()
    tsv_buffer = io.StringIO()
    normalized.to_csv(tsv_buffer, sep="\t", index=False, lineterminator="\n")
    return tsv_buffer.getvalue().encode("utf-8")


def ensure_active_report_sources() -> Path:
    """Return the saved active product list, bootstrapping from default TSV once."""
    IQVIA_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if ACTIVE_REPORT_SOURCES_PATH.is_file():
        return ACTIVE_REPORT_SOURCES_PATH

    if DEFAULT_REPORT_SOURCES_PATH.is_file():
        rows = load_report_sources(DEFAULT_REPORT_SOURCES_PATH)
        ACTIVE_REPORT_SOURCES_PATH.write_bytes(
            DEFAULT_REPORT_SOURCES_PATH.read_bytes()
        )
        stat = DEFAULT_REPORT_SOURCES_PATH.stat()
        meta = {
            "original_filename": DEFAULT_REPORT_SOURCES_PATH.name,
            "original_format": "tsv",
            "uploaded_at": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            "row_count": len(rows),
            "bootstrapped_from_default": True,
        }
        ACTIVE_REPORT_SOURCES_META_PATH.write_text(
            json.dumps(meta, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "Saved %s as active product list (%d row(s)) — "
            "will be used until a new file is uploaded",
            DEFAULT_REPORT_SOURCES_PATH.name,
            len(rows),
        )
        return ACTIVE_REPORT_SOURCES_PATH

    return DEFAULT_REPORT_SOURCES_PATH


def resolve_report_sources_path() -> Path:
    """Saved active file wins; bootstrap default TSV once if needed."""
    return ensure_active_report_sources()


def get_report_sources_info() -> dict[str, Any]:
    """Summary for UI: which file is active and how many rows it contains."""
    path = resolve_report_sources_path()
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "source": "saved" if path == ACTIVE_REPORT_SOURCES_PATH else "missing",
        "row_count": 0,
        "original_filename": None,
        "uploaded_at": None,
        "modified_at": None,
        "bootstrapped_from_default": False,
    }
    if not path.is_file():
        return info

    stat = path.stat()
    info["modified_at"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    try:
        info["row_count"] = len(load_report_sources(path))
    except (OSError, ValueError):
        info["row_count"] = None

    if ACTIVE_REPORT_SOURCES_META_PATH.is_file():
        try:
            meta = json.loads(ACTIVE_REPORT_SOURCES_META_PATH.read_text(encoding="utf-8"))
            info["original_filename"] = meta.get("original_filename")
            info["uploaded_at"] = meta.get("uploaded_at")
            info["bootstrapped_from_default"] = bool(
                meta.get("bootstrapped_from_default")
            )
        except (OSError, json.JSONDecodeError):
            pass
    return info


def validate_report_sources_bytes(
    content: bytes,
    *,
    original_filename: str = "upload.tsv",
) -> list[ReportSourceRow]:
    """Parse and validate an upload without saving it."""
    tsv_bytes = coerce_upload_to_tsv_bytes(content, original_filename=original_filename)
    IQVIA_DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = IQVIA_DATA_DIR / "_upload_validate.tmp.tsv"
    temp_path.write_bytes(tsv_bytes)
    try:
        return load_report_sources(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)


def save_report_sources_upload(content: bytes, *, original_filename: str) -> Path:
    """Persist a validated upload as TSV; used on every run until replaced."""
    tsv_bytes = coerce_upload_to_tsv_bytes(content, original_filename=original_filename)
    rows = validate_report_sources_bytes(
        tsv_bytes,
        original_filename="converted.tsv",
    )
    IQVIA_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_REPORT_SOURCES_PATH.write_bytes(tsv_bytes)
    meta = {
        "original_filename": original_filename,
        "original_format": Path(original_filename).suffix.lower().lstrip("."),
        "uploaded_at": datetime.now(tz=timezone.utc).isoformat(),
        "row_count": len(rows),
        "bootstrapped_from_default": False,
    }
    ACTIVE_REPORT_SOURCES_META_PATH.write_text(
        json.dumps(meta, indent=2),
        encoding="utf-8",
    )
    return ACTIVE_REPORT_SOURCES_PATH


def load_report_sources(path: Path | str | None = None) -> list[ReportSourceRow]:
    source_path = Path(path) if path else resolve_report_sources_path()
    if not source_path.is_file():
        raise FileNotFoundError(
            f"Report sources file not found: {source_path}. "
            "Upload a product list from the Download page or add "
            f"{DEFAULT_REPORT_SOURCES_PATH.name} under data/iqvia/."
        )

    rows: list[ReportSourceRow] = []
    with source_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fieldnames = reader.fieldnames or []
        missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
        if missing:
            raise ValueError(
                f"Missing required column(s) in {source_path.name}: "
                f"{', '.join(missing)}. Expected tab-separated columns: "
                f"{', '.join(REQUIRED_COLUMNS)}"
            )
        for record in reader:
            rows.append(
                ReportSourceRow(
                    data_source=(record.get("Data Source") or "").strip(),
                    cube_no=normalize_database_catalog(
                        record.get("Cube No.") or ""
                    ),
                    market=(record.get("MARKET") or "").strip(),
                    product=(record.get("PRODUCT") or "").strip(),
                )
            )
    if not rows:
        raise ValueError(f"No rows found in {source_path}")
    return rows


def first_report_source(path: Path | str | None = None) -> ReportSourceRow:
    return load_report_sources(path)[0]


def find_report_source(
    data_source: str,
    cube_no: str,
    path: Path | str | None = None,
) -> ReportSourceRow:
    """First TSV row matching Data Source + Cube No. (database/catalog)."""
    normalized_cube = normalize_database_catalog(cube_no)
    for row in load_report_sources(path):
        if row.data_source == data_source.strip() and row.cube_no == normalized_cube:
            return row
    raise ValueError(
        f"No report source row for data source {data_source!r} "
        f"and cube/catalog {normalized_cube!r}"
    )
