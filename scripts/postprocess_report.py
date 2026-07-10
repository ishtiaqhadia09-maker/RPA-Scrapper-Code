"""Post-process a downloaded IQVIA Excel export (VTSTACK + RAW FOR NEXT STAGE)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.scrapers.iqvia.xlsx_postprocess import (
    export_table1_csv_from_xlsx,
    postprocess_iqvia_xlsx,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add VTSTACK and RAW FOR NEXT STAGE to an IQVIA export"
    )
    parser.add_argument(
        "path",
        help=r'Path to export file, e.g. "data/downloads/iqvia/Report 49.xlsx"',
    )
    parser.add_argument("--product", default=None, help='Product name, e.g. "CRESCOR EZE"')
    parser.add_argument(
        "--attribute-only",
        action="store_true",
        help="Only export TABLE 1 CSV (RAW FOR NEXT STAGE must already exist)",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Keep plain .xlsx and .csv files (do not zip large CSV)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    path = Path(args.path)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    if args.attribute_only:
        out = export_table1_csv_from_xlsx(path)
        print(f"DONE: {out}")
        return 0

    if not args.product:
        print("--product is required unless --attribute-only is set", file=sys.stderr)
        return 1

    out = postprocess_iqvia_xlsx(
        path, product=args.product, zip_for_delivery=not args.no_zip
    )
    print(f"DONE: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
