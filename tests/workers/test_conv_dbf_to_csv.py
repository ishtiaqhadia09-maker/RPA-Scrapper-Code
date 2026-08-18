"""Tests for DBF → CSV conversion (text rewrite, not extension rename)."""

from __future__ import annotations

import struct
from pathlib import Path

from apps.workers.conv_dbf_to_csv import convert_dbf_to_csv


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
    )

    text = csv_path.read_text(encoding="utf-8")
    assert "\x00" not in text
    assert text.splitlines() == [
        "NAME,QTY",
        "Aspirin,12",
        "Ibuprofen,3",
    ]
    assert csv_path.read_bytes()[:1] != b"\x03"
