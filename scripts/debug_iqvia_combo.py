"""Debug IQVIA designer combo boxes — dump DOM + test $find API."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.scrapers.iqvia.iqvia_bot import IqviaBot
from apps.scrapers.iqvia.page_locators import CreateReportPage
from apps.scrapers.iqvia.report_sources import first_report_source

logging.basicConfig(level=logging.INFO)


def main() -> None:
    bot = IqviaBot(headless=False)
    bot._keep_open = False
    try:
        bot._open_browser_with_auth()
        bot._navigate_to_entry_url()
        if bot.page and bot.page.title() == "Decision Center":
            from apps.scrapers.iqvia.page_locators import HubPage, ExplorerPage

            HubPage(bot.page).clickMyReports()
            explorer = ExplorerPage(bot.page)
            explorer.wait_for_page_loaded()
            if bot.guard:
                bot.guard.disable()
            explorer.clickCreateReport()

        page = bot.page
        assert page is not None
        cr = CreateReportPage(page)
        cr.wait_for_loaded()
        frame = cr._designer_frame()

        info = frame.evaluate(
            """
            () => {
                const out = { combos: [], hasFind: typeof $find === 'function' };
                for (const sel of document.querySelectorAll('select')) {
                    const id = sel.id || '';
                    if (!/DataSource|Database|ddl/i.test(id + sel.name)) continue;
                    const inp = document.getElementById(id + '_Input');
                    const arrow = document.getElementById(id + '_Arrow');
                    const dd = document.getElementById(id + '_DropDown');
                    let findOk = false;
                    let itemCount = null;
                    if (typeof $find === 'function') {
                        try {
                            const c = $find(id);
                            findOk = !!c;
                            if (c?.get_items) itemCount = c.get_items().get_count();
                        } catch (e) {
                            findOk = false;
                        }
                    }
                    out.combos.push({
                        id,
                        name: sel.name,
                        options: sel.options.length,
                        hasInput: !!inp,
                        hasArrow: !!arrow,
                        hasDropDown: !!dd,
                        inputVisible: inp ? inp.getBoundingClientRect().width > 0 : false,
                        findOk,
                        itemCount,
                        selected: sel.options[sel.selectedIndex]?.textContent?.trim(),
                    });
                }
                return out;
            }
            """
        )
        print("=== COMBO DEBUG ===")
        print(info)

        row = first_report_source()
        print(f"=== TRY SELECT: {row.data_source} / {row.database_catalog} ===")
        cr.select_data_source_and_cube(row.data_source, row.database_catalog)

        after = frame.evaluate(
            """
            () => {
                const ds = document.querySelector("select[id*='ddlDataSources']");
                const db = document.querySelector("select[id*='ddlDatabases'], select[id*='ddlDatabase']");
                return {
                    dataSource: ds?.options[ds.selectedIndex]?.textContent?.trim(),
                    catalog: db?.options[db.selectedIndex]?.textContent?.trim(),
                    dsInput: document.getElementById((ds?.id||'')+'_Input')?.value,
                    dbInput: document.getElementById((db?.id||'')+'_Input')?.value,
                };
            }
            """
        )
        print("=== AFTER SELECT ===")
        print(after)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
