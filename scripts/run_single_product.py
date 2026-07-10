"""Run IQVIA bot for one product row from report_sources.tsv.

Activate venv first (PowerShell):

    .\\venv\\Scripts\\Activate.ps1
    python scripts/run_single_product.py --product "CRESCOR EZE"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.scrapers.iqvia import iqvia_bot, report_sources


def _filter_rows(
    *,
    data_source: str | None,
    cube_no: str | None,
    product: str | None,
) -> list[report_sources.ReportSourceRow]:
    rows = report_sources.load_report_sources()
    matches = rows
    if data_source:
        matches = [r for r in matches if r.data_source == data_source.strip()]
    if cube_no:
        normalized = report_sources.normalize_database_catalog(cube_no)
        matches = [
            r
            for r in matches
            if r.cube_no == normalized or cube_no.strip() in r.cube_no
        ]
    if product:
        matches = [r for r in matches if r.product.strip() == product.strip()]
    return matches


def _require_venv() -> None:
    if sys.prefix == sys.base_prefix:
        print(
            "Activate the project venv before running:\n"
            "  .\\venv\\Scripts\\Activate.ps1",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> int:
    _require_venv()
    parser = argparse.ArgumentParser(description="Run IQVIA export for one product")
    parser.add_argument("--data-source", default=None)
    parser.add_argument("--cube-no", default=None)
    parser.add_argument("--product", default=None)
    parser.add_argument("--market", default=None, help="Override MARKET (comma-separated OK)")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Keep plain .xlsx and .csv (do not zip large CSV)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    matches = _filter_rows(
        data_source=args.data_source,
        cube_no=args.cube_no,
        product=args.product,
    )
    if not matches:
        print("No matching product row found in report_sources.tsv", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(
            f"Multiple rows matched ({len(matches)}); using the first: {matches[0].product!r}",
            file=sys.stderr,
        )

    row = matches[0]
    if args.market:
        row = report_sources.ReportSourceRow(
            data_source=row.data_source,
            cube_no=row.cube_no,
            market=args.market.strip(),
            product=row.product,
        )
    print(
        f"Running single product: {row.product!r} "
        f"(market={row.market!r}, cube={row.cube_no!r})",
        flush=True,
    )

    original_loader = report_sources.load_report_sources

    def _single_product_loader(path=None):
        return [row]

    report_sources.load_report_sources = _single_product_loader
    iqvia_bot.load_report_sources = _single_product_loader

    bot = iqvia_bot.IqviaBot(
        headless=False,
        report_limit=1,
        zip_for_delivery=not args.no_zip,
    )
    try:
        bot.run(keep_open=args.keep_open)
    finally:
        bot.close()

    if bot.saved_download_path is not None:
        print(f"DOWNLOAD_COMPLETE: {bot.saved_download_path}", flush=True)
        return 0

    print("DOWNLOAD_COMPLETE: no saved path recorded", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
