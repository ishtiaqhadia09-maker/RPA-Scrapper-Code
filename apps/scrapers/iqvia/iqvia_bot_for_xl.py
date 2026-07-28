"""
IQVIA portal bot (XL variant entry point).

Same automation as ``iqvia_bot.py``, with CSV or XLS export from CRESCOR Test.

1. Open Chromium and load ``auth/iqvia_auth.json`` into the browser context (if present)
2. Navigate to the IQVIA entry URL from ``APP_URL`` (default ``https://hub.bi.iqvia.com/iam/``)
3. Check page title — login, save auth, open My Reports, etc.
4. Visible browser stays open until you close the window manually

Run from project root:

    python -m apps.scrapers.iqvia.iqvia_bot_for_xl
    python -m apps.scrapers.iqvia.iqvia_bot_for_xl --format xls
    python -m apps.scrapers.iqvia.iqvia_bot_for_xl --format csv
"""

from __future__ import annotations

import argparse
import logging
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from apps.core.paths import DEFAULT_IQVIA_DOWNLOAD_DIR, PROCESSED_RUN_DIR_FORMAT
from apps.core.utils.auth_utils import AuthSessionManager
from apps.core.utils.read_utils import ReadConfig
from apps.scrapers.iqvia.automation_guard import AutomationGuard
from apps.scrapers.iqvia.config import DOWNLOAD_TIMEOUT_MS
from apps.scrapers.iqvia.browser_session import open_browser_with_auth
from apps.scrapers.iqvia.page_locators import (
    CreateReportPage,
    DesignerPage,
    ExplorerPage,
    ExportCsvPage,
    HubPage,
    LoginPage,
    ReportPage,
)
from apps.scrapers.iqvia.report_sources import first_report_source
from apps.scrapers.iqvia.session_auth import (
    DECISION_CENTER_TITLE,
    INCOMPLETE_LOGIN_TITLES,
    IqviaSessionAuthMixin,
    LOGIN_TITLE,
)

logger = logging.getLogger(__name__)

SCRAPER_NAME = "iqvia"
DEFAULT_ENTRY_URL = "https://hub.bi.iqvia.com/iam/"
ExportFormat = Literal["csv", "xls"]
SUPPORTED_EXPORT_FORMATS: tuple[ExportFormat, ...] = ("csv", "xls")
DEFAULT_EXPORT_NAMES: dict[ExportFormat, str] = {
    "csv": "export.csv",
    "xls": "export.xls",
}
MIN_CSV_BYTES = 50_000


