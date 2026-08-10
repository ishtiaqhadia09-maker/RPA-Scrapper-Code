"""
IQVIA portal bot.

1. Open Chromium and load ``auth/iqvia_auth.json`` into the browser context (if present)
2. Navigate to the IQVIA entry URL from ``APP_URL`` (default ``https://hub.bi.iqvia.com/iam/``)
3. Check page title — login, save auth, open My Reports, etc.
4. Visible browser stays open until you close the window manually

Run from project root:

    python -m apps.scrapers.iqvia.iqvia_bot
"""

from __future__ import annotations

import argparse
import logging
import shutil
import time
from pathlib import Path

from apps.core.paths import DEFAULT_IQVIA_DOWNLOAD_DIR, ensure_data_dirs
from apps.core.utils.auth_utils import AuthSessionManager
from apps.core.utils.read_utils import ReadConfig
from apps.scrapers.iqvia.automation_guard import AutomationGuard
from apps.scrapers.iqvia.config import DOWNLOAD_TIMEOUT_MS
from apps.scrapers.iqvia.browser_session import open_browser_with_auth
from apps.scrapers.iqvia.xlsx_postprocess import (
    combined_csv_path_for,
    compress_for_delivery,
    postprocess_iqvia_xlsx,
    table1_csv_path_for,
    vtstack_csv_path_for,
    raw_csv_path_for,
)
from apps.scrapers.iqvia.page_locators import (
    CreateReportPage,
    DesignerPage,
    ExplorerPage,
    ExportCsvPage,
    HubPage,
    LoginPage,
    ReportPage,
)
from apps.scrapers.iqvia.report_sources import ReportSourceRow, load_report_sources
from apps.scrapers.iqvia.session_auth import (
    DECISION_CENTER_TITLE,
    INCOMPLETE_LOGIN_TITLES,
    IqviaSessionAuthMixin,
    LOGIN_TITLE,
)

logger = logging.getLogger(__name__)

SCRAPER_NAME = "iqvia"
DEFAULT_ENTRY_URL = "https://hub.bi.iqvia.com/iam/"
MIN_XLSX_BYTES = 50_000
MAX_PRODUCT_ATTEMPTS = 3
EXPORT_ALL_SHEETS_LABEL = "All"
EXCEL_EXTENSIONS = {".xlsx", ".xls"}


