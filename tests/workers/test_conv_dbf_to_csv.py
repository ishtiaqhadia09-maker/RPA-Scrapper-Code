"""Tests for DBF → CSV conversion (text rewrite, not extension rename)."""

from __future__ import annotations

import struct
from pathlib import Path

from apps.workers.conv_dbf_to_csv import convert_dbf_to_csv, select_csv_field_names


def _write_minimal_dbf(path: Path) -> None:
    fields = [
        ("NAME", "C", 10, 0),
        ("QTY", "N", 5, 0),
    ]
    records = [
        b" " + b"Aspirin   " + b"   12",
        b" " + b"Ibuprofen " + b"    3",
    ]
    header_len = 32 + 32 * len(fields) + 1
    rec_len = 1 + sum(length for _, _, length, _ in fields)
    header = bytearray(32)
    header[0] = 0x03
    header[1:4] = bytes((26, 8, 18))
    struct.pack_into("<I", header, 4, len(records))
    struct.pack_into("<H", header, 8, header_len)
    struct.pack_into("<H", header, 10, rec_len)

    payload = bytearray(header)
    for name, typ, length, decimal in fields:
        desc = bytearray(32)
        encoded = name.encode("ascii")
        desc[: len(encoded)] = encoded
        desc[11] = ord(typ)
        desc[16] = length
        desc[17] = decimal
        payload.extend(desc)
    payload.append(0x0D)
    payload.extend(b"".join(records))
    payload.append(0x1A)
    path.write_bytes(payload)


def test_convert_dbf_to_csv_writes_utf8_text(tmp_path: Path) -> None:
    dbf_path = tmp_path / "SAMPLE.dbf"
    _write_minimal_dbf(dbf_path)

    csv_path = convert_dbf_to_csv(
        dbf_path=dbf_path,
        output_dir=tmp_path / "out",
        encoding=None,
        csv_encoding="utf-8",
        delimiter=",",
        overwrite=False,
    )["output_path"]

    text = csv_path.read_text(encoding="utf-8")
    assert "\x00" not in text
    assert text.splitlines() == [
        "NAME,QTY",
        "Aspirin,12",
        "Ibuprofen,3",
    ]
    assert csv_path.read_bytes()[:1] != b"\x03"


def test_select_csv_field_names_matches_excel_dbf_limit() -> None:
    names = [f"F{i:03d}" for i in range(774)]
    assert len(select_csv_field_names(names)) == 255
    assert select_csv_field_names(names) == names[:255]
    assert select_csv_field_names(names, max_fields=0) == names
    assert select_csv_field_names(["A", "B"], max_fields=255) == ["A", "B"]


def _write_wide_dbf(path: Path, field_count: int) -> None:
    fields = [(f"F{i:03d}", "C", 5, 0) for i in range(field_count)]
    record_payload = b"".join(b"12345" for _ in fields)
    records = [b" " + record_payload]
    header_len = 32 + 32 * len(fields) + 1
    rec_len = 1 + sum(length for _, _, length, _ in fields)
    header = bytearray(32)
    header[0] = 0x03
    header[1:4] = bytes((26, 8, 18))
    struct.pack_into("<I", header, 4, len(records))
    struct.pack_into("<H", header, 8, header_len)
    struct.pack_into("<H", header, 10, rec_len)

    payload = bytearray(header)
    for name, typ, length, decimal in fields:
        desc = bytearray(32)
        encoded = name.encode("ascii")
        desc[: len(encoded)] = encoded
        desc[11] = ord(typ)
        desc[16] = length
        desc[17] = decimal
        payload.extend(desc)
    payload.append(0x0D)
    payload.extend(records)
    payload.append(0x1A)
    path.write_bytes(payload)


def test_convert_dbf_to_csv_caps_columns_at_excel_limit(tmp_path: Path) -> None:
    dbf_path = tmp_path / "WIDE.dbf"
    _write_wide_dbf(dbf_path, field_count=300)

    result = convert_dbf_to_csv(
        dbf_path=dbf_path,
        output_dir=tmp_path / "out",
        encoding=None,
        csv_encoding="utf-8",
        delimiter=",",
        overwrite=False,
    )

    assert result["source_field_count"] == 300
    assert result["csv_field_count"] == 255
    header = result["output_path"].read_text(encoding="utf-8").splitlines()[0]
    assert len(header.split(",")) == 255


def test_convert_dbf_to_csv_can_limit_columns(tmp_path: Path) -> None:
    dbf_path = tmp_path / "SAMPLE.dbf"
    _write_minimal_dbf(dbf_path)

    result = convert_dbf_to_csv(
        dbf_path=dbf_path,
        output_dir=tmp_path / "out",
        encoding=None,
        csv_encoding="utf-8",
        delimiter=",",
        overwrite=False,
        max_fields=1,
    )
    csv_path = result["output_path"]
    assert result["csv_field_count"] == 1

    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "NAME",
        "Aspirin",
        "Ibuprofen",
    ]
