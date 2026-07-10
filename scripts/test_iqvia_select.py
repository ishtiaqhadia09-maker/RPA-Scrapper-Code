"""Quick test: select Data Source + Database/Catalog only."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO)

from apps.scrapers.iqvia.iqvia_bot import IqviaBot
from apps.scrapers.iqvia.page_locators import CreateReportPage, ExplorerPage, HubPage
from apps.scrapers.iqvia.report_sources import first_report_source


def main() -> None:
    row = first_report_source()
    bot = IqviaBot(headless=False)
    bot._keep_open = False
    try:
        bot._open_browser_with_auth()
        bot._navigate_to_entry_url()
        page = bot.page
        assert page is not None
        HubPage(page).clickMyReports()
        explorer = ExplorerPage(page)
        explorer.wait_for_page_loaded()
        if bot.guard:
            bot.guard.disable()
        explorer.clickCreateReport()

        cr = CreateReportPage(page)
        cr.wait_for_loaded()
        cr.select_data_source_and_cube(row.data_source, row.database_catalog)

        frame = cr._designer_frame()
        result = frame.evaluate(
            """
            () => {
                const ds = document.querySelector("select[id*='ddlDataSources']");
                const db = document.querySelector(
                    "select[id*='ddlDatabases'], select[id*='ddlDatabase']"
                );
                return {
                    dataSource: ds?.options[ds.selectedIndex]?.textContent?.trim(),
                    catalog: db?.options[db.selectedIndex]?.textContent?.trim(),
                };
            }
            """
        )
        print("RESULT:", result, flush=True)
        ok = (
            row.data_source in (result.get("dataSource") or "")
            and result.get("catalog") == row.database_catalog
        )
        print("SUCCESS" if ok else "FAILED", flush=True)
        sys.exit(0 if ok else 1)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