class IqviaBotForXl(IqviaSessionAuthMixin):
    _scraper_name = SCRAPER_NAME

    def __init__(
        self,
        headless: bool = False,
        download_dir: Path | None = None,
        export_format: ExportFormat = "csv",
    ) -> None:
        ReadConfig.reload()
        app_url = ReadConfig.getAppURL().strip()
        self.entry_url = app_url or DEFAULT_ENTRY_URL
        self.username = ReadConfig.getUsername()
        self.password = ReadConfig.getPassword()
        self.headless = headless
        self.auth = AuthSessionManager()
        self.download_dir = download_dir or DEFAULT_IQVIA_DOWNLOAD_DIR
        self.saved_download_path: Path | None = None
        normalized_format = export_format.lower().strip()
        if normalized_format not in SUPPORTED_EXPORT_FORMATS:
            supported = ", ".join(SUPPORTED_EXPORT_FORMATS)
            raise ValueError(
                f"Unsupported export format {export_format!r}; use one of: {supported}"
            )
        self.export_format = normalized_format

        logger.info("IQVIA entry URL (used after browser opens): %s", self.entry_url)
        logger.info("Export format: %s", self.export_format.upper())
        if self.username.strip():
            masked = self.username.split("@")[0][:2] + "***@" + self.username.split("@")[-1]
            logger.info("IQVIA user loaded from .env: %s", masked)
        else:
            logger.warning("IQVIA_USER is empty — login will fail")

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
        self.guard.enable()

    def _refresh_automation_guard(self) -> None:
        if self.guard:
            self.guard.refresh()

    def run(self, keep_open: bool = True) -> None:
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
        open_browser_with_auth(self)

    def _navigate_to_entry_url(self) -> None:
        """Step 2 — navigate to IQVIA entry URL after browser + auth are loaded."""
        if self.page is None:
            raise RuntimeError("Browser not initialized")

        page = self.page
        logger.info("Step 2 — navigating to %s", self.entry_url)
        page.goto(self.entry_url, wait_until="load")
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
        login.sign_in(
            self.username,
            self.password,
            otp=ReadConfig.getOtp(),
            allow_manual_otp=True,
            guard=self.guard,
        )
        self._refresh_automation_guard()

    def _sanitize_filename_part(self, value: str) -> str:
        sanitized = value.strip()
        for char in '\\/:*?"<>|':
            sanitized = sanitized.replace(char, "-")
        return sanitized.strip() or "export"

    def _product_output_dir(self, product_name: str) -> Path:
        stamp = datetime.now().strftime(PROCESSED_RUN_DIR_FORMAT)
        folder_name = f"{self._sanitize_filename_part(product_name)}-{stamp}"
        output_dir = self.download_dir / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _click_export_and_download(
        self, page, export_page: ExportCsvPage, product_name: str
    ) -> Path:
        """Click Export on the popup and wait for the file download."""
        logger.info("Clicking Export…")
        if self.export_format == "xls":
            logger.info(
                "Clicking Export — waiting up to %ss for new tab and download...",
                DOWNLOAD_TIMEOUT_MS // 1_000,
            )
            with page.context.expect_page(
                timeout=DOWNLOAD_TIMEOUT_MS
            ) as new_page_info:
                with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                    export_page.clickExport()
            new_page = new_page_info.value
            logger.info("Export new tab opened — waiting for download to finish...")
            try:
                new_page.wait_for_load_state("load", timeout=DOWNLOAD_TIMEOUT_MS)
            except Exception:
                logger.info("Export tab closed after download completed")
            return self._save_download(download_info.value)

        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            export_page.clickExport()
        return self._save_product_download(download_info.value, product_name)

    def _export_report_csv(self, page, product_name: str) -> Path:
        logger.info("Waiting for report query to finish before CSV export…")
        designer = DesignerPage(page)
        designer.wait_for_query_complete()

        logger.info("Opening CSV export dialog…")
        if self.export_format == "xls":
            designer.clickXlsExport()
        else:
            designer.clickCsvExport()

        export_page = ExportCsvPage(page)
        export_page.export_sheet_label = CreateReportPage.export_sheet_name
        export_page.wait_for_page_loaded()
        export_page.configure_excel_export_options()
        return self._click_export_and_download(page, export_page, product_name)

    def _save_product_download(self, download, product_name: str) -> Path:
        product = self._sanitize_filename_part(product_name)
        output_dir = self._product_output_dir(product_name)
        final_csv = output_dir / f"C-{product}.csv"
        suggested_name = download.suggested_filename or DEFAULT_EXPORT_NAMES["csv"]
        temp_destination = output_dir / suggested_name

        logger.info("Waiting for browser download to finish...")
        download.save_as(temp_destination)

        if temp_destination.stat().st_size == 0:
            logger.error("Downloaded file is empty (0 bytes): %s", temp_destination)
            self.saved_download_path = output_dir.resolve()
            return output_dir

        if temp_destination.suffix.lower() == ".zip":
            csv_path = self._extract_product_zip(temp_destination, final_csv)
        elif temp_destination.suffix.lower() == ".csv":
            if temp_destination != final_csv:
                temp_destination.replace(final_csv)
            csv_path = final_csv
        else:
            raise RuntimeError(f"Unexpected download type: {temp_destination.name}")

        self._validate_and_postprocess_csv(csv_path)
        self.saved_download_path = output_dir.resolve()
        logger.info("Download saved to %s", csv_path)
        return output_dir

    def _extract_product_zip(self, zip_path: Path, final_csv: Path) -> Path:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.namelist()
            logger.info("ZIP contains: %s", members)
            zf.extractall(final_csv.parent)
        zip_path.unlink()

        csv_files = sorted(final_csv.parent.glob("*.csv"))
        if not csv_files:
            raise RuntimeError(f"No CSV files found in {final_csv.parent}")

        source_csv = max(csv_files, key=lambda path: path.stat().st_size)
        if source_csv != final_csv:
            if final_csv.exists():
                final_csv.unlink()
            source_csv.replace(final_csv)
        for extra_csv in final_csv.parent.glob("*.csv"):
            if extra_csv != final_csv:
                extra_csv.unlink()
        logger.info(
            "Extracted CSV: %s (%d bytes)",
            final_csv.name,
            final_csv.stat().st_size,
        )
        return final_csv

    def _validate_and_postprocess_csv(self, csv_path: Path) -> None:
        size = csv_path.stat().st_size
        if size < MIN_CSV_BYTES:
            preview = csv_path.read_text(encoding="utf-8-sig")[:200]
            raise RuntimeError(
                f"Downloaded CSV is too small ({size} bytes) — likely empty or "
                f"summary-only export ({csv_path.name}). Preview: {preview!r}"
            )

    def _save_download(self, download) -> Path:
        stamp = datetime.now().strftime(PROCESSED_RUN_DIR_FORMAT)
        suggested_name = (
            download.suggested_filename or DEFAULT_EXPORT_NAMES[self.export_format]
        )
        destination = self.download_dir / f"{stamp}_{suggested_name}"
        logger.info("Waiting for browser download to finish...")
        download.save_as(destination)

        size = destination.stat().st_size
        if size == 0:
            logger.error("Downloaded file is empty (0 bytes): %s", destination)
            self.saved_download_path = destination.resolve()
            return destination

        if destination.suffix.lower() == ".zip":
            destination = self._extract_zip(destination, stamp)

        if destination.is_dir():
            csv_files = sorted(destination.glob("*.csv"))
            if not csv_files:
                raise RuntimeError(f"No CSV files found in {destination}")
            for csv_path in csv_files:
                self._validate_and_postprocess_csv(csv_path)
            logger.info(
                "Post-processed %d CSV files in %s",
                len(csv_files),
                destination.name,
            )
        elif destination.suffix.lower() == ".csv":
            self._validate_and_postprocess_csv(destination)

        self.saved_download_path = destination.resolve()
        logger.info("Download saved to %s", self.saved_download_path)
        return destination

    def _extract_zip(self, zip_path: Path, stamp: str) -> Path:
        """Extract all CSVs from a ZIP returned by IQVIA; return the folder path."""
        extract_dir = zip_path.parent / f"{stamp}_CRESCOR_Test"
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.namelist()
            logger.info("ZIP contains: %s", members)
            zf.extractall(extract_dir)
        zip_path.unlink()

        csv_files = sorted(extract_dir.glob("*.csv"))
        if not csv_files:
            logger.error("No CSV found inside ZIP at %s", zip_path)
            return extract_dir
        for csv_path in csv_files:
            logger.info(
                "Extracted CSV: %s (%d bytes)",
                csv_path.name,
                csv_path.stat().st_size,
            )
        return extract_dir

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
        logger.info("My Reports page loaded — right-click empty area, Create Report…")
        if self.guard:
            self.guard.disable()
        try:
            explorer.clickCreateReport()
        finally:
            if self.guard:
                self.guard.enable()
                self.guard.refresh()
        self._refresh_automation_guard()
        logger.info("Create Report clicked — waiting for new report designer…")

        source_row = first_report_source()
        create_report = CreateReportPage(page)
        create_report.wait_for_loaded()
        self._refresh_automation_guard()
        logger.info(
            "Configuring first report row — data source %s, database/catalog %s",
            source_row.data_source,
            source_row.database_catalog,
        )
        if self.guard:
            self.guard.disable()
        try:
            create_report.select_data_source_and_cube(
                source_row.data_source,
                source_row.database_catalog,
            )
        finally:
            if self.guard:
                self.guard.enable()
                self.guard.refresh()
        self._refresh_automation_guard()
        logger.info(
            "Data Source and Database/Catalog selected from report file — stopping here."
        )

        if not self.headless and getattr(self, "_keep_open", False):
            logger.info(
                "Review the selection — close the browser window when done."
            )
            self.wait_until_browser_closed()
        elif not self.headless:
            logger.info("Selection complete.")
        else:
            keep_open = getattr(self, "_keep_open", False)
            if keep_open:
                logger.info("Headless mode with keep_open=True — browser left running")
            else:
                logger.info("Selection complete — closing browser")

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

    parser = argparse.ArgumentParser(description="Run IQVIA CRESCOR Test export bot")
    parser.add_argument(
        "--format",
        choices=SUPPORTED_EXPORT_FORMATS,
        default="csv",
        help="Export format: csv (default) or xls",
    )
    args = parser.parse_args()

    bot = IqviaBotForXl(headless=False, export_format=args.format)
    try:
        bot.run()
    finally:
        bot.close()