class IqviaBot(IqviaSessionAuthMixin):
    _scraper_name = SCRAPER_NAME

    def __init__(
        self,
        headless: bool = False,
        download_dir: Path | None = None,
        report_limit: int | None = None,
        start_row: int = 1,
        only_row: int | None = None,
        zip_for_delivery: bool = False,
    ) -> None:
        ensure_data_dirs()
        ReadConfig.reload()
        app_url = ReadConfig.getAppURL().strip()
        self.entry_url = app_url or DEFAULT_ENTRY_URL
        self.username = ReadConfig.getUsername()
        self.password = ReadConfig.getPassword()
        self.headless = headless
        self.auth = AuthSessionManager()
        self.download_dir = download_dir or DEFAULT_IQVIA_DOWNLOAD_DIR
        self.report_limit = report_limit
        self.start_row = max(1, start_row)
        # Explicit arg wins; otherwise use IQVIA_ROW from .env (blank = all rows).
        self.only_row = only_row if only_row is not None else ReadConfig.getReportRow()
        self.zip_for_delivery = zip_for_delivery
        self.saved_download_path: Path | None = None
        self.saved_download_paths: list[Path] = []

        logger.info("IQVIA entry URL (used after browser opens): %s", self.entry_url)
        if self.username.strip():
            masked = self.username.split("@")[0][:2] + "***@" + self.username.split("@")[-1]
            logger.info("IQVIA user loaded from .env: %s", masked)
        else:
            logger.warning("IQVIA_USER is empty — login will fail")
        if self.only_row is not None:
            logger.info("IQVIA_ROW=%d — will process only that product-list row", self.only_row)

        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.guard: AutomationGuard | None = None
        self._uses_chrome_profile = False
        self._persistent_context = False
        self._cdp_connected = False

    def _start_automation_guard(self) -> None:
        if self.context is None:
            return
        self.guard = AutomationGuard(self.context, headless=self.headless)
        # Guard is enabled only after designer steps finish (see _open_my_reports).

    def _refresh_automation_guard(self) -> None:
        if self.guard:
            self.guard.refresh()

    def run(self, keep_open: bool = False) -> None:
        """Run login (if needed), open My Reports, optionally wait for browser close."""
        self._keep_open = keep_open
        try:
            self._open_browser_with_auth()
            self._navigate_to_entry_url()
            self._handle_page_after_navigation()
        except Exception:
            logger.exception("Something went wrong — check the browser window")
            raise

    def _open_browser_with_auth(self) -> None:
        """Step 1 — launch Chrome profile / saved session before navigation."""
        self.download_dir.mkdir(parents=True, exist_ok=True)
        open_browser_with_auth(self)

    def _configure_browser_downloads(self, page=None) -> None:
        """Point Chromium downloads at our iqvia folder (CDP + polling fallback)."""
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if self.context is None:
            return
        target_page = page or self.page
        if target_page is None:
            return
        try:
            cdp = self.context.new_cdp_session(target_page)
            cdp.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(self.download_dir.resolve()),
                    "eventsEnabled": True,
                },
            )
            logger.info("Browser download path set to %s", self.download_dir)
        except Exception as exc:
            logger.warning("Could not set browser download path via CDP: %s", exc)

    def _navigate_to_entry_url(self) -> None:
        """Step 2 — navigate to IQVIA entry URL after browser + auth are loaded."""
        if self.page is None:
            raise RuntimeError("Browser not initialized")

        page = self.page
        logger.info("Step 2 — navigating to %s", self.entry_url)
        page.goto(
            self.entry_url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        page.wait_for_load_state("domcontentloaded")
        logger.info(
            "Step 2 complete — loaded %s (title: %r)",
            page.url,
            page.title(),
        )
        if page.title() == LOGIN_TITLE:
            LoginPage(page).wait_for_login_form()
        self._refresh_automation_guard()

    def _handle_page_after_navigation(self) -> None:
        """Step 3 — login, save auth, Decision Center, My Reports."""
        if self.page is None or self.context is None:
            raise RuntimeError("Browser not initialized")

        page = self.page
        title = page.title()
        logger.info("Step 3 — checking page after navigation (title: %r)", title)

        if title != DECISION_CENTER_TITLE:
            self._ensure_authenticated()
            title = page.title()
        elif self.auth.auth_file_exists(SCRAPER_NAME):
            logger.info(
                "Reusing saved auth session (%s)",
                self.auth.get_auth_path(SCRAPER_NAME),
            )
            self._persist_auth_session()

        if title == DECISION_CENTER_TITLE:
            self._automation_lock()
            self._open_my_reports()
        else:
            logger.warning(
                "Expected title %r to open My Reports; got %r",
                DECISION_CENTER_TITLE,
                title,
            )

    def _perform_login(self, login: LoginPage) -> None:
        if not self.username.strip() or not self.password.strip():
            raise RuntimeError(
                "IQVIA credentials not configured. "
                "Set IQVIA_USER and IQVIA_PASS in the project .env file."
            )
        if self.guard:
            self.guard.disable()
        try:
            login.sign_in(
                self.username,
                self.password,
                otp=ReadConfig.getOtp(),
                allow_manual_otp=True,
                guard=self.guard,
            )
        finally:
            if self.guard:
                self.guard.refresh()

    def _validate_and_postprocess_xlsx(self, xlsx_path: Path, product_name: str) -> Path:
        size = xlsx_path.stat().st_size
        if size < MIN_XLSX_BYTES:
            raise RuntimeError(
                f"Downloaded Excel file is too small ({size} bytes) — likely empty or "
                f"summary-only export ({xlsx_path.name})."
            )
        return postprocess_iqvia_xlsx(
            xlsx_path,
            product=product_name.strip(),
            zip_for_delivery=self.zip_for_delivery,
            table1_only=True,
        )

    def _sanitize_filename_part(self, value: str) -> str:
        sanitized = value.strip()
        for char in '\\/:*?"<>|':
            sanitized = sanitized.replace(char, "_")
        return sanitized.strip().replace(" ", "_") or "export"

    def _product_xlsx_path(self, product_name: str) -> Path:
        """Full path for a product workbook, e.g. OVIDREL.xlsx."""
        product = self._sanitize_filename_part(product_name)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        return self.download_dir / f"{product}.xlsx"

    def _find_new_download_file(
        self, before: dict[str, float], *, min_bytes: int = MIN_XLSX_BYTES
    ) -> Path | None:
        search_dirs = [self.download_dir]
        user_downloads = Path.home() / "Downloads"
        if user_downloads.is_dir() and user_downloads != self.download_dir:
            search_dirs.append(user_downloads)

        candidates: list[Path] = []
        for folder in search_dirs:
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix not in EXCEL_EXTENSIONS and suffix != ".zip":
                    continue
                if path.stat().st_size < min_bytes:
                    continue
                before_key = f"{folder}::{path.name}"
                if before_key not in before or path.stat().st_mtime > before.get(
                    before_key, 0
                ):
                    candidates.append(path)
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _snapshot_download_dirs(self) -> dict[str, float]:
        snap: dict[str, float] = {}
        for folder in (self.download_dir, Path.home() / "Downloads"):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if path.is_file():
                    snap[f"{folder}::{path.name}"] = path.stat().st_mtime
        return snap

    def _arm_download_listeners(self, ctx) -> list:
        """Collect download events from export tabs (fallback if expect_event times out)."""
        captured: list = []

        def _on_download(download) -> None:
            logger.info("Download event: %r", download.suggested_filename)
            captured.append(download)

        def _on_new_page(new_page) -> None:
            logger.info("Export opened new page: %s", (new_page.url or "")[:120])
            new_page.on("download", _on_download)
            self._configure_browser_downloads(page=new_page)

        ctx.on("download", _on_download)
        ctx.on("page", _on_new_page)
        for existing in ctx.pages:
            existing.on("download", _on_download)
        return captured

    def _click_export_and_download(
        self, page, export_page: ExportCsvPage, product_name: str
    ) -> Path:
        logger.info(
            "Clicking Export for sheet %r…",
            export_page.export_sheet_label,
        )
        ctx = page.context
        captured = self._arm_download_listeners(ctx)
        before = self._snapshot_download_dirs()
        if self.guard:
            self.guard.disable()
        try:
            try:
                with ctx.expect_page(timeout=DOWNLOAD_TIMEOUT_MS) as new_page_info:
                    with ctx.expect_event("download", timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                        export_page.clickExport()
                new_page = new_page_info.value
                logger.info("Export new tab: %s", (new_page.url or "")[:120])
                return self._save_product_download(dl_info.value, product_name)
            except Exception as primary_exc:
                logger.warning(
                    "Primary export capture failed (%s) — polling download folders",
                    primary_exc,
                )

            deadline = time.time() + DOWNLOAD_TIMEOUT_MS / 1000
            while time.time() < deadline:
                if captured:
                    return self._save_product_download(captured[0], product_name)

                found = self._find_new_download_file(before)
                if found:
                    logger.info("Download file appeared — %s", found)
                    if found.parent != self.download_dir:
                        dest = self.download_dir / found.name
                        shutil.copy2(found, dest)
                        found = dest
                    return self._finalize_product_download(found, product_name)

                page.wait_for_timeout(500)

            raise TimeoutError(
                f"No download received within {DOWNLOAD_TIMEOUT_MS // 1000}s "
                f"(checked {self.download_dir} and Downloads)"
            )
        finally:
            if self.guard:
                self.guard.refresh()

    def _export_report_xlsx(self, page, product_name: str) -> Path:
        product_name = product_name.strip()
        logger.info(
            "Export — product %r, wait for query, export %r (post-process → C+O+M xlsx + TABLE1 csv)…",
            product_name,
            EXPORT_ALL_SHEETS_LABEL,
        )
        designer = DesignerPage(page)
        designer.wait_for_query_complete()

        logger.info("Opening Excel export dialog…")
        designer.clickXlsExport()

        export_page = ExportCsvPage(page)
        export_page.export_sheet_label = EXPORT_ALL_SHEETS_LABEL
        export_page.wait_for_page_loaded()
        export_page.configure_excel_export_options()
        self._configure_browser_downloads()
        output_path = self._click_export_and_download(page, export_page, product_name)
        logger.info("Export complete — saved to %s", output_path)
        return output_path

    def _save_product_download(self, download, product_name: str) -> Path:
        suggested_name = download.suggested_filename or "export.xls"
        logger.info(
            "Waiting for browser download to finish (%s)…",
            suggested_name,
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        temp_destination = self.download_dir / suggested_name
        call_time = time.time()

        # Browser.setDownloadBehavior (CDP) makes Chrome save the file directly
        # to download_dir before Playwright can act.  Calling save_as() on a
        # CDP-handled download immediately overwrites the real file with 0 bytes
        # (Playwright's internal artifact is empty because CDP bypassed it).
        #
        # Strategy: poll until the CDP file appears and its size has been stable
        # for several seconds (indicating the write is complete), then skip
        # save_as entirely.  Only fall back to save_as when CDP never writes the
        # file within the allowed window.
        deadline = time.time() + DOWNLOAD_TIMEOUT_MS / 1000
        last_sz = -1
        stable_ticks = 0
        STABLE_NEEDED = 3   # 3 × 2 s of unchanging non-zero size = done
        POLL_SLEEP = 2.0

        while time.time() < deadline:
            if temp_destination.is_file():
                stat = temp_destination.stat()
                sz = stat.st_size
                # Only accept files written by the current download (mtime within
                # 5 min before this call) to avoid picking up stale leftovers.
                if sz > 0 and stat.st_mtime >= call_time - 300:
                    if sz == last_sz:
                        stable_ticks += 1
                        if stable_ticks >= STABLE_NEEDED:
                            logger.info(
                                "CDP download stable: %s (%d bytes)",
                                suggested_name,
                                sz,
                            )
                            break
                    else:
                        last_sz = sz
                        stable_ticks = 0
                        logger.info(
                            "CDP download in progress: %s (%d bytes so far)",
                            suggested_name,
                            sz,
                        )
            time.sleep(POLL_SLEEP)

        if temp_destination.is_file() and temp_destination.stat().st_size > 0:
            logger.info(
                "Download saved — %s (%d bytes). Running post-process…",
                temp_destination.name,
                temp_destination.stat().st_size,
            )
            return self._finalize_product_download(temp_destination, product_name)

        # CDP file never appeared — fall back to Playwright's save_as
        logger.warning(
            "CDP file not found after polling; falling back to save_as for %s",
            suggested_name,
        )
        before = self._snapshot_download_dirs()
        try:
            download.save_as(temp_destination)
        except Exception as exc:
            # Playwright's artifact may be canceled when the tab closes / CDP
            # handles it.  Poll download folders for the freshly created file.
            logger.warning("Download save_as failed (%s) — polling folders", exc)
            found = self._find_new_download_file(before)
            if found:
                if found.parent != self.download_dir:
                    dest = self.download_dir / found.name
                    shutil.copy2(found, dest)
                    found = dest
                temp_destination = found
            else:
                raise

        logger.info(
            "Download saved — %s (%d bytes). Running post-process…",
            temp_destination.name,
            temp_destination.stat().st_size,
        )
        return self._finalize_product_download(temp_destination, product_name)

    def _finalize_product_download(
        self, temp_destination: Path, product_name: str
    ) -> Path:
        suffix = temp_destination.suffix.lower()
        final_xlsx = self._product_xlsx_path(product_name)

        if temp_destination.stat().st_size == 0:
            logger.error("Downloaded file is empty (0 bytes): %s", temp_destination)
            self.saved_download_path = temp_destination.resolve()
            return temp_destination

        if suffix not in EXCEL_EXTENSIONS and suffix != ".zip":
            raise RuntimeError(f"Unexpected download type: {temp_destination.name}")

        raw_stem = temp_destination.stem
        raw_name = temp_destination.name
        if temp_destination != final_xlsx:
            if final_xlsx.exists():
                final_xlsx.unlink()
            shutil.move(temp_destination, final_xlsx)
            logger.info("Renamed IQVIA export %r → %s", raw_name, final_xlsx.name)

        processed = self._validate_and_postprocess_xlsx(
            final_xlsx,
            product_name,
        )

        final_csv = table1_csv_path_for(final_xlsx, product=product_name)
        src_csv = final_xlsx.parent / f"{raw_stem}.csv"
        if src_csv.is_file() and src_csv != final_csv:
            if final_csv.exists():
                final_csv.unlink()
            shutil.move(src_csv, final_csv)
        elif processed.suffix.lower() == ".csv" and processed.is_file():
            if processed != final_csv:
                if final_csv.exists():
                    final_csv.unlink()
                shutil.move(processed, final_csv)

        combined_csv = combined_csv_path_for(final_xlsx)
        if combined_csv.is_file():
            combined_csv.unlink()
        for sidecar in (
            vtstack_csv_path_for(final_xlsx),
            raw_csv_path_for(final_xlsx),
        ):
            if sidecar.is_file():
                sidecar.unlink()

        if processed.suffix.lower() == ".zip":
            final_zip = final_xlsx.with_suffix(".zip")
            if processed != final_zip:
                if final_zip.exists():
                    final_zip.unlink()
                shutil.move(processed, final_zip)
            final_path = final_zip
        elif final_csv.is_file():
            final_path = (
                compress_for_delivery(final_csv)
                if self.zip_for_delivery
                else final_csv
            )
            if not self.zip_for_delivery:
                logger.info(
                    "Plain deliverables (no zip): %s + %s",
                    final_xlsx.name,
                    final_csv.name,
                )
        else:
            final_path = final_xlsx

        size_mb = final_path.stat().st_size / 1024 / 1024
        self.saved_download_path = final_path.resolve()
        logger.info("Delivery saved to %s (%.1f MB)", final_path, size_mb)
        return final_path

    def _automation_lock(self) -> None:
        """Block user clicks while the bot drives the designer."""
        if self.guard:
            self.guard.enable()
            self.guard.refresh()

    def _run_step(self, action):
        """Run one designer step while the input guard stays active."""
        self._automation_lock()
        try:
            if self.guard:
                with self.guard.bypass():
                    return action()
            return action()
        finally:
            if self.guard:
                self.guard.refresh()

    def _close_stale_designer_tabs(self) -> None:
        """Close leftover designer tabs so the next row starts a fresh report."""
        if self.context is None or self.page is None:
            return

        hub_page = self.page
        closed = 0
        for tab in list(self.context.pages):
            if tab is hub_page:
                continue
            is_designer = any(
                "dashboard/designer.aspx" in (frame.url or "")
                for frame in tab.frames
            )
            if not is_designer:
                continue
            logger.info("Closing stale designer tab: %s", tab.url[:100])
            try:
                tab.close()
                closed += 1
            except Exception as exc:
                logger.warning("Could not close designer tab: %s", exc)

        if closed:
            logger.info("Closed %d stale designer tab(s)", closed)
            self.page = hub_page

    def _process_report_source(
        self,
        page,
        create_report: CreateReportPage,
        source_row: ReportSourceRow,
        *,
        row_number: int,
        total_rows: int,
    ) -> Path:
        """Run steps 3–17 for one report_sources.tsv row."""
        product = source_row.product.strip()
        market = source_row.market.strip()
        logger.info(
            "Row %d/%d — Data Source %s, Cube No. %s, Market %r, Product %r",
            row_number,
            total_rows,
            source_row.data_source,
            source_row.database_catalog,
            market,
            product,
        )
        self._automation_lock()
        self._run_step(
            lambda: create_report.select_data_source_and_cube(
                source_row.data_source,
                source_row.database_catalog,
            )
        )
        logger.info("Step 3 — Add, expand Period…")
        self._run_step(create_report.add_and_click_period)
        logger.info("Step 4 — Hierarchies chevron, drag Relative MAT…")
        self._run_step(create_report.drag_relative_mat_to_columns)
        logger.info("Step 5 — expand Sales Data folder…")
        self._run_step(create_report.expand_sales_data)
        logger.info("Step 6 — drag Units and Values to measures…")
        self._run_step(create_report.drag_units_and_values_to_measures)
        logger.info("Step 7 — expand Market…")
        self._run_step(create_report.expand_market)
        logger.info("Step 8 — expand Attributes, drag Market to filter…")
        self._run_step(create_report.drag_market_attribute_to_filter)
        logger.info(
            "Step 9 — set Market filter from file: %r",
            market,
        )
        self._run_step(lambda: create_report.apply_market_filter(market))
        logger.info("Step 10 — expand Product, drag Product to row (outermost)…")
        self._run_step(create_report.drag_product_attribute_to_row)
        logger.info("Step 11 — drag Pack onto Product row…")
        self._run_step(create_report.drag_pack_attribute_to_product_row)
        logger.info("Step 12 — drag Brick onto Pack row…")
        self._run_step(create_report.drag_brick_attribute_to_pack_row)
        logger.info(
            "Step 13 — Product Show Only the Top (Custom, Values), applied last…"
        )
        self._run_step(create_report.apply_product_show_only_top_custom)
        logger.info(
            "Step 13b — PivotTable → Analyze → Display Totals → Hide Totals…"
        )
        self._run_step(create_report.configure_display_totals_off)
        logger.info(
            "Step 13c — expand MAT columns (+), then Product/Pack Expand Members…"
        )
        self._run_step(create_report.expand_pivot_mat_columns)
        logger.info("Step 14 — rename active sheet to C-<product>…")
        sheet_c = self._run_step(lambda: create_report.build_sheet_c(product))
        logger.info(
            "Step 15 — copy C → rename O-<product> → filter Product (Begins with)…"
        )
        self._run_step(
            lambda: create_report.build_sheet_o(product, source_sheet=sheet_c)
        )
        logger.info(
            "Step 16 — copy C → New Sheet → M, remove Product/Pack, move Market to row…"
        )
        self._run_step(
            lambda: create_report.build_sheet_m(product, source_sheet=sheet_c)
        )
        logger.info(
            "Step 16b — pre-export check: expand collapsed columns/rows on C, O, M…"
        )
        logger.info(
            "Step 16b skipped — user confirmed pivots already expanded"
        )
        logger.info("Step 17 — export C/O/M, deliver C+O+M xlsx + TABLE1 csv…")
        return self._run_step(
            lambda: self._export_report_xlsx(
                create_report.active_page(),
                product,
            )
        )

    def _open_my_reports(self) -> None:
        if self.page is None:
            raise RuntimeError("Browser page not initialized")

        page = self.page
        logger.info("Opening My Reports on Decision Center...")
        HubPage(page).clickMyReports()
        self._refresh_automation_guard()

        explorer = ExplorerPage(page)
        logger.info("Waiting for My Reports explorer page to finish loading...")
        explorer.wait_for_page_loaded()
        self._refresh_automation_guard()

        report_sources = load_report_sources()
        file_row_count = len(report_sources)

        if self.only_row is not None:
            if self.only_row > file_row_count:
                raise ValueError(
                    f"IQVIA_ROW={self.only_row} is out of range — "
                    f"product list has {file_row_count} row(s)"
                )
            source_row = report_sources[self.only_row - 1]
            report_sources = [source_row]
            self.start_row = 1
            logger.info(
                "IQVIA_ROW=%d — only product %r (of %d in file)",
                self.only_row,
                source_row.product,
                file_row_count,
            )
        elif self.report_limit is not None and self.report_limit > 0:
            report_sources = report_sources[: self.report_limit]

        total_rows = len(report_sources)
        if self.only_row is None and self.start_row > 1:
            logger.info(
                "Resuming from row %d/%d (%r)",
                self.start_row,
                total_rows,
                report_sources[self.start_row - 1].product,
            )
        logger.info(
            "Processing %d product row(s) from report_sources.tsv",
            total_rows if self.only_row is None else 1,
        )

        self._automation_lock()

        succeeded: list[Path] = []
        failed: list[tuple[ReportSourceRow, Exception]] = []
        try:
            for row_number, source_row in enumerate(report_sources, start=1):
                if row_number < self.start_row:
                    continue
                display_row = self.only_row if self.only_row is not None else row_number
                display_total = file_row_count if self.only_row is not None else total_rows
                logger.info(
                    "=== Product %d/%d: %r ===",
                    display_row,
                    display_total,
                    source_row.product,
                )
                if row_number > 1:
                    self._close_stale_designer_tabs()
                    explorer.return_to_my_reports()

                last_exc: Exception | None = None
                for attempt in range(1, MAX_PRODUCT_ATTEMPTS + 1):
                    if attempt > 1:
                        logger.warning(
                            "Retry %d/%d for product %r",
                            attempt,
                            MAX_PRODUCT_ATTEMPTS,
                            source_row.product,
                        )
                        self._close_stale_designer_tabs()
                        explorer.return_to_my_reports()

                    logger.info(
                        "My Reports — Create Report on Report Files tile "
                        "(row %d/%d, attempt %d/%d)…",
                        display_row,
                        display_total,
                        attempt,
                        MAX_PRODUCT_ATTEMPTS,
                    )
                    explorer.clickCreateReport()
                    create_report = CreateReportPage(page)
                    create_report.reset_session_state()
                    logger.info("Waiting for report designer to open…")
                    create_report.wait_for_loaded()
                    if self.guard:
                        self.guard.refresh()
                    try:
                        xlsx_path = self._process_report_source(
                            page,
                            create_report,
                            source_row,
                            row_number=display_row,
                            total_rows=display_total,
                        )
                        succeeded.append(xlsx_path)
                        self.saved_download_paths.append(xlsx_path)
                        self.saved_download_path = xlsx_path.resolve()
                        logger.info(
                            "Row %d/%d complete — saved %s",
                            display_row,
                            display_total,
                            xlsx_path.name,
                        )
                        break
                    except Exception as exc:
                        last_exc = exc
                        logger.exception(
                            "Attempt %d/%d failed for product %r",
                            attempt,
                            MAX_PRODUCT_ATTEMPTS,
                            source_row.product,
                        )
                else:
                    failed.append((source_row, last_exc or RuntimeError("unknown error")))
        finally:
            if self.guard and getattr(self, "_keep_open", False):
                self.guard.disable()
                self.guard.refresh()
        self._refresh_automation_guard()

        logger.info(
            "Batch complete — %d/%d succeeded, %d failed",
            len(succeeded),
            total_rows,
            len(failed),
        )
        if failed:
            names = ", ".join(row.product for row, _ in failed[:5])
            suffix = "…" if len(failed) > 5 else ""
            raise RuntimeError(
                f"{len(failed)} of {total_rows} product export(s) failed: "
                f"{names}{suffix}"
            )

        logger.info("All products processed.")
        if not self.headless and getattr(self, "_keep_open", False):
            logger.info("keep_open=True — close the browser window when done.")
            self.wait_until_browser_closed()

    def wait_until_browser_closed(self) -> None:
        """Block until the user closes the Chromium window."""
        if not self.browser or not self.browser.is_connected():
            return

        logger.info(
            "Selection saved — close the browser window when you are done."
        )
        while self.browser.is_connected():
            time.sleep(0.5)
        logger.info("Browser window closed by user")

    def _persist_auth_session(self) -> None:
        if self.context is None:
            return
        try:
            title = self.page.title() if self.page else ""
        except Exception:
            return
        if title in INCOMPLETE_LOGIN_TITLES:
            return
        self.auth.save_auth_from_context(self.context, SCRAPER_NAME)
        logger.info("Auth session saved to %s", self.auth.get_auth_path(SCRAPER_NAME))

    def close(self) -> None:
        self._teardown_browser(persist_auth=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Run IQVIA pivot export bot")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N rows from report_sources.tsv (0 = all rows)",
    )
    parser.add_argument(
        "--start-row",
        type=int,
        default=1,
        help="Resume from this 1-based row in report_sources.tsv (skip earlier rows)",
    )
    parser.add_argument(
        "--only-row",
        type=int,
        default=0,
        help="Process only this 1-based row (overrides IQVIA_ROW in .env when set)",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave the browser open after the batch until you close it manually",
    )
    args = parser.parse_args()
    report_limit = args.limit if args.limit > 0 else None
    only_row = args.only_row if args.only_row > 0 else None

    bot = IqviaBot(
        headless=False,
        report_limit=report_limit,
        start_row=max(1, args.start_row),
        only_row=only_row,
    )
    try:
        bot.run(keep_open=args.keep_open)
    finally:
        bot.close()
