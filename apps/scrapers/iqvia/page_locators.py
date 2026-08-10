import logging
import re
import time

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

from apps.scrapers.iqvia.config import (
    COLUMN_EXPAND_PAUSE_MS,
    DIALOG_SETTLE_MS,
    FRAME_POLL_MS,
    POLL_MS,
    QUERY_EXPORT_MAX_WAIT_MS,
    QUERY_EXPORT_POLL_MS,
    QUERY_EXPORT_STABLE_CHECKS,
    QUERY_IDLE_POLL_MS,
    SETTLE_MS,
    SLOW_POLL_MS,
)
from apps.scrapers.iqvia.report_sources import normalize_database_catalog

logger = logging.getLogger(__name__)

DEFAULT_FRAME_TIMEOUT_MS = 120_000
DROPDOWN_ITEM_SELECTOR = (
    ".rcbSlide .rcbItem:visible, "
    ".RadComboBoxDropDown .rcbItem:visible, "
    ".rcbList .rcbItem:visible, "
    '[id*="DropDown"] .rcbItem:visible'
)


def _poll_until(
    scope,
    fn,
    *,
    timeout_ms: int = 15_000,
    poll_ms: int = POLL_MS,
) -> bool:
    """Poll until fn() is true; returns immediately when ready."""
    interval = max(1, poll_ms)
    for _ in range(max(1, timeout_ms // interval)):
        try:
            if fn():
                return True
        except Exception:
            pass
        scope.wait_for_timeout(interval)
    return False


def _wait_for_combo_popup(frame, *, timeout_ms: int = 8_000) -> bool:
    def ready() -> bool:
        popup = frame.locator(
            ".rcbSlide:visible, .RadComboBoxDropDown:visible"
        ).first
        return popup.count() > 0 and popup.is_visible()

    return _poll_until(frame, ready, timeout_ms=timeout_ms)


def _wait_for_dropdown_items(frame, *, timeout_ms: int = 10_000) -> bool:
    def ready() -> bool:
        return frame.locator(DROPDOWN_ITEM_SELECTOR).count() > 0

    return _poll_until(frame, ready, timeout_ms=timeout_ms)


def _poll_for_frame(page: Page, url_fragment: str, timeout_ms: int = DEFAULT_FRAME_TIMEOUT_MS):
    for _ in range(max(1, timeout_ms // FRAME_POLL_MS)):
        frame = page.frame(
            url=lambda url: url is not None and url_fragment in url
        )
        if frame:
            return frame
        page.wait_for_timeout(FRAME_POLL_MS)
    raise PlaywrightTimeoutError(
        f"Frame containing {url_fragment!r} did not load within {timeout_ms}ms"
    )


class LoginPage:
    textbox_username = "#username"
    button_continue = "#btnContinue"
    textbox_password = "#password"
    button_submit = "#btnSubmit"
    textbox_otp = "#inputVerificationCode"
    trust_checkbox = "#chkRememberDevice"
    password_url_fragment = "/account/password"
    VERIFICATION_CODE_TITLE = "Verification Code"
    DECISION_CENTER_TITLE = "Decision Center"
    POPUP_LOGOUT = 'div[class="mat-menu-trigger user-nav"]'
    OPTION_LOGOUT = 'button[role="menuitem"][tabindex="0"]:nth-of-type(3)'

    def __init__(self, page: Page) -> None:
        self.page = page

    def wait_for_login_form(self, timeout_ms: int = 20_000) -> None:
        self.page.locator(self.textbox_username).wait_for(
            state="visible", timeout=timeout_ms
        )

    def _fill_field(self, selector: str, value: str) -> None:
        field = self.page.locator(selector)
        field.wait_for(state="visible", timeout=15_000)
        field.click()
        field.fill(value)
        if field.input_value() != value:
            field.fill("")
            field.fill(value)

    def setUsername(self, username: str) -> None:
        self._fill_field(self.textbox_username, username)

    def setPassword(self, password: str) -> None:
        self._fill_field(self.textbox_password, password)

    def clickContinue(self) -> None:
        button = self.page.locator(self.button_continue)
        button.wait_for(state="visible", timeout=10_000)
        button.click()

    def clickSubmit(self) -> None:
        button = self.page.locator(self.button_submit)
        button.wait_for(state="visible", timeout=10_000)
        button.click()

    def clickTrustCheckbox(self) -> None:
        self.page.locator(self.trust_checkbox).click()

    def setOtp(self, otp: str) -> None:
        self._fill_field(self.textbox_otp, otp)

    def sign_in(
        self,
        username: str,
        password: str,
        *,
        otp: str = "",
        allow_manual_otp: bool = False,
        guard: object | None = None,
    ) -> None:
        """Two-step IQVIA login: email → Continue → password → Submit."""
        self.wait_for_login_form()
        logger.info("Filling User ID / email…")
        self.setUsername(username)
        logger.info("Clicking Continue…")
        self.clickContinue()
        try:
            self.page.wait_for_url(
                f"**{self.password_url_fragment}**", timeout=20_000
            )
        except PlaywrightTimeoutError:
            self.page.locator(self.textbox_password).wait_for(
                state="visible", timeout=20_000
            )
        logger.info("Password page loaded — filling password…")
        self.setPassword(password)
        logger.info("Submitting login…")
        self.clickSubmit()
        # Wait for the post-submit navigation to start and settle.
        # wait_for_load_state alone can return immediately when called before the
        # navigation has started, so we first wait for the URL/title to change
        # away from the password page, giving the auth server up to 60 s to respond.
        try:
            self.page.wait_for_function(
                "() => !document.location.href.includes('/account/password')",
                timeout=60_000,
            )
        except PlaywrightTimeoutError:
            pass
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            pass
        logger.info("Post-submit page: %r", self.page.title())

        if self.page.title() == self.DECISION_CENTER_TITLE:
            return

        self._handle_verification(
            otp,
            allow_manual_otp=allow_manual_otp,
            guard=guard,
        )
        if self.page.title() != self.DECISION_CENTER_TITLE:
            self._wait_for_login_landing(timeout_sec=180)

    def is_verification_page(self, timeout_ms: int = 0) -> bool:
        try:
            if self.page.title() == self.VERIFICATION_CODE_TITLE:
                return True
        except Exception:
            pass
        if timeout_ms > 0:
            return self._is_visible(self.textbox_otp, timeout_ms)
        return False

    def _wait_for_verification_page(self, timeout_ms: int = 20_000) -> bool:
        return _poll_until(
            self.page,
            lambda: self.is_verification_page(timeout_ms=500),
            timeout_ms=timeout_ms,
        )

    def _handle_verification(
        self,
        otp: str,
        *,
        allow_manual_otp: bool,
        guard: object | None,
    ) -> None:
        if self.page.title() == self.DECISION_CENTER_TITLE:
            return
        if not self._wait_for_verification_page(timeout_ms=60_000):
            return

        if otp.strip():
            logger.info("Entering OTP from .env…")
            self.setOtp(otp)
        elif allow_manual_otp:
            if guard is not None and hasattr(guard, "disable"):
                guard.disable()
            logger.info(
                "Verification code required — enter OTP in the browser "
                "(check 'Trust this device' if shown). Waiting up to 180s…"
            )
            try:
                if not self._wait_for_verification_code_entry(timeout_sec=180):
                    raise TimeoutError(
                        f"OTP not entered within 180s — current title: "
                        f"{self.page.title()!r}"
                    )
            finally:
                if guard is not None and hasattr(guard, "enable"):
                    guard.enable()
                    guard.refresh()
        else:
            raise RuntimeError(
                "Verification code required but IQVIA_OTP is not set in .env. "
                "Add the code from your email/SMS, or run with a visible "
                "browser to enter OTP manually."
            )

        self._submit_verification_form()

    def _wait_for_verification_code_entry(self, timeout_sec: int = 180) -> bool:
        def ready() -> bool:
            if self.page.title() == self.DECISION_CENTER_TITLE:
                return True
            if not self.is_verification_page(timeout_ms=200):
                return True
            try:
                value = self.page.locator(self.textbox_otp).input_value().strip()
            except Exception:
                value = ""
            return bool(value)

        return _poll_until(
            self.page,
            ready,
            timeout_ms=timeout_sec * 1_000,
            poll_ms=200,
        )

    def _submit_verification_form(self) -> None:
        if not self.is_verification_page(timeout_ms=500):
            return

        if self.is_trust_checkbox_visible(timeout_ms=3_000):
            logger.info("Checking 'Trust this device'…")
            self.clickTrustCheckbox()

        if self.is_verification_page(timeout_ms=500):
            logger.info("Submitting verification code…")
            self.clickSubmit()
            self.page.wait_for_load_state("domcontentloaded")

    def _wait_for_login_landing(self, timeout_sec: int = 180) -> None:
        if _poll_until(
            self.page,
            lambda: self.page.title() == self.DECISION_CENTER_TITLE,
            timeout_ms=timeout_sec * 1_000,
            poll_ms=200,
        ):
            logger.info("Decision Center loaded after login")
            return

        raise TimeoutError(
            f"Decision Center (title {self.DECISION_CENTER_TITLE!r}) did not load "
            f"within {timeout_sec}s — current title: {self.page.title()!r}"
        )

    def _wait_for_otp_completion(self, timeout_sec: int = 120) -> None:
        if _poll_until(
            self.page,
            lambda: self.page.title() == "Decision Center"
            or not self._is_visible(self.textbox_otp, 200),
            timeout_ms=timeout_sec * 1_000,
            poll_ms=200,
        ):
            if self.page.title() == "Decision Center":
                return
            self.page.wait_for_load_state("domcontentloaded")
            if self.page.title() == "Decision Center":
                return
            return

        raise TimeoutError(
            f"OTP not completed within {timeout_sec}s — current title: {self.page.title()!r}"
        )

    def _is_visible(self, selector: str, timeout_ms: int) -> bool:
        try:
            self.page.locator(selector).wait_for(
                state="visible", timeout=timeout_ms
            )
            return True
        except PlaywrightTimeoutError:
            return False

    def is_username_visible(self, timeout_ms: int = 5_000) -> bool:
        return self._is_visible(self.textbox_username, timeout_ms)

    def is_password_visible(self, timeout_ms: int = 5_000) -> bool:
        return self._is_visible(self.textbox_password, timeout_ms)

    def is_otp_visible(self, timeout_ms: int = 8_000) -> bool:
        return self._is_visible(self.textbox_otp, timeout_ms)

    def is_trust_checkbox_visible(self, timeout_ms: int = 5_000) -> bool:
        return self._is_visible(self.trust_checkbox, timeout_ms)

    def clickLogout(self) -> None:
        self.page.locator(self.POPUP_LOGOUT).click()
        self.page.locator(self.OPTION_LOGOUT).click()


class HubPage:
    user_main_frame_url = "UserMainPage.aspx"
    my_reports_link = "a.SectionLink_Tile"
    my_reports_href = 'a[href="explorer/explorer.aspx?root=user"]'

    def __init__(self, page: Page) -> None:
        self.page = page

    def _user_main_frame(self, timeout_ms: int = DEFAULT_FRAME_TIMEOUT_MS):
        return _poll_for_frame(self.page, self.user_main_frame_url, timeout_ms)

    def clickMyReports(self) -> None:
        frame = self._user_main_frame()
        frame.locator(self.my_reports_link, has_text="My Reports").click()


class ExplorerPage:
    tile_view_frame_url = "explorer/TileView.aspx"
    section_link_tile = "a.SectionLink_Tile"
    report_files_text = "Report Files"
    crescor_test_text = "CRESCOR Test(1)"
    create_report_text = "Create Report"

    def __init__(self, page: Page) -> None:
        self.page = page

    def _tile_view_frame(self, timeout_ms: int = DEFAULT_FRAME_TIMEOUT_MS):
        return _poll_for_frame(self.page, self.tile_view_frame_url, timeout_ms)

    def _crescor_test_locator(self, frame):
        return frame.locator(self.section_link_tile).get_by_text(
            self.crescor_test_text,
            exact=True,
        )

    def wait_for_page_loaded(self, timeout_ms: int = 120_000) -> None:
        frame = self._tile_view_frame(timeout_ms=timeout_ms)
        frame.locator("body").wait_for(state="visible", timeout=timeout_ms)
        frame.locator(self.section_link_tile).first.wait_for(
            state="visible",
            timeout=timeout_ms,
        )

    def clickReportFiles(self) -> None:
        frame = self._tile_view_frame()
        frame.locator(self.section_link_tile, has_text=self.report_files_text).click()

    def _context_menu_has_item(self, frame, label: str) -> bool:
        for scope in (frame, self.page):
            found = scope.evaluate(
                """
                ([text]) => {
                    const nodes = Array.from(
                        document.querySelectorAll("td, div, span, a, li")
                    );
                    return nodes.some((el) => {
                        if ((el.textContent || "").trim() !== text) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                }
                """,
                [label],
            )
            if found:
                return True
        return False

    def _open_context_menu_on_empty_area(self, frame) -> None:
        body = frame.locator("body")
        body.wait_for(state="visible", timeout=15_000)
        for x, y in ((300, 350), (120, 280), (400, 450), (200, 180)):
            body.click(button="right", position={"x": x, "y": y})
            if _poll_until(
                frame,
                lambda: self._context_menu_has_item(
                    frame, self.create_report_text
                ),
                timeout_ms=800,
                poll_ms=50,
            ):
                return
        raise PlaywrightTimeoutError(
            "Could not open context menu with Create Report on My Reports page"
        )

    def _click_context_menu_item(self, frame, label: str) -> None:
        for scope in (frame, self.page):
            try:
                item = scope.get_by_text(label, exact=True).first
                if item.is_visible(timeout=500):
                    item.click(timeout=3_000)
                    return
            except PlaywrightTimeoutError:
                pass

            clicked = scope.evaluate(
                """
                ([text]) => {
                    const nodes = Array.from(
                        document.querySelectorAll("td, div, span, a, li")
                    );
                    for (const el of nodes) {
                        if ((el.textContent || "").trim() !== text) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }
                """,
                [label],
            )
            if clicked:
                return
        raise PlaywrightTimeoutError(f"Context menu item {label!r} not found")

    def _click_create_report_on_report_files_tile(self, frame) -> bool:
        """Click the Create Report link under the Report Files tile (no Open step)."""
        try:
            report_link = frame.locator(
                self.section_link_tile, has_text=self.report_files_text
            )
            report_link.wait_for(state="visible", timeout=5_000)
            tile = report_link.locator("xpath=ancestor::td[1]")
            create = tile.get_by_text(self.create_report_text, exact=True)
            create.wait_for(state="visible", timeout=3_000)
            create.click(timeout=5_000)
            return True
        except PlaywrightTimeoutError:
            pass

        clicked = frame.evaluate(
            """
            ([folderName, actionText]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const folders = Array.from(
                    document.querySelectorAll('a.SectionLink_Tile, a, span')
                ).filter((el) => trim(el.textContent) === folderName && isVisible(el));

                for (const folder of folders) {
                    let container = folder.closest('td, div, li, tr') || folder.parentElement;
                    for (let depth = 0; depth < 8 && container; depth++) {
                        if (!trim(container.textContent).includes(folderName)) {
                            container = container.parentElement;
                            continue;
                        }
                        for (const el of container.querySelectorAll('a, span, div')) {
                            if (trim(el.textContent) !== actionText) continue;
                            if (!isVisible(el)) continue;
                            el.click();
                            return true;
                        }
                        container = container.parentElement;
                    }
                }
                return false;
            }
            """,
            [self.report_files_text, self.create_report_text],
        )
        return bool(clicked)

    def clickCreateReport(self) -> None:
        """Click Create Report on the Report Files tile (fallback: context menu)."""
        frame = self._tile_view_frame()
        logger.info(
            "Clicking %r on %r tile…",
            self.create_report_text,
            self.report_files_text,
        )
        if self._click_create_report_on_report_files_tile(frame):
            return

        logger.info("Direct tile click failed — trying context menu on empty area…")
        for attempt in range(5):
            self._open_context_menu_on_empty_area(frame)
            logger.info("Clicking %r…", self.create_report_text)
            try:
                self._click_context_menu_item(frame, self.create_report_text)
                return
            except PlaywrightTimeoutError:
                if attempt == 4:
                    raise
                logger.info(
                    "Create Report menu not found — retrying context menu (%d/5)",
                    attempt + 2,
                )

    def wait_for_report_files_loaded(self, timeout_ms: int = 60_000) -> None:
        frame = self._tile_view_frame()
        frame.wait_for_load_state("load")
        self._crescor_test_locator(frame).wait_for(
            state="visible",
            timeout=timeout_ms,
        )

    def _hub_entry_url(self) -> str:
        current = self.page.url
        if "/iam/" in current:
            return current.split("/iam/", 1)[0] + "/iam/"
        return "https://hub.bi.iqvia.com/iam/"

    def _explorer_entry_url(self) -> str:
        return f"{self._hub_entry_url()}explorer/explorer.aspx?root=user"

    def _wait_for_decision_center(self, timeout_sec: int = 90) -> None:
        if not _poll_until(
            self.page,
            lambda: self.page.title() == "Decision Center",
            timeout_ms=timeout_sec * 1_000,
            poll_ms=200,
        ):
            raise PlaywrightTimeoutError(
                f"Decision Center did not load within {timeout_sec}s "
                f"(title: {self.page.title()!r})"
            )

    def _dismiss_unsaved_changes_if_present(self) -> None:
        for label in ("Don't Save", "Do Not Save", "No", "Discard"):
            for scope in (self.page,):
                try:
                    button = scope.get_by_role("button", name=label).first
                    if button.is_visible(timeout=400):
                        logger.info("Dismissing unsaved-changes prompt (%r)", label)
                        button.click(timeout=2_000)
                        self.page.wait_for_timeout(500)
                        return
                except PlaywrightTimeoutError:
                    pass
                try:
                    item = scope.get_by_text(label, exact=True).first
                    if item.is_visible(timeout=400):
                        logger.info("Dismissing unsaved-changes prompt (%r)", label)
                        item.click(timeout=2_000)
                        self.page.wait_for_timeout(500)
                        return
                except PlaywrightTimeoutError:
                    pass

    def return_to_my_reports(self) -> None:
        """Leave the designer and reopen the My Reports explorer tile view."""
        logger.info("Returning to My Reports explorer…")
        for attempt in range(3):
            try:
                HubPage(self.page).clickMyReports()
                self._dismiss_unsaved_changes_if_present()
                self.wait_for_page_loaded()
                return
            except PlaywrightTimeoutError:
                if attempt >= 2:
                    raise
                hub_url = self._hub_entry_url()
                logger.info(
                    "Reopening Decision Center hub before My Reports (attempt %d): %s",
                    attempt + 2,
                    hub_url,
                )
                try:
                    self.page.goto(
                        hub_url,
                        wait_until="domcontentloaded",
                        timeout=120_000,
                    )
                except Exception as exc:
                    logger.warning(
                        "Hub goto failed (attempt %d): %s", attempt + 1, exc
                    )
                    if attempt >= 2:
                        raise
                    _poll_until(
                        self.page,
                        lambda: self.page.title() == "Decision Center",
                        timeout_ms=5_000,
                        poll_ms=200,
                    )
                    continue
                self._dismiss_unsaved_changes_if_present()
                try:
                    self._wait_for_decision_center(timeout_sec=45)
                except PlaywrightTimeoutError:
                    logger.info(
                        "Decision Center title not seen — retrying My Reports link"
                    )

    def clickCrescorTest(self) -> None:
        frame = self._tile_view_frame()
        self._crescor_test_locator(frame).click()


class CreateReportPage:
    """New report designer — left-panel Data Source / Database/Catalog dropdowns."""

    designer_frame_url = "dashboard/designer.aspx"
    data_source_label = "Data Source"
    database_catalog_label = "Database/Catalog"
    cube_label = "Cube"
    pivot_table_text = "PivotTable"
    dimensions_folder = "Dimensions"
    period_item = "Period"
    market_dimension = "Market"
    product_dimension = "Product"
    geography_dimension = "Geography"
    attributes_folder = "Attributes"
    pack_attribute = "Pack"
    brick_attribute = "Brick"
    pivot_market_field = "Market (None)"
    set_filter_menu_text = "Set Filter..."
    begins_with_filter_operator = "Begins with"
    filter_member_search_row_label = "All"
    filter_condition_dialog_title = "Filter Condition Settings"
    show_only_top_menu_text = "Show Only the Top"
    filter_menu_text = "Filter"
    expand_members_menu_text = "Expand Members"
    custom_top_menu_text = "Custom..."
    customize_filter_dialog_title = "Customize Filter Condition"
    customize_display_range_label = "Display Range"
    custom_top_display_range_value = "Show Top Only"
    custom_top_based_on_measure = "Values"
    product_top_count = 5
    pivot_table_menu_label = "PivotTable1"
    analyze_menu_text = "Analyze"
    display_totals_menu_text = "Display Totals..."
    display_totals_dialog_title = "Display Totals"
    display_totals_hide_button_text = "Hide Totals"
    move_or_copy_menu_text = "Move or Copy..."
    move_or_copy_dialog_title = "Move or Copy"
    rename_sheet_menu_text = "Rename"
    copy_sheet_menu_text = "Copy..."
    copy_sheet_dialog_title = "Copy"
    move_part_dialog_title = "Move Part"
    copy_into_new_sheet_label = "New Sheet"
    remove_dimension_menu_text = "Remove Dimension"
    move_dimension_to_row_menu_text = "Move Dimension to Row"
    export_sheet_name = "C"
    detail_settings_menu_text = "Detail Settings..."
    detail_settings_dialog_title = "Detail Settings"
    measure_format_tab_text = "Measure Format"
    display_of_null_label = "Display of Null"
    display_of_infinity_label = "Display of Infinity"
    null_infinity_display_value = "0"
    cubes_folder = "Cubes"
    hierarchies_folder = "Hierarchies"
    relative_mat_item = "Relative MAT - Relative Month"
    column_drop_zone_text = "Drop a Column Dimension Here"
    filter_drop_zone_text = "Drop a Filter Condition Here"
    measure_drop_zone_text = "Drop a Measure Here"
    measures_folder = "Measures"
    sales_data_folder = "Sales Data"
    units_item = "Units"
    values_item = "Values"
    row_drop_zone_text = "Drop a Row Dimension Here"
    add_button_text = "Add"
    query_running_text = "Query is running"
    schema_tree_id = "trvSchema"

    def __init__(self, page: Page) -> None:
        self.page = page
        self._target_catalog: str | None = None
        self._last_product_row_coords: dict | None = None
        self._last_pack_row_coords: dict | None = None
        self._last_brick_row_coords: dict | None = None

    def reset_session_state(self) -> None:
        """Clear cached pivot coordinates between report_sources.tsv rows."""
        self._target_catalog = None
        self._last_product_row_coords = None
        self._last_pack_row_coords = None
        self._last_brick_row_coords = None

    def active_page(self) -> Page:
        """Page that currently hosts the designer (may be a new tab)."""
        try:
            frame = self._designer_frame(timeout_ms=5_000)
            for page in reversed(self.page.context.pages):
                if any(frame is child for child in page.frames):
                    return page
        except PlaywrightTimeoutError:
            pass
        return self.page

    def _designer_frame(self, timeout_ms: int = 120_000):
        """Return the designer iframe (may be on the current tab or a new one)."""
        attempts = max(1, timeout_ms // FRAME_POLL_MS)
        for _ in range(attempts):
            for page in reversed(self.page.context.pages):
                frame = page.frame(
                    url=lambda url: url is not None
                    and self.designer_frame_url in url
                )
                if frame:
                    if page != self.page:
                        logger.info(
                            "Designer opened in a new browser tab: %s",
                            page.url,
                        )
                        self.page = page
                    return frame
            self.page.wait_for_timeout(FRAME_POLL_MS)

        raise PlaywrightTimeoutError(
            f"Designer frame ({self.designer_frame_url}) did not load "
            f"within {timeout_ms}ms"
        )

    def wait_for_loaded(self, timeout_ms: int = 120_000) -> None:
        logger.info("Waiting for report designer…")
        frame = self._designer_frame(timeout_ms=timeout_ms)
        logger.info("Designer frame found — waiting for Data Source controls…")

        # The designer iframe often never fires "load" again after the first
        # paint; wait on real UI instead of frame load events.
        frame.locator(
            "#ddlDataSources, select[name='ddlDataSources']"
        ).first.wait_for(state="attached", timeout=timeout_ms)
        frame.locator("#ddlDataSources").locator(
            "xpath=ancestor::td[1]//div[contains(@class,'search-dropdown-box')]"
        ).first.wait_for(state="visible", timeout=timeout_ms)
        frame.get_by_text(self.data_source_label, exact=True).wait_for(
            state="visible",
            timeout=timeout_ms,
        )
        try:
            frame.get_by_text(self.pivot_table_text, exact=False).wait_for(
                state="visible",
                timeout=min(timeout_ms, 30_000),
            )
        except PlaywrightTimeoutError:
            logger.info(
                "PivotTable label not visible yet — sidebar is ready, continuing"
            )
        logger.info("Report designer ready")
        frame = self._designer_frame()
        self._dismiss_stale_db_popup_aggressively(frame)

    def _all_dialog_scopes(self) -> list:
        """Page + designer iframe + any child frames (error modals may live anywhere)."""
        scopes: list = []
        if self.page is not None:
            for page in self.page.context.pages:
                if page not in scopes:
                    scopes.append(page)
                for child in page.frames:
                    if child not in scopes:
                        scopes.append(child)
        try:
            designer = self._designer_frame()
            if designer not in scopes:
                scopes.append(designer)
        except PlaywrightTimeoutError:
            pass
        return scopes

    def _access_error_visible(self) -> bool:
        for scope in self._all_dialog_scopes():
            try:
                visible = scope.evaluate(
                    """
                    () => {
                        const text = document.body?.innerText || '';
                        return text.includes('does not have access')
                            || text.includes('does not exist');
                    }
                    """
                )
                if visible:
                    return True
            except Exception:
                continue
        return False

    def _dismiss_error_dialog_if_present(self, frame) -> bool:
        """Close IQVIA access/error popups instantly (stale 9229 catalog checks)."""
        dismissed = False
        for _ in range(8):
            clicked = False
            for scope in self._all_dialog_scopes():
                try:
                    hit = scope.evaluate(
                        """
                        () => {
                            const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                            const isAccessError = (text) =>
                                text.includes('does not have access')
                                || text.includes('does not exist');

                            let errorSnippet = null;
                            for (const el of document.body.querySelectorAll(
                                'div, span, td, p, label'
                            )) {
                                const text = trim(el.textContent);
                                if (!text || text.length > 400) continue;
                                if (!isAccessError(text)) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width <= 0 || r.height <= 0) continue;
                                errorSnippet = text.slice(0, 120);
                                break;
                            }
                            if (!errorSnippet) return null;

                            const candidates = [];
                            for (const btn of document.querySelectorAll(
                                'button, input[type="button"], a'
                            )) {
                                const label = trim(btn.value || btn.textContent);
                                if (label !== 'OK') continue;
                                const br = btn.getBoundingClientRect();
                                if (br.width <= 0 || br.height <= 0) continue;
                                let score = 0;
                                if (btn.closest(
                                    '.rwWindow, .RadWindow, [class*="dialog"], [class*="Dialog"]'
                                )) {
                                    score += 100;
                                }
                                const overlay = document.querySelector('.dlg-background');
                                if (overlay && overlay.offsetParent !== null) {
                                    score += 50;
                                }
                                candidates.push({ btn, score });
                            }
                            candidates.sort((a, b) => b.score - a.score);
                            if (candidates.length) {
                                candidates[0].btn.click();
                                return errorSnippet;
                            }
                            return null;
                        }
                        """
                    )
                except Exception:
                    hit = None
                if hit:
                    logger.warning("Dismissed IQVIA dialog: %s", hit)
                    dismissed = True
                    clicked = True
                    try:
                        scope.wait_for_timeout(30)
                    except Exception:
                        pass
                    break

            if clicked:
                continue

            for scope in self._all_dialog_scopes():
                try:
                    ok = scope.get_by_role("button", name="OK")
                    for index in range(ok.count()):
                        button = ok.nth(index)
                        if not button.is_visible(timeout=100):
                            continue
                        button.click(timeout=2_000, force=True)
                        logger.warning("Dismissed IQVIA dialog via OK button")
                        dismissed = True
                        clicked = True
                        break
                except Exception:
                    continue
                if clicked:
                    break

            if clicked:
                continue

            if not self._access_error_visible():
                break

            try:
                self.page.keyboard.press("Enter")
                dismissed = True
            except Exception:
                pass
            try:
                frame.wait_for_timeout(30)
            except Exception:
                pass

            if not self._access_error_visible():
                break

        return dismissed

    def _dismiss_stale_db_popup_aggressively(self, frame) -> bool:
        """Repeatedly close stale-db (9229) access popups until gone."""
        dismissed = False
        for _ in range(6):
            if self._dismiss_error_dialog_if_present(frame):
                dismissed = True
            if not self._access_error_visible():
                break
        return dismissed

    def _normalize(self, value: str) -> str:
        return "".join(ch for ch in value.upper() if ch.isalnum())

    def _resolve_select(self, frame, label: str, select_index: int | None = None):
        row_select = frame.locator("tr").filter(has_text=label).locator("select")
        if row_select.count() > 0:
            return row_select.first
        if select_index is not None:
            return frame.locator("select").nth(select_index)
        raise PlaywrightTimeoutError(f"Could not find select for {label!r}")

    def _list_select_options(self, select) -> list[str]:
        return select.evaluate(
            "el => Array.from(el.options).map(o => (o.textContent || '').trim())"
        )

    def _find_option_value(self, select, needle: str) -> str | None:
        return select.evaluate(
            """
            (element, needle) => {
                const normalize = (s) =>
                    String(s || '').replace(/[+\\s*_]/g, '').toUpperCase();
                const n = normalize(needle);
                for (const opt of element.options) {
                    const text = (opt.textContent || '').trim();
                    const val = (opt.value || '').trim();
                    if (
                        text.includes(needle) ||
                        val.includes(needle) ||
                        normalize(text).includes(n) ||
                        normalize(val).includes(n)
                    ) {
                        return opt.value;
                    }
                }
                return null;
            }
            """,
            needle,
        )

    def _select_dropdown_option(
        self,
        frame,
        label: str,
        needle: str,
        *,
        select_index: int | None = None,
        wait_attempts: int = 120,
    ) -> str:
        select = self._resolve_select(frame, label, select_index)
        select.wait_for(state="attached", timeout=15_000)

        last_options: list[str] = []
        for _ in range(wait_attempts):
            option_value = self._find_option_value(select, needle)
            if option_value:
                select.select_option(value=option_value)
                selected = select.evaluate(
                    "el => (el.options[el.selectedIndex]?.textContent || '').trim()"
                )
                logger.info("Selected %s → %r", label, selected)
                return selected
            last_options = self._list_select_options(select)
            frame.wait_for_timeout(500)

        raise PlaywrightTimeoutError(
            f"Could not select {label!r} matching {needle!r}; "
            f"options: {last_options[:15]}"
        )

    def _labeled_combo_row(self, frame, label: str):
        row = frame.locator("tr").filter(
            has=frame.get_by_text(label, exact=True)
        ).first
        if row.count() == 0:
            row = frame.locator("tr").filter(has_text=label).first
        row.wait_for(state="visible", timeout=15_000)
        return row

    def _combo_input(self, row):
        combo_input = row.locator(
            "input.rcbInput, .RadComboBox input[type='text'], input[type='text']"
        ).first
        if combo_input.count() == 0:
            raise PlaywrightTimeoutError(
                "RadComboBox text input not found in labeled row"
            )
        return combo_input

    def _combo_filter_text(self, needle: str) -> str:
        cleaned = needle.strip().lstrip("+").rstrip("*")
        numeric = re.search(r"(\d{10,})", cleaned)
        if numeric:
            return numeric.group(1)
        return cleaned

    def _select_searchable_combo(
        self,
        frame,
        label: str,
        needle: str,
        *,
        wait_attempts: int = 60,
    ) -> str:
        """IQVIA designer fields use visible RadComboBox widgets backed by hidden selects."""
        row = self._labeled_combo_row(frame, label)
        filter_text = self._combo_filter_text(needle)
        combo_input = self._combo_input(row)

        last_seen: list[str] = []
        for attempt in range(wait_attempts):
            combo_input.click()
            _wait_for_combo_popup(frame)

            popup_filter = frame.locator(
                ".rcbSlide input:visible, .RadComboBoxDropDown input:visible"
            ).first
            target_input = popup_filter if popup_filter.count() > 0 else combo_input
            target_input.fill("")
            target_input.fill(filter_text)
            _wait_for_dropdown_items(frame)

            selected = self._click_matching_combo_item(frame, needle)
            if selected:
                logger.info("Selected %s → %r", label, selected)
                return selected

            list_items = frame.locator(
                ".rcbSlide .rcbItem, .RadComboBoxDropDown .rcbItem, .rcbList .rcbItem"
            )
            for index in range(list_items.count()):
                text = list_items.nth(index).inner_text().strip()
                if self._option_matches(text, needle):
                    list_items.nth(index).click()
                    logger.info("Selected %s → %r", label, text)
                    return text
                if text:
                    last_seen.append(text)

            if attempt == wait_attempts - 1:
                target_input.press("Enter")
                _poll_until(
                    frame,
                    lambda: self._option_matches(
                        combo_input.input_value().strip(), needle
                    ),
                    timeout_ms=2_000,
                )
                current = combo_input.input_value().strip()
                if self._option_matches(current, needle):
                    logger.info("Selected %s → %r", label, current)
                    return current

            frame.page.keyboard.press("Escape")
            _poll_until(
                frame,
                lambda: frame.locator(DROPDOWN_ITEM_SELECTOR).count() == 0,
                timeout_ms=1_000,
                poll_ms=50,
            )

        raise PlaywrightTimeoutError(
            f"Could not select {label!r} matching {needle!r}; "
            f"options seen: {last_seen[:15]}"
        )

    def _select_hidden_select_option(
        self,
        frame,
        select_selector: str,
        needle: str,
        label: str,
    ) -> str | None:
        """Fallback: set value on IQVIA's hidden backing select (e.g. ddlDataSources)."""
        select = frame.locator(select_selector).first
        if select.count() == 0:
            return None
        select.wait_for(state="attached", timeout=15_000)

        option_value = self._find_option_value(select, needle)
        if not option_value:
            return None

        select.select_option(value=option_value, force=True)
        select.evaluate(
            """
            (el) => {
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }
            """
        )
        selected = select.evaluate(
            "el => (el.options[el.selectedIndex]?.textContent || '').trim()"
        )
        logger.info("Selected %s via hidden select → %r", label, selected)
        return selected

    # ------------------------------------------------------------------ #
    # Database/Catalog selection helpers (clean, Telerik-safe)           #
    # ------------------------------------------------------------------ #

    def _try_force_native_catalog_select(
        self, frame, catalog: str
    ) -> str | None:
        """Set Database/Catalog via hidden select (beats stale 9229 UI label)."""
        catalog = normalize_database_catalog(catalog)
        sel = self._catalog_select(frame)
        if sel is None:
            return None
        option_value = self._find_exact_option_value(sel, catalog)
        if not option_value:
            option_value = self._find_option_value(sel, catalog)
        if not option_value:
            digits = re.search(r"(\d{10,})$", catalog)
            if digits:
                option_value = sel.evaluate(
                    """
                    (el, [suffix]) => {
                        for (const opt of el.options) {
                            const text = (opt.textContent || '').trim();
                            if (text.endsWith(suffix)) return opt.value;
                        }
                        return null;
                    }
                    """,
                    [digits.group(1)],
                )
        if not option_value:
            return None
        try:
            sel.select_option(value=option_value, force=True)
            sel.evaluate(
                """
                (el, [optionValue]) => {
                    el.value = optionValue;
                    for (const opt of el.options) {
                        opt.selected = opt.value === optionValue;
                    }
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    const label = el.closest('td')
                        ?.querySelector('.search-dropdown-current-label');
                    if (label) {
                        const match = [...el.options].find(
                            (o) => o.value === optionValue
                        );
                        if (match) label.textContent = match.textContent.trim();
                    }
                }
                """,
                [option_value],
            )
            selected = sel.evaluate(
                "el => (el.options[el.selectedIndex]?.textContent || '').trim()"
            )
            if self._catalog_exact_match(selected, catalog):
                logger.info(
                    "Database/Catalog forced via native select → %r", selected
                )
                return selected
        except Exception:
            return None
        return None

    def _is_wrong_catalog_selected(
        self, frame, target_catalog: str
    ) -> bool:
        """True when UI shows a catalog other than the TSV target (e.g. stale 9229)."""
        target_catalog = normalize_database_catalog(target_catalog)
        cat_sel = self._catalog_select(frame)
        if cat_sel is None:
            return False
        try:
            current = cat_sel.evaluate(
                "el => (el.options[el.selectedIndex]?.textContent || '').trim()"
            )
        except Exception:
            return False
        if not current:
            return False
        return not self._catalog_exact_match(current, target_catalog)

    def _is_catalog_correct(self, frame, catalog: str) -> bool:
        """True when the hidden select (or display) matches the TSV catalog."""
        catalog = normalize_database_catalog(catalog)
        native = self._read_catalog_value(frame)
        if self._catalog_exact_match(native, catalog):
            return True
        cat_sel = self._catalog_select(frame)
        if cat_sel is None:
            return False
        cat_id = cat_sel.evaluate("el => el.id || ''")
        display = self._read_search_dropdown_display(
            frame, cat_id, self.database_catalog_label
        )
        return self._catalog_exact_match(display, catalog)

    def _force_correct_catalog(
        self, frame, catalog: str, *, dismiss: bool = True
    ) -> str | None:
        """Dismiss stale-db popup and re-select the TSV catalog if UI drifted."""
        if dismiss:
            self._dismiss_stale_db_popup_aggressively(frame)
        if self._is_catalog_correct(frame, catalog):
            return None
        logger.warning(
            "Wrong Database/Catalog on screen — forcing %r", catalog
        )
        picked = self._try_force_native_catalog_select(frame, catalog)
        if picked:
            self._dismiss_stale_db_popup_aggressively(frame)
            return picked
        return None

    @staticmethod
    def _catalog_exact_match(text: str, catalog: str) -> bool:
        return (text or "").strip() == catalog.strip()

    def _find_exact_option_value(self, select, catalog: str) -> str | None:
        return select.evaluate(
            """
            (element, [catalog]) => {
                const target = String(catalog || '').trim();
                for (const opt of element.options) {
                    const text = (opt.textContent || '').trim();
                    const val  = (opt.value || '').trim();
                    if (text === target || val === target) return opt.value;
                }
                return null;
            }
            """,
            [catalog],
        )

    def _catalog_select(self, frame):
        for selector in (
            "select[id*='ddlDatabases']",
            "select[name='ddlDatabases']",
            "select[id*='ddlDatabase']",
            "select[name='ddlDatabase']",
            "select[id*='ddlCatalogs']",
            "select[name='ddlCatalogs']",
        ):
            loc = frame.locator(selector).first
            if loc.count() > 0:
                return loc
        return None

    def _catalog_filter_text(self, catalog: str) -> str:
        """Full Database/Catalog value to type into the searchable dropdown."""
        return normalize_database_catalog(catalog)

    def _read_combo_display(self, frame, row_label: str) -> str:
        """Read the value currently shown in a labeled RadComboBox row."""
        return frame.evaluate(
            """
            ([rowLabel]) => {
                const trim = (s) => (s || "").replace(/\\s+/g, " ").trim();
                const row = [...document.querySelectorAll("tr")].find((tr) =>
                    (tr.textContent || "").includes(rowLabel)
                );
                if (!row) return "";

                for (const inp of row.querySelectorAll("input")) {
                    const r = inp.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        const val = trim(inp.value);
                        if (val) return val;
                    }
                }

                for (const combo of row.querySelectorAll(".RadComboBox")) {
                    const inp = combo.querySelector("input");
                    if (inp) {
                        const val = trim(inp.value);
                        if (val) return val;
                    }
                }

                const tds = row.querySelectorAll("td");
                for (let i = tds.length - 1; i >= 0; i--) {
                    const text = trim(tds[i].textContent);
                    if (text && text !== rowLabel && !text.includes(rowLabel)) {
                        return text;
                    }
                }
                return "";
            }
            """,
            [row_label],
        )

    def _discover_labeled_combo_ui(
        self, frame, row_label: str, select_selectors: tuple[str, ...]
    ) -> dict:
        """Locate a labeled RadComboBox arrow/input via DOM."""
        return frame.evaluate(
            """
            ([rowLabel, selectors]) => {
                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const pt = (el) => {
                    const r = el.getBoundingClientRect();
                    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                };
                const out = {};
                let select = null;
                for (const sel of selectors) {
                    select = document.querySelector(sel);
                    if (select) break;
                }
                if (select?.id) {
                    out.selectId = select.id;
                    const input = document.getElementById(select.id + "_Input");
                    if (visible(input)) {
                        out.inputId = input.id;
                        out.input = pt(input);
                    }
                    const arrow = document.getElementById(select.id + "_Arrow");
                    if (visible(arrow)) {
                        out.arrow = pt(arrow);
                    } else {
                        const link = arrow?.querySelector("a");
                        if (visible(link)) out.arrow = pt(link);
                    }
                }
                const row = [...document.querySelectorAll("tr")].find((tr) =>
                    (tr.textContent || "").includes(rowLabel)
                );
                if (row) {
                    for (const inp of row.querySelectorAll("input")) {
                        if (!visible(inp)) continue;
                        out.inputId = out.inputId || inp.id || null;
                        out.input = pt(inp);
                        break;
                    }
                    for (const combo of row.querySelectorAll(".RadComboBox")) {
                        const inp = combo.querySelector("input");
                        if (visible(inp)) {
                            out.inputId = out.inputId || inp.id || null;
                            out.input = pt(inp);
                        }
                        const a = combo.querySelector(
                            ".rcbArrowCell a, .rcbActionButton, a"
                        );
                        if (visible(a)) out.arrow = pt(a);
                    }
                    const tds = row.querySelectorAll("td");
                    for (let i = tds.length - 1; i >= 0; i--) {
                        if (visible(tds[i])) {
                            out.rowCell = pt(tds[i]);
                            break;
                        }
                    }
                }
                return out;
            }
            """,
            [row_label, list(select_selectors)],
        )

    def _discover_catalog_ui(self, frame) -> dict:
        return self._discover_labeled_combo_ui(
            frame,
            self.database_catalog_label,
            (
                "select[id*='ddlDatabases']",
                "select[name='ddlDatabases']",
                "select[id*='ddlDatabase']",
            ),
        )

    def _discover_data_source_ui(self, frame) -> dict:
        return self._discover_labeled_combo_ui(
            frame,
            self.data_source_label,
            (
                "select[id*='ddlDataSources']",
                "select[name='ddlDataSources']",
            ),
        )

    def _type_combo_filter(
        self, frame, filter_text: str, ui: dict | None = None
    ) -> None:
        """Type into an open combo filter box, or fall back to keyboard."""
        input_id = (ui or {}).get("inputId")
        if input_id:
            focused = frame.evaluate(
                """
                ([inputId]) => {
                    const el = document.getElementById(inputId);
                    if (!el) return false;
                    el.focus();
                    el.value = "";
                    el.dispatchEvent(new Event("focus", { bubbles: true }));
                    return true;
                }
                """,
                [input_id],
            )
            if focused:
                inp = frame.locator(f"#{input_id}").first
                if inp.count() > 0:
                    inp.press_sequentially(filter_text, delay=50)
                    return

        popup_filter = frame.locator(
            ".rcbSlide input:visible, .RadComboBoxDropDown input:visible"
        ).first
        if popup_filter.count() > 0:
            popup_filter.fill("")
            popup_filter.press_sequentially(filter_text, delay=50)
            return

        frame.page.keyboard.type(filter_text, delay=50)

    def _type_catalog_filter(
        self, frame, filter_text: str, ui: dict | None = None
    ) -> None:
        self._type_combo_filter(frame, filter_text, ui)

    def _open_labeled_combo(
        self, frame, ui: dict, row_label: str, select_id: str = ""
    ) -> dict:
        """Open a labeled RadComboBox dropdown."""
        if ui.get("arrow"):
            frame.page.mouse.click(ui["arrow"]["x"], ui["arrow"]["y"])
            logger.info("Opened %r dropdown via arrow", row_label)
            return ui

        if ui.get("input"):
            frame.page.mouse.click(ui["input"]["x"], ui["input"]["y"])
            logger.info("Opened %r dropdown via input", row_label)
            return ui

        if ui.get("rowCell"):
            frame.page.mouse.click(ui["rowCell"]["x"], ui["rowCell"]["y"])
            logger.info("Opened %r dropdown via row cell", row_label)
            return ui

        if select_id:
            for arrow_sel in (
                f"#{select_id}_Arrow",
                f"#{select_id}_Arrow a",
            ):
                arrow = frame.locator(arrow_sel).first
                if arrow.count() > 0:
                    try:
                        arrow.click(force=True, timeout=4_000)
                        logger.info("Opened %r dropdown via %r", row_label, arrow_sel)
                        return ui
                    except PlaywrightTimeoutError:
                        pass

        row = frame.locator("tr").filter(has_text=row_label).first
        row.click(force=True)
        logger.info("Opened %r dropdown via row click", row_label)
        return ui

    def _read_telerik_combo_display(
        self, frame, select_id: str, row_label: str
    ) -> str:
        """Read current value from backing <select> (IQVIA has no _Input elements)."""
        if select_id:
            display = frame.evaluate(
                """
                ([selectId]) => {
                    const trim = (s) => (s || "").replace(/\\s+/g, " ").trim();
                    const sel = document.getElementById(selectId);
                    if (sel && sel.selectedIndex >= 0) {
                        return trim(
                            sel.options[sel.selectedIndex]?.textContent
                        );
                    }
                    return "";
                }
                """,
                [select_id],
            )
            if display:
                return display
        return self._read_combo_display(frame, row_label)

    def _click_combo_row_cell(self, frame, row_label: str) -> None:
        """Click the dropdown arrow area (right side of value cell) to open list."""
        point = frame.evaluate(
            """
            ([rowLabel]) => {
                const row = [...document.querySelectorAll("tr")].find((tr) =>
                    (tr.textContent || "").includes(rowLabel)
                );
                if (!row) return null;

                for (const el of row.querySelectorAll(
                    "img, a, .rcbArrowCell, [class*='Arrow']"
                )) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.width < 40) {
                        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                    }
                }

                const tds = row.querySelectorAll("td");
                for (let i = tds.length - 1; i >= 0; i--) {
                    const r = tds[i].getBoundingClientRect();
                    if (r.width > 30 && r.height > 0) {
                        return {
                            x: r.right - 12,
                            y: r.y + r.height / 2,
                        };
                    }
                }
                return null;
            }
            """,
            [row_label],
        )
        if point:
            frame.page.mouse.click(point["x"], point["y"])
            logger.info(
                "Clicked %r dropdown arrow at (%.0f, %.0f)",
                row_label,
                point["x"],
                point["y"],
            )
            return
        frame.locator("tr").filter(has_text=row_label).first.click(force=True)
        logger.info("Clicked %r row (fallback)", row_label)

    def _set_hidden_select_value(
        self, frame, select_id: str, needle: str, *, partial: bool = True
    ) -> str | None:
        """Set backing <select> and fire change — may sync visible combo."""
        return frame.evaluate(
            """
            ([selectId, needle, partial]) => {
                const sel = document.getElementById(selectId);
                if (!sel) return null;
                const n = String(needle || "").trim();
                const norm = (s) => s.replace(/[+\\s*_]/g, "").toUpperCase();
                for (const opt of sel.options) {
                    const text = (opt.textContent || "").trim();
                    const ok = partial
                        ? text.includes(n) || n.includes(text)
                          || norm(text).includes(norm(n))
                        : text === n;
                    if (!ok) continue;
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event("change", { bubbles: true }));
                    sel.dispatchEvent(new Event("input", { bubbles: true }));
                    return text;
                }
                return null;
            }
            """,
            [select_id, needle, partial],
        )

    def _select_telerik_combo_by_id(
        self,
        frame,
        select_id: str,
        row_label: str,
        needle: str,
        filter_text: str,
        *,
        exact_item: bool = False,
        exact_display: bool = False,
    ) -> str:
        """
        Select from IQVIA designer dropdown.

        These combos use a hidden <select> with NO _Input / _Arrow elements.
        Open the row, type to filter, then ArrowDown+Enter to commit.
        """
        if not select_id:
            raise PlaywrightTimeoutError(
                f"Cannot select {row_label!r} — backing select id not found"
            )

        matches_display = (
            (lambda text: self._catalog_exact_match(text, needle))
            if exact_display
            else (lambda text: self._option_matches(text, needle))
        )

        current = self._read_telerik_combo_display(frame, select_id, row_label)
        if matches_display(current):
            logger.info("%s already set → %r", row_label, current)
            return current

        forced = self._set_hidden_select_value(
            frame, select_id, needle, partial=not exact_item
        )
        if forced:
            if _poll_until(
                frame,
                lambda: matches_display(
                    self._read_telerik_combo_display(
                        frame, select_id, row_label
                    )
                ),
                timeout_ms=5_000,
            ):
                current = self._read_telerik_combo_display(
                    frame, select_id, row_label
                )
                logger.info("%s set via select → %r", row_label, current)
                return current

        last_seen: list[str] = []
        for attempt in range(6):
            logger.info(
                "Selecting %r attempt %d (filter %r)…",
                row_label,
                attempt + 1,
                filter_text,
            )

            self._click_combo_row_cell(frame, row_label)
            _wait_for_combo_popup(frame)
            frame.page.keyboard.press("Control+A")
            frame.page.keyboard.type(filter_text, delay=0)
            _wait_for_dropdown_items(frame)

            coords = self._find_dropdown_item_coords(
                frame, needle, exact=exact_item
            )
            if coords:
                frame.page.mouse.click(coords["x"], coords["y"])
            else:
                frame.page.keyboard.press("ArrowDown")
                frame.page.keyboard.press("Enter")

            if _poll_until(
                frame,
                lambda: matches_display(
                    self._read_telerik_combo_display(
                        frame, select_id, row_label
                    )
                ),
                timeout_ms=5_000,
            ):
                current = self._read_telerik_combo_display(
                    frame, select_id, row_label
                )
                logger.info("%s selected → %r", row_label, current)
                return current

            items = frame.locator(
                ".rcbSlide .rcbItem:visible, "
                ".RadComboBoxDropDown .rcbItem:visible, "
                ".rcbList .rcbItem:visible, "
                '[id*="DropDown"] .rcbItem:visible'
            )
            for index in range(items.count()):
                try:
                    text = items.nth(index).inner_text(timeout=500).strip()
                except PlaywrightTimeoutError:
                    continue
                if text:
                    last_seen.append(text)

            frame.page.keyboard.press("Escape")
            _poll_until(
                frame,
                lambda: frame.locator(DROPDOWN_ITEM_SELECTOR).count() == 0,
                timeout_ms=1_000,
                poll_ms=50,
            )

        raise PlaywrightTimeoutError(
            f"Could not select {row_label!r} → {needle!r}; "
            f"UI shows {current!r}; options seen: {last_seen[:15]}"
        )

    def _find_dropdown_item_coords(
        self, frame, needle: str, *, exact: bool = True
    ) -> dict | None:
        """Find a dropdown list item by visible text (frame then page)."""
        js = """
            ([needle, exact]) => {
                const target = needle.trim();
                const match = (text) =>
                    exact ? text === target : text.includes(target);
                const selectors = [
                    ".rcbItem",
                    ".rcbSlide li",
                    ".RadComboBoxDropDown li",
                    '[id*="DropDown"] li',
                    '[id*="dropDown"] li',
                    '[class*="rcb"] li',
                ];
                const seen = new Set();
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (seen.has(el)) continue;
                        seen.add(el);
                        const text = (el.textContent || "")
                            .replace(/\\s+/g, " ").trim();
                        if (!match(text)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        el.scrollIntoView({ block: "nearest" });
                        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                    }
                }
                return null;
            }
        """
        coords = frame.evaluate(js, [needle, exact])
        if coords:
            return coords
        return self.page.evaluate(js, [needle, exact])

    def _select_labeled_dropdown(
        self,
        frame,
        row_label: str,
        needle: str,
        filter_text: str,
        *,
        ui: dict,
        select_id: str = "",
        exact_item: bool = True,
        exact_display: bool = False,
    ) -> str:
        """Open dropdown, filter, click item; confirm visible row text."""
        matches_display = (
            (lambda text: self._catalog_exact_match(text, needle))
            if exact_display
            else (lambda text: self._option_matches(text, needle))
        )
        matches_item = (
            (lambda text: self._catalog_exact_match(text, needle))
            if exact_item
            else (lambda text: self._option_matches(text, needle))
        )

        last_seen: list[str] = []
        for _attempt in range(40):
            self._open_labeled_combo(frame, ui, row_label, select_id)
            _wait_for_combo_popup(frame)
            self._type_combo_filter(frame, filter_text, ui)
            _wait_for_dropdown_items(frame)

            coords = self._find_dropdown_item_coords(
                frame, needle, exact=exact_item
            )
            if coords:
                frame.page.mouse.click(coords["x"], coords["y"])
                if _poll_until(
                    frame,
                    lambda: matches_display(
                        self._read_combo_display(frame, row_label)
                    ),
                    timeout_ms=5_000,
                ):
                    display = self._read_combo_display(frame, row_label)
                    logger.info("%s selected in UI → %r", row_label, display)
                    return display

            items = frame.locator(
                ".rcbSlide .rcbItem:visible, "
                ".RadComboBoxDropDown .rcbItem:visible, "
                ".rcbList .rcbItem:visible"
            )
            for index in range(items.count()):
                try:
                    text = items.nth(index).inner_text(timeout=1_000).strip()
                except PlaywrightTimeoutError:
                    continue
                if text:
                    last_seen.append(text)
                if not matches_item(text):
                    continue
                items.nth(index).click(force=True, timeout=5_000)
                if _poll_until(
                    frame,
                    lambda: matches_display(
                        self._read_combo_display(frame, row_label)
                    ),
                    timeout_ms=5_000,
                ):
                    display = self._read_combo_display(frame, row_label)
                    logger.info("%s selected in UI → %r", row_label, display)
                    return display

            frame.page.keyboard.press("Escape")
            _poll_until(
                frame,
                lambda: frame.locator(DROPDOWN_ITEM_SELECTOR).count() == 0,
                timeout_ms=1_000,
                poll_ms=50,
            )

        display = self._read_combo_display(frame, row_label)
        raise PlaywrightTimeoutError(
            f"Could not select {row_label!r} → {needle!r}; "
            f"UI shows {display!r}; options seen: {last_seen[:15]}"
        )

    def _read_catalog_value(self, frame) -> str:
        sel = self._catalog_select(frame)
        if sel is not None:
            return sel.evaluate(
                "el => (el.options[el.selectedIndex]?.textContent || '').trim()"
            )
        return ""

    def _wait_for_catalog_option(
        self, frame, catalog: str, timeout_ms: int = 120_000
    ) -> None:
        """Wait until the target catalog appears after Data Source selection."""
        catalog = normalize_database_catalog(catalog)
        logger.info(
            "Waiting for Database/Catalog option %r to load…", catalog
        )
        last_count = 0
        elapsed = 0
        poll_ms = POLL_MS
        saw_query = False

        while elapsed < timeout_ms:
            if self._is_query_running_visible(frame):
                if not saw_query:
                    logger.info(
                        "Query is running — waiting for catalog options…"
                    )
                    saw_query = True
                poll_ms = SLOW_POLL_MS
            else:
                if saw_query:
                    logger.info("Query finished")
                    saw_query = False
                poll_ms = POLL_MS

            sel = self._catalog_select(frame)
            if sel is None:
                frame.wait_for_timeout(poll_ms)
                elapsed += poll_ms
                continue

            available = sel.evaluate(
                "el => Array.from(el.options)"
                ".map(o => (o.textContent || '').trim()).filter(Boolean)"
            )
            if any(self._catalog_exact_match(t, catalog) for t in available):
                logger.info(
                    "Database/Catalog option found (%d options loaded)",
                    len(available),
                )
                return

            count = len(available)
            if count != last_count and count > 0:
                logger.info(
                    "Database/Catalog loading… %d options so far", count
                )
                last_count = count

            if elapsed > 0 and elapsed % 15_000 == 0:
                logger.info(
                    "Still waiting for Database/Catalog %r… (%ds)",
                    catalog,
                    elapsed // 1_000,
                )

            frame.wait_for_timeout(poll_ms)
            elapsed += poll_ms

        available = []
        sel = self._catalog_select(frame)
        if sel is not None:
            available = sel.evaluate(
                "el => Array.from(el.options)"
                ".map(o => (o.textContent || '').trim()).filter(Boolean)"
            )
        logger.warning(
            "Database/Catalog %r did not appear after Data Source; available: %r",
            catalog,
            available[:15],
        )
        return

    def _pick_fallback_catalog(self, available: list[str], target: str) -> str | None:
        """Pick closest available catalog (prefer non _Prior) when target is missing."""
        target = normalize_database_catalog(target)
        if not available:
            return None

        # Prefer exact match if present.
        for opt in available:
            if self._catalog_exact_match(opt, target):
                return opt

        # Prefer same base prefix.
        def is_prior(opt: str) -> bool:
            return opt.strip().upper().endswith("_PRIOR")

        t_digits = re.search(r"(\d{6,})$", target)
        t_num = int(t_digits.group(1)) if t_digits else None

        best: tuple[int, int, str] | None = None  # (priorPenalty, diff, opt)
        for opt in available:
            opt_clean = (opt or "").strip()
            if not opt_clean:
                continue
            o_digits = re.search(r"(\d{6,})$", opt_clean)
            o_num = int(o_digits.group(1)) if (o_digits and t_num is not None) else None
            diff = abs(o_num - t_num) if (o_num is not None and t_num is not None) else 10**12
            prior_penalty = 1 if is_prior(opt_clean) else 0
            cand = (prior_penalty, diff, opt_clean)
            if best is None or cand < best:
                best = cand
        return best[2] if best else None

    def _select_database_catalog(self, frame, catalog: str) -> str:
        """Select Database/Catalog (Cube No.) via search-dropdown-box."""
        catalog = normalize_database_catalog(catalog)
        filter_text = self._catalog_filter_text(catalog)
        sel = self._catalog_select(frame)
        if sel is None:
            raise PlaywrightTimeoutError(
                "Database/Catalog backing select not found"
            )
        select_id = sel.evaluate("el => el.id || ''")
        return self._select_search_dropdown(
            frame,
            select_id,
            catalog,
            filter_text,
            row_label=self.database_catalog_label,
            exact_match=True,
        )

    def _open_catalog_combo(self, frame, select_id: str = "") -> dict:
        ui = self._discover_catalog_ui(frame)
        return self._open_labeled_combo(
            frame, ui, self.database_catalog_label, select_id
        )

    def _find_catalog_item_coords(
        self, frame, catalog: str
    ) -> dict | None:
        return self._find_dropdown_item_coords(frame, catalog, exact=True)

    def _option_matches(self, text: str, needle: str) -> bool:
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        if needle in cleaned or cleaned in needle:
            return True
        return self._normalize(cleaned) == self._normalize(needle)

    def _click_matching_combo_item(self, frame, needle: str) -> str | None:
        return frame.evaluate(
            """
            ([needle]) => {
                const normalize = (s) =>
                    (s || '').replace(/[+\\s*_]/g, '').toUpperCase();
                const n = normalize(needle);
                const matchText = (text) => {
                    const t = (text || '').replace(/\\s+/g, ' ').trim();
                    if (!t || t.length > 120) return false;
                    return (
                        t.includes(needle) ||
                        needle.includes(t) ||
                        normalize(t).includes(n) ||
                        n.includes(normalize(t))
                    );
                };
                for (const sel of [
                    '.rcbSlide .rcbItem',
                    '.RadComboBoxDropDown .rcbItem',
                    '.rcbList .rcbItem',
                    '.rcbSlide li',
                    '.RadComboBoxDropDown li',
                ]) {
                    for (const item of document.querySelectorAll(sel)) {
                        const text = (item.textContent || '')
                            .replace(/\\s+/g, ' ').trim();
                        if (!matchText(text)) continue;
                        const r = item.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        item.click();
                        return text;
                    }
                }
                return null;
            }
            """,
            [needle],
        )

    def _search_dropdown_box(self, frame, select_id: str):
        box = frame.locator(f"#{select_id}").locator(
            "xpath=ancestor::td[1]//div[contains(@class,'search-dropdown-box')]"
        ).first
        if box.count() == 0:
            raise PlaywrightTimeoutError(
                f"search-dropdown-box not found for {select_id!r}"
            )
        return box

    def _read_search_dropdown_display(
        self, frame, select_id: str, row_label: str
    ) -> str:
        label = self._search_dropdown_box(frame, select_id).locator(
            ".search-dropdown-current-label"
        ).first
        if label.count() > 0:
            text = label.inner_text().strip()
            if text:
                return text
        return self._read_telerik_combo_display(frame, select_id, row_label)

    def _select_search_dropdown(
        self,
        frame,
        select_id: str,
        needle: str,
        filter_text: str,
        *,
        row_label: str,
        exact_match: bool = False,
    ) -> str:
        """Open IQVIA search-dropdown-box, filter, and pick an option."""
        matches = (
            (lambda text: self._catalog_exact_match(text, needle))
            if exact_match
            else (lambda text: self._option_matches(text, needle))
        )

        current = self._read_search_dropdown_display(frame, select_id, row_label)
        if matches(current):
            logger.info("%s already set → %r", row_label, current)
            return current

        logger.info(
            "Opening %s dropdown, typing %r, picking from list…",
            row_label,
            filter_text,
        )
        for attempt in range(6):
            self._dismiss_stale_db_popup_aggressively(frame)
            self._open_search_dropdown(frame, select_id)
            frame.wait_for_timeout(SETTLE_MS)
            self._type_search_dropdown_filter(frame, filter_text)
            frame.wait_for_timeout(SETTLE_MS)

            self._click_search_dropdown_option(
                frame, select_id, needle, partial=not exact_match
            )
            frame.wait_for_timeout(SETTLE_MS * 2)

            current = self._read_search_dropdown_display(
                frame, select_id, row_label
            )
            if matches(current):
                logger.info("%s selected → %r", row_label, current)
                return current

            logger.info(
                "%s attempt %d — UI shows %r, retrying…",
                row_label,
                attempt + 1,
                current,
            )
            frame.page.keyboard.press("Escape")
            frame.wait_for_timeout(SETTLE_MS)

        if exact_match and row_label == self.database_catalog_label:
            picked = self._try_force_native_catalog_select(frame, needle)
            if picked and self._catalog_exact_match(picked, needle):
                logger.info("%s forced via native select → %r", row_label, picked)
                return picked

        raise PlaywrightTimeoutError(
            f"Could not select {row_label!r} → {needle!r}; UI shows {current!r}"
        )

    def _open_search_dropdown(self, frame, select_id: str) -> None:
        box = self._search_dropdown_box(frame, select_id)
        box.scroll_into_view_if_needed()
        opened = frame.evaluate(
            """
            ([selectId]) => {
                const sel = document.getElementById(selectId);
                const label = sel?.closest("td")
                    ?.querySelector(".search-dropdown-current-label");
                if (!label) return false;
                label.click();
                return true;
            }
            """,
            [select_id],
        )
        if not opened:
            box.locator(".search-dropdown-current-label").click(
                force=True, timeout=5_000
            )
        logger.info("Opened search-dropdown for %r", select_id)

    def _search_option_matches(
        self, text: str, needle: str, *, exact: bool
    ) -> bool:
        if exact:
            return self._catalog_exact_match(text, needle)
        normalized_needle = self._normalize(needle)
        normalized_text = self._normalize(text)
        return (
            normalized_needle in normalized_text
            or normalized_text in normalized_needle
        )

    def _type_search_dropdown_filter(self, frame, filter_text: str) -> None:
        search = frame.locator("input.search-dropdown-search-box:visible").first
        search.wait_for(state="visible", timeout=5_000)
        search.fill(filter_text)
        search.dispatch_event("input")
        search.dispatch_event("keyup")
        frame.wait_for_timeout(350)

    def _click_search_dropdown_option(
        self,
        frame,
        select_id: str,
        needle: str,
        *,
        partial: bool = True,
    ) -> str | None:
        list_box = frame.locator("select.search-dropdown-list-box:visible").first
        if list_box.count() == 0:
            return None

        option_texts = list_box.evaluate(
            "el => [...el.options].map((o) => (o.textContent || '').trim())"
        )
        exact = not partial
        target_text = None
        for text in option_texts:
            if self._search_option_matches(text, needle, exact=exact):
                target_text = text
                break
        if not target_text:
            return None

        picked = frame.evaluate(
            """
            ([selectId, optionText]) => {
                const list = document.querySelector(
                    "select.search-dropdown-list-box"
                );
                let listValue = null;
                if (list) {
                    for (const opt of list.options) {
                        const t = (opt.textContent || "").trim();
                        if (t !== optionText) continue;
                        listValue = opt.value;
                        list.value = listValue;
                        opt.selected = true;
                        opt.dispatchEvent(
                            new MouseEvent("mousedown", { bubbles: true })
                        );
                        opt.dispatchEvent(
                            new MouseEvent("mouseup", { bubbles: true })
                        );
                        opt.dispatchEvent(
                            new MouseEvent("click", { bubbles: true })
                        );
                        list.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                        break;
                    }
                }

                const sel = document.getElementById(selectId);
                if (sel) {
                    for (const opt of sel.options) {
                        const t = (opt.textContent || "").trim();
                        if (t !== optionText) continue;
                        sel.value = opt.value;
                        sel.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                        sel.dispatchEvent(
                            new Event("input", { bubbles: true })
                        );
                        break;
                    }
                }

                const label = sel?.closest("td")
                    ?.querySelector(".search-dropdown-current-label");
                return (label?.textContent || optionText || "").trim();
            }
            """,
            [select_id, target_text],
        )
        logger.info("Selected search-dropdown option %r", picked or target_text)
        return picked or target_text

    def _select_data_source(self, frame, data_source: str) -> str:
        """Step 1 — Data Source via search-dropdown-box."""
        sel = frame.locator(
            "#ddlDataSources, select[name='ddlDataSources']"
        ).first
        if sel.count() == 0:
            raise PlaywrightTimeoutError("Data Source backing select not found")
        select_id = sel.evaluate("el => el.id || ''")
        return self._select_search_dropdown(
            frame,
            select_id,
            data_source,
            self._combo_filter_text(data_source),
            row_label=self.data_source_label,
            exact_match=False,
        )

    def _click_combo_list_item(
        self, frame, needle: str, *, partial: bool = True
    ) -> str | None:
        """Click a visible dropdown list row matching needle."""
        matches = (
            (lambda text: self._option_matches(text, needle))
            if partial
            else (lambda text: self._catalog_exact_match(text, needle))
        )
        for scope in (frame, self.page):
            items = scope.locator(
                ".rcbSlide .rcbItem:visible, "
                ".RadComboBoxDropDown .rcbItem:visible, "
                ".rcbList .rcbItem:visible, "
                ".rcbList li:visible, "
                '[id*="DropDown"] .rcbItem:visible'
            )
            for index in range(items.count()):
                try:
                    text = items.nth(index).inner_text(timeout=1_000).strip()
                except PlaywrightTimeoutError:
                    continue
                if not matches(text):
                    continue
                items.nth(index).click(force=True, timeout=5_000)
                logger.info("Clicked dropdown item %r", text)
                return text
        coords = self._find_dropdown_item_coords(
            frame, needle, exact=not partial
        )
        if coords:
            frame.page.mouse.click(coords["x"], coords["y"])
            return needle
        return None

    def select_data_source_and_cube(self, data_source: str, cube_no: str) -> None:
        """Select Data Source + Database/Catalog from report_sources.tsv."""
        frame = self._designer_frame()
        catalog = normalize_database_catalog(cube_no)
        self._target_catalog = catalog
        self._dismiss_stale_db_popup_aggressively(frame)

        logger.info("Step 1 — Data Source from file: %r", data_source)
        selected_source = self._select_data_source(frame, data_source)
        logger.info("Data Source confirmed → %r", selected_source)
        self._dismiss_stale_db_popup_aggressively(frame)

        ds_sel = frame.locator(
            "#ddlDataSources, select[name='ddlDataSources']"
        ).first
        ds_id = ds_sel.evaluate("el => el.id || ''") if ds_sel.count() else ""
        ds_ui = self._read_search_dropdown_display(
            frame, ds_id, self.data_source_label
        )
        if not self._option_matches(ds_ui, data_source):
            raise PlaywrightTimeoutError(
                f"Data Source UI mismatch: shows {ds_ui!r}, "
                f"expected {data_source!r}"
            )
        logger.info("Step 1 complete — Data Source %r", ds_ui)
        self._dismiss_stale_db_popup_aggressively(frame)
        self._wait_for_query_idle(frame)
        self._force_correct_catalog(frame, catalog)

        logger.info("Step 2 — Database/Catalog (Cube No.) from file: %r", catalog)
        selected_catalog = self._ensure_database_catalog(frame, catalog)
        native_catalog = self._read_catalog_value(frame)
        if not self._catalog_exact_match(native_catalog, catalog):
            raise PlaywrightTimeoutError(
                f"Database/Catalog mismatch: native select shows {native_catalog!r}, "
                f"expected {catalog!r} (picked {selected_catalog!r})"
            )
        logger.info(
            "Step 2 complete — Database/Catalog %r",
            native_catalog,
        )
        self._dismiss_stale_db_popup_aggressively(frame)
        self._wait_for_query_idle(frame)
        self._wait_for_add_enabled(frame)
        self._wait_for_overlay_cleared(frame)
        self._dismiss_stale_db_popup_aggressively(frame)

    def _ensure_database_catalog(self, frame, catalog: str) -> str:
        """Pick target catalog — native select first; display label can lag behind."""
        catalog = normalize_database_catalog(catalog)
        self._dismiss_stale_db_popup_aggressively(frame)

        if self._is_catalog_correct(frame, catalog):
            current = self._read_catalog_value(frame)
            logger.info("Database/Catalog already set → %r", current)
            return current or catalog

        self._wait_for_catalog_option(frame, catalog, timeout_ms=120_000)

        # If target catalog is not present, fall back to the closest available option.
        sel = self._catalog_select(frame)
        available: list[str] = []
        if sel is not None:
            available = sel.evaluate(
                "el => Array.from(el.options)"
                ".map(o => (o.textContent || '').trim()).filter(Boolean)"
            )
        if available and not any(
            self._catalog_exact_match(t, catalog) for t in available
        ):
            fallback = self._pick_fallback_catalog(available, catalog)
            if fallback and fallback.strip() != catalog.strip():
                logger.warning(
                    "Database/Catalog %r not available — falling back to %r",
                    catalog,
                    fallback,
                )
                catalog = normalize_database_catalog(fallback)
                self._target_catalog = catalog

        for attempt in range(1, 16):
            self._dismiss_stale_db_popup_aggressively(frame)
            picked = self._try_force_native_catalog_select(frame, catalog)
            if picked and self._catalog_exact_match(
                self._read_catalog_value(frame), catalog
            ):
                logger.info(
                    "Database/Catalog set via native select → %r", picked
                )
                return picked
            if self._is_catalog_correct(frame, catalog):
                current = self._read_catalog_value(frame)
                return current or catalog
            if attempt % 4 == 0:
                logger.info(
                    "Waiting for Database/Catalog %r to stick (attempt %d)…",
                    catalog,
                    attempt,
                )
            frame.wait_for_timeout(400)

        try:
            picked = self._select_database_catalog(frame, catalog)
            if self._is_catalog_correct(frame, catalog):
                return self._read_catalog_value(frame) or picked
        except PlaywrightTimeoutError:
            pass

        native = self._read_catalog_value(frame)
        if self._catalog_exact_match(native, catalog):
            logger.info(
                "Database/Catalog native value OK → %r (display may lag)",
                native,
            )
            return native

        raise PlaywrightTimeoutError(
            f"Database/Catalog {catalog!r} could not be selected; "
            f"native shows {native!r}"
        )

    def _is_add_enabled(self, frame) -> bool:
        return frame.evaluate(
            """
            () => {
                const btn = document.getElementById('btnOk')
                    || document.querySelector(
                        "input[type='button'][value='Add']"
                    );
                return !!btn && !btn.disabled;
            }
            """
        )

    def _wait_for_add_enabled(self, frame, timeout_ms: int = 120_000) -> None:
        """After catalog pick, poll until Add is clickable (query-aware)."""
        if self._is_add_enabled(frame):
            return
        logger.info("Waiting for Add button after Database/Catalog…")
        elapsed = 0
        poll_ms = POLL_MS
        saw_query = False
        while elapsed < timeout_ms:
            if self._is_add_enabled(frame):
                logger.info(
                    "Add button ready (%.1fs after catalog)", elapsed / 1_000
                )
                return
            if self._is_query_running_visible(frame):
                if not saw_query:
                    logger.info(
                        "Query is running — waiting for Add after catalog…"
                    )
                    saw_query = True
                poll_ms = SLOW_POLL_MS
            else:
                if saw_query:
                    logger.info("Query finished")
                    saw_query = False
                poll_ms = POLL_MS
            frame.wait_for_timeout(poll_ms)
            elapsed += poll_ms
        raise PlaywrightTimeoutError(
            "Add button did not enable within "
            f"{timeout_ms // 1_000}s after Database/Catalog selection"
        )

    def _wait_for_overlay_cleared(self, frame, timeout_ms: int = 5_000) -> None:
        cleared = frame.evaluate(
            """
            () => {
                const overlay = document.querySelector('.dlg-background');
                if (!overlay) return true;
                const style = window.getComputedStyle(overlay);
                return style.display === 'none'
                    || style.visibility === 'hidden'
                    || overlay.offsetParent === null;
            }
            """
        )
        if cleared:
            return
        logger.info("Waiting for dialog overlay to clear…")
        try:
            frame.wait_for_function(
                """
                () => {
                    const overlay = document.querySelector('.dlg-background');
                    if (!overlay) return true;
                    const style = window.getComputedStyle(overlay);
                    return style.display === 'none'
                        || style.visibility === 'hidden'
                        || overlay.offsetParent === null;
                }
                """,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            logger.info("Overlay wait timed out — continuing")

    def _click_add_button(self, frame) -> None:
        clicked = frame.evaluate(
            """
            () => {
                const btn = document.getElementById('btnOk')
                    || document.querySelector(
                        "input[type='button'][value='Add']"
                    );
                if (typeof confirmSource === 'function') {
                    try {
                        confirmSource();
                        return 'confirmSource';
                    } catch (err) {
                        /* schema tree not ready — fall back to button click */
                    }
                }
                if (btn) {
                    btn.click();
                    return 'btnOk';
                }
                return '';
            }
            """
        )
        logger.info("Add clicked via %s", clicked or "unknown")
        try:
            frame.wait_for_timeout(50)
        except Exception:
            pass
        self._dismiss_stale_db_popup_aggressively(frame)

    def _is_query_running_visible(self, frame) -> bool:
        try:
            loc = frame.get_by_text(self.query_running_text, exact=True).first
            return loc.is_visible(timeout=150)
        except PlaywrightTimeoutError:
            return False
        except Exception:
            return False

    def _is_cube_tree_loaded(self, frame) -> bool:
        """True when Add has loaded the schema tree far enough to show Period."""
        return self._period_in_schema(frame)

    def _is_tree_node_in_schema(self, frame, label: str) -> bool:
        tree = frame.locator("#trvSchema, [id*='trvSchema']").first
        try:
            return tree.get_by_text(label, exact=True).first.is_visible(
                timeout=150
            )
        except PlaywrightTimeoutError:
            return False

    def _wait_after_add_click(self, frame, timeout_ms: int = 300_000) -> None:
        """Wait after Add — fast poll when idle, slower only while query runs."""
        logger.info("Waiting for cube to load after Add…")
        elapsed = 0
        saw_query = False
        poll_ms = POLL_MS

        while elapsed < timeout_ms:
            self._dismiss_stale_db_popup_aggressively(frame)

            if self._is_query_running_visible(frame):
                if not saw_query:
                    logger.info("Query is running — waiting for it to finish…")
                    saw_query = True
                poll_ms = SLOW_POLL_MS
            elif saw_query:
                logger.info("Query finished")
                saw_query = False
                poll_ms = POLL_MS

            if self._is_cube_tree_loaded(frame):
                logger.info("Tree loaded after Add")
                return

            if elapsed > 0 and elapsed % 15_000 == 0:
                logger.info("Waiting for tree after Add… (%ds)", elapsed // 1_000)

            frame.wait_for_timeout(poll_ms)
            elapsed += poll_ms

        raise PlaywrightTimeoutError(
            f"Cube tree did not load within {timeout_ms // 1_000}s after Add"
        )

    def _wait_for_period_in_tree(self, frame, timeout_ms: int = 120_000) -> None:
        logger.info("Waiting for %r in tree…", self.period_item)
        try:
            frame.get_by_text(self.period_item, exact=True).first.wait_for(
                state="visible",
                timeout=timeout_ms,
            )
            logger.info("%r is visible in tree", self.period_item)
        except PlaywrightTimeoutError as exc:
            raise PlaywrightTimeoutError(
                f"{self.period_item!r} not visible within {timeout_ms // 1_000}s"
            ) from exc

    @staticmethod
    def _frame_click_at(frame, x: float, y: float) -> None:
        fe = frame.frame_element()
        fbox = fe.bounding_box()
        if not fbox:
            raise PlaywrightTimeoutError("Could not get designer frame position")
        frame.page.mouse.click(fbox["x"] + x, fbox["y"] + y)

    def _collect_visible_label_boxes(
        self, frame, label: str
    ) -> list[tuple[dict, int]]:
        loc = frame.get_by_text(label, exact=True)
        boxes: list[tuple[dict, int]] = []
        for idx in range(loc.count()):
            node = loc.nth(idx)
            try:
                if not node.is_visible(timeout=300):
                    continue
            except PlaywrightTimeoutError:
                continue
            box = node.bounding_box()
            if box:
                boxes.append((box, idx))
        return boxes

    def _parent_anchor_box(
        self,
        frame,
        parent_label: str,
        *,
        grandparent_label: str | None = None,
    ) -> dict | None:
        """Locate a parent tree row when the same label appears multiple times."""
        parent_boxes = self._collect_visible_label_boxes(frame, parent_label)
        if not parent_boxes:
            return None

        if grandparent_label:
            gp_box = None
            if grandparent_label != self.dimensions_folder:
                dim_loc = self._dimension_anchor_locator(
                    frame, grandparent_label
                )
                if dim_loc is not None:
                    try:
                        gp_box = dim_loc.bounding_box()
                    except PlaywrightTimeoutError:
                        gp_box = None
            if not gp_box:
                gp_boxes = self._collect_visible_label_boxes(
                    frame, grandparent_label
                )
                if not gp_boxes:
                    return None
                gp_box = min(gp_boxes, key=lambda item: item[0]["y"])[0]
            parent_boxes = [
                item
                for item in parent_boxes
                if item[0]["y"] > gp_box["y"] + 4
                and abs(item[0]["x"] - gp_box["x"]) <= 64
            ]
            if not parent_boxes:
                return None

        return min(parent_boxes, key=lambda item: item[0]["y"])[0]

    def _resolve_tree_label_locator(
        self,
        frame,
        label: str,
        *,
        deepest: bool = True,
        below_label: str | None = None,
        grandparent_label: str | None = None,
    ):
        loc = frame.get_by_text(label, exact=True)
        if loc.count() == 0:
            return None

        if below_label or grandparent_label:
            candidates: list[tuple[float, float, int]] = []
            for box, idx in self._collect_visible_label_boxes(frame, label):
                if below_label:
                    anchor = self._parent_anchor_box(
                        frame,
                        below_label,
                        grandparent_label=grandparent_label,
                    )
                    if not anchor:
                        continue
                    if box["y"] <= anchor["y"] + 4:
                        continue
                    if abs(box["x"] - anchor["x"]) > 64:
                        continue
                elif grandparent_label:
                    gp_box = self._parent_anchor_box(frame, grandparent_label)
                    if not gp_box:
                        continue
                    if box["y"] <= gp_box["y"] + 4:
                        continue
                    if abs(box["x"] - gp_box["x"]) > 64:
                        continue
                candidates.append((box["x"], box["y"], idx))
            if not candidates:
                return None
            pick = (
                max(candidates, key=lambda item: (item[0], -item[1]))
                if deepest
                else min(candidates, key=lambda item: (item[0], item[1]))
            )
            return loc.nth(pick[2])

        return loc.last if deepest else loc.first

    def _settle(self, frame, ms: int | None = None) -> None:
        """Brief pause only when UI needs a moment to settle (not a fixed step delay)."""
        pause = SETTLE_MS if ms is None else ms
        if pause > 0:
            frame.wait_for_timeout(pause)

    def _poll_until(
        self,
        frame,
        fn,
        timeout_ms: int = 15_000,
        poll_ms: int = POLL_MS,
    ) -> bool:
        """Poll until fn() is true; fast when the site responds quickly."""
        interval = max(1, poll_ms)
        for _ in range(max(1, timeout_ms // interval)):
            if fn():
                return True
            frame.wait_for_timeout(interval)
        return False

    def _poll_until_ui(
        self,
        frame,
        fn,
        *,
        idle_timeout_ms: int = 800,
        busy_timeout_ms: int = 12_000,
    ) -> bool:
        """Fast poll when idle; slower only while 'Query is running' is visible."""
        elapsed = 0
        poll_ms = POLL_MS
        while elapsed < busy_timeout_ms:
            if fn():
                return True
            if self._is_query_running_visible(frame):
                poll_ms = SLOW_POLL_MS
            else:
                poll_ms = POLL_MS
                if elapsed >= idle_timeout_ms:
                    return False
            frame.wait_for_timeout(poll_ms)
            elapsed += poll_ms
        return fn()

    def _wait_for_catalog_reload(
        self, frame, catalog: str | None = None, timeout_ms: int = 120_000
    ) -> None:
        """After Data Source change, wait until Database/Catalog is ready."""
        target = normalize_database_catalog(catalog) if catalog else None
        logger.info(
            "Waiting for Database/Catalog to reload after Data Source change…"
        )
        elapsed = 0
        poll_ms = POLL_MS
        last_count = 0
        saw_query = False

        while elapsed < timeout_ms:
            if self._is_query_running_visible(frame):
                if not saw_query:
                    logger.info(
                        "Query is running — Database/Catalog may still be loading…"
                    )
                    saw_query = True
                poll_ms = SLOW_POLL_MS
            else:
                if saw_query:
                    logger.info("Query finished")
                    saw_query = False
                poll_ms = POLL_MS

            sel = self._catalog_select(frame)
            if sel is not None:
                try:
                    available = sel.evaluate(
                        "el => Array.from(el.options)"
                        ".map(o => (o.textContent || '').trim()).filter(Boolean)"
                    )
                    count = len(available)
                    if target and any(
                        self._catalog_exact_match(t, target) for t in available
                    ):
                        logger.info(
                            "Database/Catalog ready — %r found (%d options)",
                            target,
                            count,
                        )
                        return
                    if count > 0:
                        logger.info(
                            "Database/Catalog reloaded (%d options loaded)",
                            count,
                        )
                        return
                    if count != last_count:
                        logger.info(
                            "Database/Catalog select present, options loading…"
                        )
                        last_count = count
                except Exception:
                    pass

            if elapsed > 0 and elapsed % 15_000 == 0:
                logger.info(
                    "Still waiting for Database/Catalog reload… (%ds)",
                    elapsed // 1_000,
                )

            frame.wait_for_timeout(poll_ms)
            elapsed += poll_ms

        sel = self._catalog_select(frame)
        if sel is not None:
            logger.info(
                "Database/Catalog control present after %ds — continuing",
                timeout_ms // 1_000,
            )
            return

        raise PlaywrightTimeoutError(
            "Database/Catalog control did not appear after Data Source change"
        )

    def _expand_tree_row_by_label(
        self,
        frame,
        label: str,
        *,
        deepest: bool = True,
        below_label: str | None = None,
        require_below: str | None = None,
        verify=None,
    ) -> bool:
        """Click the row chevron via in-frame JS (finds > on same row as label)."""
        result = frame.evaluate(
            """
            ([label, deepest, belowLabel, requireBelow]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const fireClick = (el) => {
                    if (!el || !isVisible(el)) return false;
                    el.scrollIntoView({ block: 'center', inline: 'nearest' });
                    for (const type of ['mousedown', 'mouseup', 'click']) {
                        el.dispatchEvent(new MouseEvent(type, {
                            bubbles: true, cancelable: true, view: window,
                        }));
                    }
                    if (typeof el.click === 'function') el.click();
                    return true;
                };

                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]')
                    || document.body;

                let anchorRect = null;
                if (requireBelow) {
                    let maxLeft = -1;
                    for (const el of root.querySelectorAll(
                        'span, a, td, div, label, li'
                    )) {
                        const text = trim(el.textContent);
                        if (text !== requireBelow || text.includes('Loading')) continue;
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.left > maxLeft) {
                            maxLeft = r.left;
                            anchorRect = r;
                        }
                    }
                }

                let belowRect = null;
                if (belowLabel) {
                    let maxLeft = -1;
                    for (const el of root.querySelectorAll(
                        'span, a, td, div, label, li'
                    )) {
                        const text = trim(el.textContent);
                        if (text !== belowLabel || text.includes('Loading')) continue;
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.left > maxLeft) {
                            maxLeft = r.left;
                            belowRect = r;
                        }
                    }
                    if (!belowRect) return { ok: false, reason: 'no-below-label' };
                }

                const hits = [];
                for (const el of root.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== label || text.includes('Loading')) continue;
                    if (!isVisible(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (anchorRect && r.top <= anchorRect.top + 4) continue;
                    if (belowRect) {
                        if (r.top <= belowRect.top + 4) continue;
                        if (Math.abs(r.left - belowRect.left) > 64) continue;
                    }
                    hits.push({ el, left: r.left, r });
                }
                if (!hits.length) return { ok: false, reason: 'no-label' };
                hits.sort((a, b) =>
                    deepest ? b.left - a.left : a.left - b.left
                );
                const labelEl = hits[0].el;
                const labelRect = hits[0].r;
                const row = labelEl.closest(
                    'tr, li, [class*="Node"], [class*="node"], '
                    + '[class*="rtLI"], [class*="Tree"]'
                ) || labelEl.parentElement;
                const rowRect = row ? row.getBoundingClientRect() : labelRect;
                const rowY = rowRect.top + rowRect.height / 2;
                const rowTol = Math.max(rowRect.height * 0.65, 10);

                for (const el of root.querySelectorAll('*')) {
                    const t = trim(el.textContent);
                    if (t !== '>' && t !== '▶' && t !== '›') continue;
                    if (!isVisible(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (Math.abs(r.top + r.height / 2 - rowY) > rowTol) continue;
                    if (r.left >= labelRect.left) continue;
                    if (fireClick(el)) {
                        return { ok: true, method: 'gt-char' };
                    }
                }

                if (row) {
                    for (const el of row.querySelectorAll(
                        '.rtPlus, .rtMinus, [class*="rtPlus"], [class*="rtMinus"]'
                    )) {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 && r.height <= 0) continue;
                        if (fireClick(el)) {
                            return { ok: true, method: 'rtPlus' };
                        }
                    }
                    for (const img of row.querySelectorAll('img')) {
                        const r = img.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.left >= labelRect.left) continue;
                        if (Math.abs(r.top + r.height / 2 - rowY) > rowTol) continue;
                        if (fireClick(img)) {
                            return { ok: true, method: 'row-img' };
                        }
                    }
                    const cands = [];
                    for (const el of row.querySelectorAll('*')) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.left >= labelRect.left - 2) continue;
                        if (Math.abs(r.top + r.height / 2 - rowY) > rowTol) continue;
                        const t = trim(el.textContent);
                        if (t && t.length > 2 && t !== label) continue;
                        cands.push({ el, left: r.left, w: r.width });
                    }
                    cands.sort((a, b) => a.left - b.left);
                    for (const cand of cands) {
                        if (cand.w > 28) continue;
                        if (fireClick(cand.el)) {
                            return { ok: true, method: 'row-leftmost' };
                        }
                    }
                }
                return { ok: false, reason: 'no-chevron' };
            }
            """,
            [label, deepest, below_label, require_below],
        )
        if not result or not result.get("ok"):
            logger.info(
                "JS chevron click failed for %r (%s)",
                label,
                (result or {}).get("reason", "unknown"),
            )
            return False

        logger.info(
            "Clicked %r chevron via JS (%s)",
            label,
            result.get("method", "unknown"),
        )
        if verify is not None:
            return self._poll_until_ui(frame, verify, idle_timeout_ms=4_000)
        self._settle(frame)
        return True

    def _chevron_offset_for_label_box(
        self, frame, label_box: dict, *, tolerance: float = 14
    ) -> float | None:
        """Return pixels to click left of label_box center for the row chevron."""
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None

        frame_row_y = (label_box["y"] + label_box["height"] / 2) - fbox["y"]
        frame_label_left = label_box["x"] - fbox["x"]

        return frame.evaluate(
            """
            ([rowY, labelLeft, tolerance]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };

                let best = null;
                let bestDy = tolerance + 1;
                for (const el of document.body.querySelectorAll('*')) {
                    const t = trim(el.textContent);
                    if (t !== '>' && t !== '▶' && t !== '›' && t !== '▼') continue;
                    if (!isVisible(el)) continue;
                    const r = el.getBoundingClientRect();
                    const y = r.top + r.height / 2;
                    const dy = Math.abs(y - rowY);
                    if (dy > tolerance) continue;
                    if (r.right >= labelLeft - 4) continue;
                    const dx = labelLeft - (r.left + r.width / 2);
                    if (dx < 8) continue;
                    if (dy < bestDy || (Math.abs(dy - bestDy) < 1 && r.left < (best?.left ?? 9999))) {
                        bestDy = dy;
                        best = { dx, left: r.left };
                    }
                }
                return best ? best.dx : null;
            }
            """,
            [frame_row_y, frame_label_left, tolerance],
        )

    def _click_chevron_for_label_box(
        self,
        frame,
        label_box: dict,
        label: str,
        *,
        verify=None,
        idle_timeout_ms: int | None = None,
        busy_timeout_ms: int | None = None,
    ) -> bool:
        """Click the > chevron left of a label using page coordinates."""
        page = frame.page
        row_y = label_box["y"] + label_box["height"] / 2
        label_left = label_box["x"]

        offsets: list[float] = []
        chevron_dx = self._chevron_offset_for_label_box(frame, label_box)
        if chevron_dx:
            offsets.append(chevron_dx)
        offsets.extend([36, 42, 48, 54, 60])

        seen: set[float] = set()
        preferred = {36, 42, 48, 54}
        if chevron_dx:
            preferred.add(round(chevron_dx))
        poll_kwargs = {}
        if idle_timeout_ms is not None:
            poll_kwargs["idle_timeout_ms"] = idle_timeout_ms
        if busy_timeout_ms is not None:
            poll_kwargs["busy_timeout_ms"] = busy_timeout_ms
        for index, offset in enumerate(offsets):
            offset = round(offset)
            if offset in seen or offset < 8:
                continue
            seen.add(offset)
            page.mouse.click(label_left - offset, row_y)
            if verify is None:
                logger.info(
                    "Clicked %r chevron (offset %dpx from label)",
                    label,
                    offset,
                )
                return True
            idle_ms = 2_500 if offset in preferred else 400
            if idle_timeout_ms is None:
                poll_kwargs["idle_timeout_ms"] = idle_ms
            if self._poll_until_ui(frame, verify, **poll_kwargs):
                logger.info(
                    "Clicked %r chevron (offset %dpx from label)",
                    label,
                    offset,
                )
                return True
        return False

    def _try_scroll_locator(self, target, *, timeout_ms: int = 2_000) -> None:
        """Best-effort scroll; never block the run on a hidden tree node."""
        try:
            target.scroll_into_view_if_needed(timeout=timeout_ms)
        except PlaywrightTimeoutError:
            logger.debug("scroll_into_view skipped — locator not visible")

    def _expand_tree_chevron(
        self,
        frame,
        label: str,
        verify,
        *,
        deepest: bool = True,
        below_label: str | None = None,
        require_below: str | None = None,
        grandparent_label: str | None = None,
    ) -> bool:
        """Expand a schema tree row — schema chevron, JS, coords, then offset."""
        self._scroll_schema_label_into_view(frame, label)

        if self._click_schema_tree_chevron(
            frame,
            label,
            below_label=below_label,
            require_below=require_below or grandparent_label,
            deepest=deepest,
        ):
            if self._poll_until_ui(frame, verify, idle_timeout_ms=2_000):
                logger.info("Clicked %r chevron via schema tree (JS)", label)
                return True

        if grandparent_label and below_label:
            target = self._resolve_tree_label_locator(
                frame,
                label,
                deepest=deepest,
                below_label=below_label,
                grandparent_label=grandparent_label,
            )
            if target is not None:
                self._try_scroll_locator(target)
                label_box = target.bounding_box()
                if label_box and self._click_chevron_for_label_box(
                    frame, label_box, label, verify=verify
                ):
                    return True

        if self._expand_tree_row_by_label(
            frame,
            label,
            deepest=deepest,
            below_label=below_label,
            require_below=require_below or grandparent_label,
            verify=verify,
        ):
            return True

        coords = self._find_row_chevron_coords(
            frame, label, deepest=deepest, below_label=below_label
        )
        if coords:
            frame.page.mouse.click(coords["x"], coords["y"])
            if self._poll_until_ui(frame, verify, idle_timeout_ms=2_000):
                logger.info(
                    "Clicked %r chevron via coords (%s)",
                    label,
                    coords.get("method", "?"),
                )
                return True

        target = self._resolve_tree_label_locator(
            frame,
            label,
            deepest=deepest,
            below_label=below_label,
            grandparent_label=grandparent_label,
        )
        if target is None:
            return False
        self._try_scroll_locator(target)
        label_box = target.bounding_box()
        if not label_box:
            return False
        return self._click_chevron_for_label_box(
            frame, label_box, label, verify=verify
        )

    def _find_row_chevron_coords(
        self,
        frame,
        label: str,
        *,
        deepest: bool = True,
        below_label: str | None = None,
    ) -> dict | None:
        result = frame.evaluate(
            """
            ([label, deepest, belowLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]')
                    || document.body;

                let belowRect = null;
                if (belowLabel) {
                    let maxLeft = -1;
                    for (const el of root.querySelectorAll(
                        'span, a, td, div, label, li, nobr'
                    )) {
                        const text = trim(el.textContent);
                        if (text !== belowLabel || text.includes('Loading')) continue;
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.left > maxLeft) {
                            maxLeft = r.left;
                            belowRect = r;
                        }
                    }
                    if (!belowRect) return null;
                }

                const labelHits = [];
                for (const el of root.querySelectorAll(
                    'span, a, td, div, label, li, nobr'
                )) {
                    const text = trim(el.textContent);
                    if (text !== label || text.includes('Loading')) continue;
                    if (!isVisible(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (belowRect) {
                        if (r.top <= belowRect.top + 4) continue;
                        if (r.left < belowRect.left - 8) continue;
                        if (r.left - belowRect.left > 96) continue;
                    }
                    labelHits.push({ el, left: r.left, r });
                }
                if (!labelHits.length) return null;
                labelHits.sort((a, b) =>
                    deepest ? b.left - a.left : a.left - b.left
                );
                const labelHit = labelHits[0];
                const labelRect = labelHit.r;
                const rowY = labelRect.top + labelRect.height / 2;
                const rowTol = Math.max(labelRect.height * 0.75, 10);

                let chevron = null;
                for (const el of document.body.querySelectorAll('*')) {
                    const t = trim(el.textContent);
                    if (t !== '>' && t !== '▶' && t !== '▼' && t !== '›') continue;
                    if (!isVisible(el)) continue;
                    const r = el.getBoundingClientRect();
                    const y = r.top + r.height / 2;
                    if (Math.abs(y - rowY) > rowTol) continue;
                    if (r.left >= labelRect.left - 2) continue;
                    if (!chevron || r.left < chevron.left) {
                        chevron = { x: r.left + r.width / 2, y, left: r.left };
                    }
                }
                if (chevron) {
                    return { ok: true, x: chevron.x, y: chevron.y, method: 'gt-char' };
                }

                const row = labelHit.el.closest(
                    'tr, li, [class*="Node"], [class*="rtLI"], [class*="Tree"]'
                ) || labelHit.el.parentElement;
                if (row) {
                    for (const el of row.querySelectorAll(
                        '.rtPlus, .rtMinus, [class*="rtPlus"], [class*="rtMinus"]'
                    )) {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 && r.height <= 0) continue;
                        return {
                            ok: true,
                            x: r.left + Math.max(r.width, 8) / 2,
                            y: rowY,
                            method: 'rtPlus',
                        };
                    }
                    for (const el of row.querySelectorAll('img, svg, span, i')) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.left >= labelRect.left - 2) continue;
                        const y = r.top + r.height / 2;
                        if (Math.abs(y - rowY) > rowTol) continue;
                        if (!chevron || r.left < chevron.left) {
                            chevron = { x: r.left + r.width / 2, y, left: r.left };
                        }
                    }
                }
                if (chevron) {
                    return { ok: true, x: chevron.x, y: chevron.y, method: 'row-leftmost' };
                }

                for (const offset of [48, 42, 36, 54, 60]) {
                    const x = labelRect.left - offset;
                    if (x > 4) {
                        return { ok: true, x, y: rowY, method: 'offset-' + offset };
                    }
                }
                return null;
            }
            """,
            [label, deepest, below_label],
        )
        return result if result and result.get("ok") else None

    def _click_tree_row_chevron(
        self,
        frame,
        label: str,
        *,
        deepest: bool = True,
        below_label: str | None = None,
    ) -> bool:
        """Click the > chevron on a tree row — never the label text."""
        target = self._resolve_tree_label_locator(
            frame, label, deepest=deepest, below_label=below_label
        )
        if target is None:
            logger.info("No %r label found for chevron click", label)
            return False

        target.scroll_into_view_if_needed()
        label_box = target.bounding_box()
        if not label_box:
            return False

        return self._click_chevron_for_label_box(frame, label_box, label)

    def _dimension_anchor_locator(self, frame, dimension: str):
        """The dimension row under Dimensions — not a nested attribute leaf."""
        target = self._resolve_tree_label_locator(
            frame,
            dimension,
            deepest=False,
            below_label=self.dimensions_folder,
        )
        if target is not None:
            return target
        return self._resolve_tree_label_locator(
            frame, dimension, deepest=False
        )

    def _dimension_children_visible(self, frame, dimension: str) -> bool:
        target = self._dimension_anchor_locator(frame, dimension)
        if target is None:
            return False
        try:
            p_box = target.bounding_box()
        except PlaywrightTimeoutError:
            return False
        if not p_box:
            return False

        found = {self.attributes_folder: False, self.hierarchies_folder: False}
        for child_label in found:
            loc = frame.get_by_text(child_label, exact=True)
            for idx in range(loc.count()):
                node = loc.nth(idx)
                try:
                    if not node.is_visible(timeout=200):
                        continue
                except PlaywrightTimeoutError:
                    continue
                box = node.bounding_box()
                if not box:
                    continue
                if box["y"] <= p_box["y"] + 4:
                    continue
                if abs(box["x"] - p_box["x"]) > 64:
                    continue
                found[child_label] = True
                break
        return all(found.values())

    def _period_children_visible(self, frame) -> bool:
        return self._dimension_children_visible(frame, self.period_item)

    def _market_children_visible(self, frame) -> bool:
        return self._dimension_children_visible(frame, self.market_dimension)

    def click_add(self) -> None:
        """Click Add — wait for query + cube tree before continuing."""
        frame = self._designer_frame()
        logger.info("Step 3a — clicking %r button…", self.add_button_text)

        add_button = frame.locator(
            "#btnOk, input[type='button'][value='Add']"
        ).first
        add_button.wait_for(state="visible", timeout=15_000)
        if not self._is_add_enabled(frame):
            self._wait_for_add_enabled(frame)
        if self._target_catalog:
            self._force_correct_catalog(frame, self._target_catalog)
        add_button.scroll_into_view_if_needed()
        self._wait_for_overlay_cleared(frame)
        self._dismiss_stale_db_popup_aggressively(frame)
        self._click_add_button(frame)
        logger.info("Add clicked — waiting for load…")

        self._dismiss_stale_db_popup_aggressively(frame)
        self._wait_after_add_click(frame)
        self._dismiss_stale_db_popup_aggressively(frame)
        logger.info("Step 3a complete — cube tree loaded")

    def _period_in_schema(self, frame) -> bool:
        tree = frame.locator("#trvSchema, [id*='trvSchema']").first
        try:
            return tree.get_by_text(
                self.period_item, exact=True
            ).first.is_visible(timeout=150)
        except PlaywrightTimeoutError:
            return False

    def click_period_in_tree(self) -> None:
        """Click Period chevron — wait for Attributes + Hierarchies."""
        frame = self._designer_frame()
        logger.info("Step 3b — clicking Period chevron…")

        if self._period_expand_verified(frame):
            logger.info("Period already expanded")
            return

        self._ensure_period_children_visible(frame)
        logger.info("Step 3b complete — %r expanded", self.period_item)

    def add_and_click_period(self) -> None:
        """Step 3 — Add, wait for tree, expand Period via chevron."""
        logger.info("Step 3 — Add, wait for load, expand Period…")
        self.click_add()
        self.click_period_in_tree()

    def drag_relative_mat_to_columns(self) -> None:
        """Step 4 — Hierarchies chevron → drag Relative MAT to columns."""
        logger.info(
            "Step 4 — %r chevron, drag %r to columns…",
            self.hierarchies_folder,
            self.relative_mat_item,
        )
        self.open_hierarchies_and_drag_relative_mat_to_columns()
        logger.info("Step 4 complete — %r in column zone", self.relative_mat_item)

    def expand_sales_data(self) -> None:
        """Step 5 — expand Measures → Sales Data via chevron."""
        logger.info(
            "Step 5 — expand %r under %r…",
            self.sales_data_folder,
            self.measures_folder,
        )
        self.expand_sales_data_folder()
        logger.info("Step 5 complete — %r expanded", self.sales_data_folder)

    def drag_units_and_values_to_measures(self) -> None:
        """Step 6 — drag Units and Values from Sales Data to measure zone."""
        logger.info(
            "Step 6 — drag %r and %r to %r…",
            self.units_item,
            self.values_item,
            self.measure_drop_zone_text,
        )
        self.open_sales_data_and_drag_units_values_to_measures()
        logger.info(
            "Step 6 complete — %r and %r in measure zone",
            self.units_item,
            self.values_item,
        )

    def expand_market(self) -> None:
        """Step 7 — expand Market under Dimensions via chevron."""
        logger.info(
            "Step 7 — expand %r under %r…",
            self.market_dimension,
            self.dimensions_folder,
        )
        self.expand_market_folder()
        logger.info("Step 7 complete — %r expanded", self.market_dimension)

    def drag_market_attribute_to_filter(self) -> None:
        """Step 8 — expand Market → Attributes, drag Market to filter zone."""
        logger.info(
            "Step 8 — expand %r under %r, drag %r to %r…",
            self.attributes_folder,
            self.market_dimension,
            self.market_dimension,
            self.filter_drop_zone_text,
        )
        self.open_market_attributes_and_drag_market_to_filter()
        logger.info(
            "Step 8 complete — %r in filter zone",
            self.market_dimension,
        )

    def expand_product(self) -> None:
        """Step 10 — expand Product under Dimensions via chevron."""
        logger.info(
            "Step 10 — expand %r under %r…",
            self.product_dimension,
            self.dimensions_folder,
        )
        self.expand_product_folder()
        logger.info("Step 10 complete — %r expanded", self.product_dimension)

    def expand_geography(self) -> None:
        """Expand Geography under Dimensions via chevron (before Brick drop)."""
        logger.info(
            "Expanding %r under %r…",
            self.geography_dimension,
            self.dimensions_folder,
        )
        self.expand_geography_folder()
        logger.info("%r expanded", self.geography_dimension)

    def drag_product_attribute_to_row(self) -> None:
        """Step 11 — Product → Attributes → drag Product to row zone."""
        logger.info(
            "Step 11 — expand %r under %r, drag %r to %r…",
            self.attributes_folder,
            self.product_dimension,
            self.product_dimension,
            self.row_drop_zone_text,
        )
        self.open_product_attributes_and_drag_product_to_row()
        logger.info(
            "Step 11 complete — %r in row zone",
            self.product_dimension,
        )

    def drag_pack_attribute_to_product_row(self) -> None:
        """Step 12 — Product → Attributes → drag Pack onto Product row (right pivot)."""
        logger.info(
            "Step 12 — drag %r from %r → %r onto right-side %r row…",
            self.pack_attribute,
            self.attributes_folder,
            self.product_dimension,
            self.product_dimension,
        )
        self.open_product_attributes_and_drag_pack_to_product_row()
        logger.info(
            "Step 12 complete — %r nested on %r row",
            self.pack_attribute,
            self.product_dimension,
        )

    def drag_brick_attribute_to_pack_row(self) -> None:
        """Step 14 — Geography → Attributes → drag Brick onto Pack row (right pivot)."""
        logger.info(
            "Step 14 — expand %r, expand %r, drag %r onto right-side %r row "
            "(nested with %r and %r)…",
            self.geography_dimension,
            self.attributes_folder,
            self.brick_attribute,
            self.pack_attribute,
            self.product_dimension,
            self.pack_attribute,
        )
        self.open_geography_attributes_and_drag_brick_to_pack_row()
        logger.info(
            "Step 14 complete — %r nested on %r row with %r / %r",
            self.brick_attribute,
            self.pack_attribute,
            self.product_dimension,
            self.pack_attribute,
        )

    def drag_brick_attribute_to_row(self) -> None:
        """Brick-first order — Geography → Attributes → drag Brick to empty row zone."""
        logger.info(
            "Drag %r from %r → %r (outermost row dimension)…",
            self.brick_attribute,
            self.geography_dimension,
            self.row_drop_zone_text,
        )
        self.open_geography_attributes_and_drag_brick_to_row()
        logger.info(
            "%r placed as outermost row dimension", self.brick_attribute
        )

    def drag_pack_attribute_to_brick_row(self) -> None:
        """Brick-first order — Product → Attributes → drag Pack onto the Brick row."""
        logger.info(
            "Drag %r from %r onto right-side %r row…",
            self.pack_attribute,
            self.product_dimension,
            self.brick_attribute,
        )
        self.open_product_attributes_and_drag_pack_to_brick_row()
        logger.info(
            "%r nested on %r row", self.pack_attribute, self.brick_attribute
        )

    def drag_product_attribute_to_pack_row(self) -> None:
        """Brick-first order — Product → Attributes → drag Product onto the Pack row."""
        logger.info(
            "Drag %r from %r onto right-side %r row…",
            self.product_dimension,
            self.attributes_folder,
            self.pack_attribute,
        )
        self.open_product_attributes_and_drag_product_to_pack_row()
        logger.info(
            "%r nested on %r row", self.product_dimension, self.pack_attribute
        )

    @staticmethod
    def sheet_name(prefix: str, product: str) -> str:
        """Build a sheet tab name like ``C-COSOME E SYP`` from a product name."""
        clean = " ".join((product or "").split()).strip()
        for char in '\\/:*?"<>|[]':
            clean = clean.replace(char, "-")
        clean = clean.strip()
        return f"{prefix}-{clean}" if clean else prefix

    def _find_collapsed_column_expand_targets(self, frame) -> list[dict]:
        """Return click points for collapsed MAT/MTH column (+) icons in the pivot."""
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const isPeriodColumnLabel = (text) =>
                    /^(?:MAT|MTH)\\s+\\d{4}\\/\\d{1,2}$/.test(trim(text));

                const targets = [];
                const seen = new Set();

                const consider = (td, span, text) => {
                    const r = td.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 260) return;

                    const expall = span.getAttribute('expall');
                    const colall = span.getAttribute('colall');
                    let expandImg = null;
                    for (const img of td.querySelectorAll('img')) {
                        const src = (img.getAttribute('src') || '').toLowerCase();
                        const alt = (img.getAttribute('alt') || '').toLowerCase();
                        if (
                            src.includes('plus')
                            || src.includes('expand')
                            || alt.includes('expand')
                        ) {
                            expandImg = img;
                            break;
                        }
                    }
                    const collapsed = expall === '0' || colall === '0' || !!expandImg;
                    if (!collapsed) return;
                    if (!expandImg) {
                        for (const img of td.querySelectorAll('img')) {
                            const ir = img.getBoundingClientRect();
                            if (ir.width <= 0 || ir.height <= 0) continue;
                            if (ir.width > 18 && ir.height > 18) continue;
                            expandImg = img;
                            break;
                        }
                    }
                    if (!expandImg) return;

                    const ir = expandImg.getBoundingClientRect();
                    const key = `${text}|${Math.round(ir.x)}`;
                    if (seen.has(key)) return;
                    seen.add(key);
                    targets.push({
                        text,
                        x: ir.x + ir.width / 2,
                        y: ir.y + ir.height / 2,
                    });
                };

                const scanColumnCells = (requirePeriodLabel) => {
                    for (const td of document.querySelectorAll('td[area="columns"]')) {
                        if (inTree(td)) continue;
                        const span = td.querySelector('span[axis="c"], nobr span');
                        if (!span) continue;
                        const text = trim(span.textContent);
                        if (!text) continue;
                        if (requirePeriodLabel && !isPeriodColumnLabel(text)) continue;
                        consider(td, span, text);
                    }
                };

                scanColumnCells(true);

                if (targets.length === 0) {
                    for (const el of document.body.querySelectorAll(
                        'span, td, nobr, th'
                    )) {
                        const text = trim(el.textContent);
                        if (!isPeriodColumnLabel(text)) continue;
                        if (inTree(el)) continue;
                        const td = el.closest('td[area="columns"], td, th') || el;
                        const span = td.querySelector('span[axis="c"]') || el;
                        consider(td, span, text);
                    }
                }

                if (targets.length === 0) {
                    scanColumnCells(false);
                }

                targets.sort((a, b) => a.x - b.x);
                return targets;
            }
            """
        )

    def _find_collapsed_mat_column_expand_targets(self, frame) -> list[dict]:
        """Backward-compatible alias — includes MAT and MTH column headers."""
        return self._find_collapsed_column_expand_targets(frame)

    def _expand_collapsed_column_headers(self, frame) -> bool:
        """Click + on collapsed MAT/MTH column headers; return True if any expanded."""
        expanded_any = False
        for _pass in range(1, 4):
            targets = self._find_collapsed_column_expand_targets(frame)
            if not targets:
                break

            fbox = frame.frame_element().bounding_box()
            if not fbox:
                break

            for target in targets:
                px = fbox["x"] + target["x"]
                py = fbox["y"] + target["y"]
                frame.page.mouse.click(px, py)
                frame.wait_for_timeout(COLUMN_EXPAND_PAUSE_MS)
                logger.info("Expanded column %r (+ icon)", target.get("text"))
                expanded_any = True

            if expanded_any:
                self._wait_for_query_idle(frame)
            frame.wait_for_timeout(COLUMN_EXPAND_PAUSE_MS)

        return expanded_any

    def _expand_collapsed_mat_column_headers(self, frame) -> bool:
        """Backward-compatible alias for MAT/MTH column header expansion."""
        return self._expand_collapsed_column_headers(frame)

    def _pivot_has_collapsed_row_members(self, frame) -> bool:
        """True when pivot data rows still show + expand icons."""
        return bool(
            frame.evaluate(
                """
                () => {
                    const tree = document.getElementById('trvSchema')
                        || document.querySelector('[id*="trvSchema"]');
                    const inTree = (el) => !!(tree && tree.contains(el));

                    for (const img of document.querySelectorAll('img')) {
                        if (inTree(img)) continue;
                        const src = (img.getAttribute('src') || '').toLowerCase();
                        const alt = (img.getAttribute('alt') || '').toLowerCase();
                        const isPlus = (
                            src.includes('plus')
                            || src.includes('itemexpand')
                            || alt.includes('expand')
                        );
                        if (!isPlus) continue;
                        const r = img.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.top < 140 || r.top > 900) continue;
                        if (img.closest('td[area="columns"]')) continue;
                        return true;
                    }
                    return false;
                }
                """
            )
        )

    def _active_pivot_row_dimensions(self, frame) -> list[str]:
        """Row-dimension headers currently on the pivot (Product, Pack, Brick, Market, …)."""
        found: list[str] = []
        for label in (
            self.product_dimension,
            self.pack_attribute,
            self.brick_attribute,
            self.market_dimension,
        ):
            if self._resolve_pivot_row_field_coords(frame, label):
                found.append(label)
        return found

    def _expand_all_pivot_row_members(self, frame) -> None:
        for dimension in self._active_pivot_row_dimensions(frame):
            self._expand_pivot_row_members(dimension)

    def _com_sheet_tabs_for_product(self, frame, product: str) -> list[str]:
        """Resolve C-, O-, and M- sheet tab names for a product."""
        tabs: list[str] = []
        for prefix in ("C", "O", "M"):
            preferred = self.sheet_name(prefix, product)
            try:
                tabs.append(self._resolve_sheet_tab_name(frame, preferred))
            except PlaywrightTimeoutError:
                needle = f"{prefix}-".upper()
                match = next(
                    (t for t in self._list_sheet_tab_names(frame) if t.upper().startswith(needle)),
                    None,
                )
                if match and match not in tabs:
                    tabs.append(match)
        return tabs

    def _expand_pivot_on_active_sheet(self, frame) -> None:
        """Expand collapsed column headers and row members on the active sheet."""
        for attempt in range(1, 4):
            col_collapsed = bool(self._find_collapsed_column_expand_targets(frame))
            row_collapsed = self._pivot_has_collapsed_row_members(frame)
            if not col_collapsed and not row_collapsed:
                return

            logger.info(
                "Pivot expansion needed (column +=%s, row +=%s, attempt %d)…",
                col_collapsed,
                row_collapsed,
                attempt,
            )

            if col_collapsed:
                self._expand_collapsed_column_headers(frame)

            if row_collapsed:
                self._expand_all_pivot_row_members(frame)

            self._wait_for_query_idle(frame)

    def ensure_pivot_expanded_before_export(self, product: str | None = None) -> None:
        """Before export: expand collapsed columns/rows on each C, O, and M sheet."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)

        sheets = (
            self._com_sheet_tabs_for_product(frame, product)
            if product
            else [
                tab
                for tab in self._list_sheet_tab_names(frame)
                if re.match(r"^[COM]-", tab, re.I)
            ]
        )
        if not sheets:
            sheets = [None]

        for sheet_name in sheets:
            label = sheet_name or "active sheet"
            if sheet_name:
                logger.info("Pre-export expansion — opening sheet %r…", sheet_name)
                self._activate_sheet_tab(frame, sheet_name)
                self._wait_for_query_idle(frame)

            self._expand_pivot_on_active_sheet(frame)

            if self._find_collapsed_column_expand_targets(frame) or (
                self._pivot_has_collapsed_row_members(frame)
            ):
                logger.warning(
                    "Sheet %r may still have collapsed members after pre-export expand",
                    label,
                )
            else:
                logger.info("Pre-export expansion check — %r fully expanded", label)

    def expand_pivot_mat_columns(self) -> None:
        """Expand MAT/MTH column (+) icons, then Product/Pack → Expand Members."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)

        col_collapsed = bool(self._find_collapsed_column_expand_targets(frame))
        row_collapsed = self._pivot_has_collapsed_row_members(frame)
        if not col_collapsed and not row_collapsed:
            logger.info(
                "Pivot columns and row members already expanded — skipping step 13c"
            )
            return

        if col_collapsed:
            logger.info("Expanding collapsed MAT/MTH column headers (+ icon)…")
            self._expand_collapsed_column_headers(frame)
            logger.info("Column header expansion complete")
        else:
            logger.info("No collapsed column headers found — already expanded")

        if row_collapsed:
            self._expand_all_pivot_row_members(frame)
        else:
            logger.info("Pivot row members already expanded")

    def _expand_pivot_row_members(self, dimension: str) -> None:
        """Pivot row filter menu → Expand Members (Product, Pack, Brick, …)."""
        frame = self._designer_frame()
        if not self._pivot_has_collapsed_row_members(frame):
            logger.info(
                "No collapsed %r row members — skipping Expand Members",
                dimension,
            )
            return
        logger.info(
            "Expanding %r row members (%r → %r)…",
            dimension,
            self.filter_menu_text,
            self.expand_members_menu_text,
        )
        self._scroll_pivot_row_dimension_into_view(frame, dimension)
        frame.wait_for_timeout(300)

        def _expand_menu_visible() -> bool:
            return self._is_menu_item_visible(
                self.expand_members_menu_text, partial=True
            )

        try:
            opened = False
            for attempt in range(1, 5):
                self._clear_open_popups(frame)

                if self._click_row_dimension_member_icon(frame, dimension):
                    frame.wait_for_timeout(400)
                    if self._poll_until(frame, _expand_menu_visible, timeout_ms=2_500):
                        opened = True
                        break

                self._ensure_pivot_row_menu_open(frame, dimension)
                if self._poll_until(frame, _expand_menu_visible, timeout_ms=2_500):
                    opened = True
                    break

                logger.info(
                    "Expand Members menu for %r not ready (attempt %d)",
                    dimension,
                    attempt,
                )
                frame.wait_for_timeout(400)

            if not opened and not _expand_menu_visible():
                logger.warning(
                    "Could not open %r row menu for %r",
                    dimension,
                    self.expand_members_menu_text,
                )
                return

            if self._try_click_menu_item(self.expand_members_menu_text):
                self._wait_for_query_idle(frame)
                logger.info(
                    "%r row members expanded via %r",
                    dimension,
                    self.expand_members_menu_text,
                )
            else:
                logger.warning(
                    "%r not found for %r row — continuing",
                    self.expand_members_menu_text,
                    dimension,
                )
        except Exception as exc:
            logger.warning("Expand Members for %r skipped: %s", dimension, exc)
        finally:
            self._clear_open_popups(frame)

    def build_sheet_c(self, product: str) -> str:
        """Rename the active pivot sheet to ``C-<product>`` (right-click tab → Rename)."""
        name = self.sheet_name("C", product)
        logger.info("Building sheet C — rename active sheet to %r…", name)
        self.rename_sheet_tab(name)
        frame = self._designer_frame()
        actual = self._wait_for_sheet_tab_name(frame, name)
        logger.info("Sheet C ready — %r", actual)
        return actual

    def build_sheet_o(self, product: str, *, source_sheet: str) -> str:
        """Copy sheet C, rename to ``O-<product>``, apply Product Begins-with filter → OK."""
        name = self.sheet_name("O", product)
        logger.info(
            "Building sheet O — copy %r → %r, filter %r (Begins with)…",
            source_sheet,
            name,
            self.product_dimension,
        )
        self.copy_sheet_tab(current_name=source_sheet)
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self._activate_copied_sheet_tab(frame, source_sheet)
        self.rename_sheet_tab(name)
        self.apply_product_row_filter(product)
        logger.info("Sheet O ready — %r (no further changes after filter OK)", name)
        return name

    def build_sheet_m(self, product: str, *, source_sheet: str) -> str:
        """Copy C to New Sheet, rename M, remove Product + Pack, move Market to row."""
        name = self.sheet_name("M", product)
        logger.info(
            "Building sheet M — copy %r → New Sheet → rename %r…",
            source_sheet,
            name,
        )
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self._activate_sheet_tab(frame, source_sheet)
        self.copy_sheet_tab(current_name=source_sheet, into_new_sheet=True)
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self._activate_copied_sheet_tab(frame, source_sheet)
        self.rename_sheet_tab(name)
        self._wait_for_query_idle(frame)
        # Innermost first: Product, then Pack — chevron → Dimension → Remove.
        self.remove_pivot_dimension(self.product_dimension)
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self._clear_open_popups(frame)
        frame.wait_for_timeout(DIALOG_SETTLE_MS)
        self.remove_pivot_dimension(self.pack_attribute)
        self.move_market_dimension_to_row()
        logger.info("Sheet M ready — %r", name)
        return name

    def rename_export_sheet(self, sheet_name: str | None = None) -> None:
        """Step 15 — PivotTable → Analyze → Move or Copy → rename sheet to C."""
        target = (sheet_name or self.export_sheet_name).strip()
        logger.info(
            "Step 15 — %r → %r → %r, set name %r…",
            self.pivot_table_menu_label,
            self.analyze_menu_text,
            self.move_or_copy_menu_text,
            target,
        )
        self.rename_pivot_sheet(target)
        logger.info("Step 15 complete — sheet renamed to %r", target)

    def _product_begins_with_term(self, product: str) -> str:
        """First word of PRODUCT from TSV — used with Begins-with member filter."""
        product = product.strip()
        if not product:
            return product
        return product.split()[0]

    def _filter_condition_dialog_page_locator(self):
        """Filter Condition Settings dialog — any dimension, pierces iframes."""
        return (
            self.page.locator("div, table, form")
            .filter(has_text=self.filter_condition_dialog_title)
            .filter(has_text="Filter Method")
            .last
        )

    def _designer_filter_dialog_locator(self):
        """Filter Condition Settings for Product — pierces all iframes from page."""
        return (
            self.page.locator("div, table, form")
            .filter(has_text=self.filter_condition_dialog_title)
            .filter(has_text=re.compile(r"Dimension:\s*Product", re.I))
            .last
        )

    def _product_member_filter_frame(self):
        """Member-tree iframe inside Filter Condition Settings (Product dimension)."""
        for frame in self._walk_page_frames():
            url = (frame.url or "").lower()
            name = (frame.name or "").lower()
            if "product.product" in url or "product.product" in name:
                return frame
            if "%5bproduct.product%5d" in url or "[product.product]" in name:
                return frame
        return None

    def _locate_filter_dialog_all_row(self) -> dict | None:
        """Find All row coords in the Product member-tree iframe."""
        member_frame = self._product_member_filter_frame()
        if member_frame:
            try:
                label = member_frame.get_by_text("All", exact=True).first
                if label.is_visible(timeout=1_000):
                    box = label.bounding_box()
                    if box:
                        return {
                            "x": box["x"],
                            "y": box["y"],
                            "w": box["width"],
                            "h": box["height"],
                        }
            except PlaywrightTimeoutError:
                pass
            except Exception:
                pass
            try:
                coords = member_frame.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const el of document.querySelectorAll(
                        'span, td, nobr, label, a, div'
                    )) {
                        if (trim(el.textContent) !== 'All') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        return { x: r.left, y: r.top, w: r.width, h: r.height };
                    }
                    return null;
                }
                """
                )
            except Exception:
                coords = None
            if coords:
                # iframe-local coords → convert via label bounding_box when possible
                try:
                    label = member_frame.get_by_text("All", exact=True).first
                    box = label.bounding_box()
                    if box:
                        return {
                            "x": box["x"],
                            "y": box["y"],
                            "w": box["width"],
                            "h": box["height"],
                        }
                except PlaywrightTimeoutError:
                    pass

        try:
            label = self._filter_dialog_all_label_locator()
            if label.count() > 0 and label.is_visible(timeout=500):
                box = label.bounding_box()
                if box:
                    return {
                        "x": box["x"],
                        "y": box["y"],
                        "w": box["width"],
                        "h": box["height"],
                    }
        except PlaywrightTimeoutError:
            pass
        return None

    def _filter_dialog_all_label_locator(self):
        """All label in Product member-tree iframe."""
        member_frame = self._product_member_filter_frame()
        if member_frame:
            return member_frame.get_by_text("All", exact=True).first
        return self._designer_filter_dialog_locator().get_by_text(
            "All", exact=True
        ).first

    def apply_product_row_filter(self, product: str) -> None:
        """
        O-sheet Product filter — Set Filter → uncheck All → funnel icon →
        qual Equals box → Begins with → first word → Filter → OK.
        """
        product = product.strip()
        if not product:
            raise ValueError("PRODUCT value is empty in report_sources.tsv")
        begins_term = self._product_begins_with_term(product)
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        logger.info(
            "Filtering %r row — Begins with %r (from product %r)…",
            self.product_dimension,
            begins_term,
            product,
        )
        self._open_product_set_filter_dialog()
        self._wait_for_filter_all_row(timeout_ms=45_000)
        self._click_filter_all_checkbox()
        self._click_filter_all_funnel_icon()
        frame.wait_for_timeout(1_000)
        self._apply_filter_qual_begins_with(begins_term)
        frame.wait_for_timeout(1_500)
        self._click_filter_dialog_ok()
        self._wait_for_query_idle(frame)
        logger.info(
            "Product row filter applied: %r (Begins with %r)",
            product,
            begins_term,
        )

    def _walk_page_frames(self):
        """Yield every live frame in the page (main + nested iframes)."""
        stack = [self.page.main_frame]
        seen: set = set()
        while stack:
            frame = stack.pop()
            if frame in seen:
                continue
            seen.add(frame)
            try:
                if frame.is_detached():
                    continue
            except Exception:
                continue
            yield frame
            try:
                stack.extend(frame.child_frames)
            except Exception:
                continue

    def _frame_has_filter_dialog(self, frame) -> bool:
        return bool(
            self._safe_scope_evaluate(
                frame,
                """
                () => {
                    for (const el of document.querySelectorAll('div, table, form')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        if (!text.includes('Filter Method')) continue;
                        if (!text.includes('OK')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width >= 180 && r.height >= 100) return true;
                    }
                    return false;
                }
                """,
            )
        )

    def _filter_dialog_scopes(self):
        scopes = [
            frame
            for frame in self._walk_page_frames()
            if self._frame_has_filter_dialog(frame)
        ]
        if scopes:
            return scopes
        scopes = [self.page]
        try:
            designer = self._designer_frame()
            if designer not in scopes:
                scopes.append(designer)
        except PlaywrightTimeoutError:
            pass
        return scopes

    def _filter_dialog_all_row_locator(self, scope):
        dialog = self._filter_condition_dialog_locator(scope)
        return dialog.locator("tr, li, .rtLI").filter(
            has_text=re.compile(r"^All(\s|\(|$)")
        ).first

    def _wait_for_filter_all_row(self, timeout_ms: int = 45_000) -> None:
        frame = self._designer_frame()

        def ready() -> bool:
            member_frame = self._product_member_filter_frame()
            if member_frame:
                try:
                    return member_frame.get_by_text(
                        "All", exact=True
                    ).first.is_visible(timeout=400)
                except PlaywrightTimeoutError:
                    pass
            if self._locate_filter_dialog_all_row():
                return True
            try:
                return self._filter_dialog_all_label_locator().is_visible(timeout=300)
            except PlaywrightTimeoutError:
                return False

        if not self._poll_until(frame, ready, timeout_ms=timeout_ms, poll_ms=400):
            dim = self._filter_condition_dialog_dimension()
            frame_hits: list = []
            for scan_frame in self._walk_page_frames():
                try:
                    hits = scan_frame.evaluate(
                        """
                        () => {
                            const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                            const out = [];
                            for (const el of document.querySelectorAll('span, td, nobr')) {
                                const t = trim(el.textContent);
                                if (t === 'All' || t.startsWith('All ')) {
                                    const r = el.getBoundingClientRect();
                                    out.push(t.slice(0, 30) + '@' + Math.round(r.top));
                                }
                            }
                            return out.slice(0, 5);
                        }
                        """
                    )
                    if hits:
                        frame_hits.append(
                            {"url": (scan_frame.url or "")[-40:], "hits": hits}
                        )
                except Exception:
                    continue
            logger.warning(
                "All row not visible after %dms (dimension=%r, frames=%s)",
                timeout_ms,
                dim,
                frame_hits,
            )
            try:
                frame.screenshot(path="data/logs/filter_dialog_all_row_miss.png")
            except Exception:
                pass
            raise PlaywrightTimeoutError(
                "All row not visible in Filter Condition Settings (Dimension: Product)"
            )

    def _click_filter_all_checkbox(self) -> None:
        """Click checkbox left of All (layout: expand | checkbox | All | funnel)."""
        row_label = self.filter_member_search_row_label
        coords = self._locate_filter_dialog_all_row()
        if coords:
            click_x = max(4, coords["x"] - 18)
            click_y = coords["y"] + coords["h"] / 2
            self.page.mouse.click(click_x, click_y)
            frame = self._designer_frame()
            frame.wait_for_timeout(POLL_MS)
            logger.info(
                "Clicked %r checkbox at (%.0f, %.0f) via row scan",
                row_label,
                click_x,
                click_y,
            )
            return

        label = self._filter_dialog_all_label_locator()
        label.wait_for(state="visible", timeout=10_000)
        label.scroll_into_view_if_needed()
        box = label.bounding_box()
        if not box:
            raise PlaywrightTimeoutError(
                f"Could not locate {row_label!r} row in filter dialog"
            )
        click_x = max(4, box["x"] - 18)
        click_y = box["y"] + box["height"] / 2
        self.page.mouse.click(click_x, click_y)
        frame = self._designer_frame()
        frame.wait_for_timeout(POLL_MS)
        logger.info(
            "Clicked %r checkbox at (%.0f, %.0f) left of label",
            row_label,
            click_x,
            click_y,
        )

    def _click_filter_all_funnel_icon(self) -> None:
        """Click blue funnel icon immediately right of the All row label."""
        row_label = self.filter_member_search_row_label
        coords = self._locate_filter_dialog_all_row()
        if coords:
            click_x = coords["x"] + coords["w"] + 14
            click_y = coords["y"] + coords["h"] / 2
            self.page.mouse.click(click_x, click_y)
            frame = self._designer_frame()
            frame.wait_for_timeout(SLOW_POLL_MS)
            logger.info(
                "Clicked funnel beside %r at (%.0f, %.0f) via row scan",
                row_label,
                click_x,
                click_y,
            )
            return

        label = self._filter_dialog_all_label_locator()
        label.wait_for(state="visible", timeout=10_000)
        label.scroll_into_view_if_needed()
        box = label.bounding_box()
        if not box:
            raise PlaywrightTimeoutError(
                f"Could not locate {row_label!r} funnel in filter dialog"
            )
        click_x = box["x"] + box["width"] + 14
        click_y = box["y"] + box["height"] / 2
        self.page.mouse.click(click_x, click_y)
        frame = self._designer_frame()
        frame.wait_for_timeout(SLOW_POLL_MS)
        logger.info(
            "Clicked funnel beside %r at (%.0f, %.0f)",
            row_label,
            click_x,
            click_y,
        )

    def _wait_for_begins_with_filter_results(self, begins_term: str) -> None:
        """Wait until member list shows Begins-with matches (e.g. COSOME, COSOME-E)."""
        begins_term = begins_term.strip()
        frame = self._designer_frame()
        needle = begins_term.upper()

        def ready() -> bool:
            for scope in self._filter_dialog_scopes():
                found = scope.evaluate(
                    """
                    ([needle]) => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const norm = (s) => trim(s).toUpperCase();
                        let dialog = null;
                        for (const el of document.querySelectorAll('div, table')) {
                            const text = el.textContent || '';
                            if (!text.includes('Filter Condition Settings')) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width > 200 && r.height > 120) {
                                dialog = el;
                                break;
                            }
                        }
                        if (!dialog) return false;
                        const body = dialog.textContent || '';
                        if (body.toUpperCase().includes('BEGINS WITH ' + needle)) {
                            return true;
                        }
                        for (const el of dialog.querySelectorAll(
                            'span, td, label, nobr'
                        )) {
                            const t = norm(el.textContent);
                            if (t === needle || t.startsWith(needle)) return true;
                        }
                        return false;
                    }
                    """,
                    [needle],
                )
                if found:
                    return True
            return False

        if not self._poll_until(frame, ready, timeout_ms=12_000, poll_ms=250):
            logger.warning(
                "Begins-with results for %r not confirmed — continuing",
                begins_term,
            )
        else:
            logger.info("Begins-with filter results visible for %r", begins_term)

    def _ensure_filter_all_unchecked(self) -> None:
        """Uncheck the top-level All row in Filter Condition Settings."""
        self._uncheck_all_in_filter_dialog()
        for scope in self._filter_dialog_scopes():
            unchecked = scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let dialog = null;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 200 && r.height > 120) {
                            dialog = el;
                            break;
                        }
                    }
                    if (!dialog) return false;

                    for (const row of dialog.querySelectorAll('tr, li, .rtLI')) {
                        let allCell = null;
                        for (const cell of row.querySelectorAll(
                            'td, span, label, nobr'
                        )) {
                            if (trim(cell.textContent) !== 'All') continue;
                            allCell = cell;
                            break;
                        }
                        if (!allCell) continue;
                        const cb = row.querySelector('input[type="checkbox"]');
                        if (cb?.checked) {
                            (cb || allCell).click();
                            return true;
                        }
                        return false;
                    }
                    return false;
                }
                """
            )
            if unchecked:
                scope.wait_for_timeout(POLL_MS)
                logger.info("Unchecked 'All' in filter dialog")
                return

    def _click_filter_row_funnel_icon(self, row_label: str) -> None:
        """Click the funnel/search icon beside a row (e.g. All) in Set Filter dialog."""
        row_label = row_label.strip()
        frame = self._designer_frame()

        def all_row_visible() -> bool:
            for scope in self._filter_dialog_scopes():
                try:
                    dialog = self._filter_condition_dialog_locator(scope)
                    for pattern in ("All", "All (Begins"):
                        loc = dialog.get_by_text(pattern, exact=False)
                        if loc.count() > 0 and loc.first.is_visible(timeout=200):
                            return True
                except PlaywrightTimeoutError:
                    continue
            return False

        self._poll_until(frame, all_row_visible, timeout_ms=15_000, poll_ms=200)

        for scope in self._filter_dialog_scopes():
            try:
                row = self._filter_dialog_all_row_locator(scope)
                row.wait_for(state="visible", timeout=3_000)
                row.scroll_into_view_if_needed()
                imgs = row.locator("img")
                for index in range(imgs.count()):
                    img = imgs.nth(index)
                    try:
                        if not img.is_visible(timeout=200):
                            continue
                        src = (img.get_attribute("src") or "").lower()
                        alt = (img.get_attribute("alt") or "").lower()
                        if (
                            "minus" in src
                            or "plus" in src
                            or "expand" in alt
                            or "collapse" in alt
                        ):
                            continue
                        img.click(force=True, timeout=3_000)
                        scope.wait_for_timeout(SLOW_POLL_MS)
                        logger.info(
                            "Clicked funnel icon beside %r (Playwright img #%d)",
                            row_label,
                            index,
                        )
                        return
                    except PlaywrightTimeoutError:
                        continue
                label = row.get_by_text(re.compile(r"^All(\s|\(|$)")).first
                box = label.bounding_box()
                if box:
                    scope.mouse.click(
                        box["x"] + box["width"] + 14,
                        box["y"] + box["height"] / 2,
                    )
                    scope.wait_for_timeout(SLOW_POLL_MS)
                    logger.info(
                        "Clicked funnel beside %r via frame mouse coords",
                        row_label,
                    )
                    return
            except PlaywrightTimeoutError:
                pass

        for scope in self._filter_dialog_scopes():
            clicked = scope.evaluate(
                """
                ([rowLabel]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let dialog = null;
                    let bestArea = Infinity;
                    for (const el of document.querySelectorAll('div, table, form')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        if (!text.includes('Filter Method')) continue;
                        if (!text.includes('OK')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 180 || r.height < 100) continue;
                        const area = r.width * r.height;
                        if (area < bestArea) {
                            bestArea = area;
                            dialog = el;
                        }
                    }
                    if (!dialog) return false;

                    const rowCells = (row) =>
                        Array.from(row.querySelectorAll('td, span, label, nobr'));
                    const rowExactLabel = (row, labelText) => {
                        for (const cell of rowCells(row)) {
                            const t = trim(cell.textContent);
                            if (t === labelText) return cell;
                            if (
                                labelText === 'All'
                                && (t.startsWith('All ') || t.startsWith('All('))
                            ) {
                                return cell;
                            }
                        }
                        return null;
                    };

                    for (const row of dialog.querySelectorAll('tr, li, .rtLI')) {
                        const cell = rowExactLabel(row, rowLabel);
                        if (!cell) continue;
                        const cellRect = cell.getBoundingClientRect();
                        const imgs = Array.from(row.querySelectorAll('img'));
                        let best = null;
                        let bestDist = Infinity;
                        for (const img of imgs) {
                            const r = img.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            const src = (img.src || '').toLowerCase();
                            const alt = (img.alt || '').toLowerCase();
                            const title = (img.title || '').toLowerCase();
                            if (
                                src.includes('minus')
                                || src.includes('plus')
                                || alt.includes('expand')
                                || alt.includes('collapse')
                            ) {
                                continue;
                            }
                            const isFunnel =
                                src.includes('filter')
                                || src.includes('funnel')
                                || alt.includes('filter')
                                || title.includes('filter');
                            if (isFunnel && r.left >= cellRect.left) {
                                best = img;
                                break;
                            }
                            if (r.left >= cellRect.right - 8) {
                                const dist = Math.abs(
                                    (r.left + r.width / 2)
                                    - (cellRect.right + 10)
                                );
                                if (dist < bestDist) {
                                    bestDist = dist;
                                    best = img;
                                }
                            }
                        }
                        if (best) {
                            best.scrollIntoView({ block: 'center', inline: 'nearest' });
                            best.click();
                            return true;
                        }
                        const clickX = cellRect.right + 12;
                        const clickY = cellRect.top + cellRect.height / 2;
                        const target = document.elementFromPoint(clickX, clickY);
                        if (target) {
                            target.click();
                            return true;
                        }
                        return false;
                    }
                    return false;
                }
                """,
                [row_label],
            )
            if clicked:
                scope.wait_for_timeout(SLOW_POLL_MS)
                logger.info(
                    "Clicked funnel icon beside %r in filter dialog", row_label
                )
                return

        for scope in self._filter_dialog_scopes():
            try:
                dialog = self._filter_condition_dialog_locator(scope)
                all_label = dialog.get_by_text(row_label, exact=True).first
                all_label.wait_for(state="visible", timeout=2_000)
                row = all_label.locator(
                    "xpath=ancestor::tr[1] | ancestor::li[1] | "
                    "ancestor::*[contains(@class,'rtLI')][1]"
                )
                box = all_label.bounding_box()
                if box:
                    page = frame.page
                    page.mouse.click(box["x"] + box["width"] + 14, box["y"] + box["height"] / 2)
                    frame.wait_for_timeout(SLOW_POLL_MS)
                    logger.info(
                        "Clicked beside %r label in filter dialog (coords)",
                        row_label,
                    )
                    return
                imgs = row.locator("img")
                for index in range(imgs.count()):
                    img = imgs.nth(index)
                    try:
                        if img.is_visible(timeout=200):
                            img.click(timeout=3_000)
                            logger.info(
                                "Clicked funnel img #%d beside %r",
                                index,
                                row_label,
                            )
                            return
                    except PlaywrightTimeoutError:
                        continue
            except PlaywrightTimeoutError:
                continue

        raise PlaywrightTimeoutError(
            f"Could not click funnel icon beside {row_label!r} in "
            f"{self.filter_condition_dialog_title!r} dialog"
        )

    def _find_qual_operator_select(self, scope):
        """Equals/Begins-with operator dropdown in the qual row (not All funnel)."""
        for index in range(scope.locator("select").count()):
            sel = scope.locator("select").nth(index)
            try:
                options = sel.evaluate(
                    "el => [...el.options].map(o => (o.textContent || '').trim())"
                )
            except Exception:
                continue
            if any("Begins" in opt for opt in options):
                return sel
            if "Equals" in options:
                return sel
        return None

    def _scope_has_qual_operator_row(self, scope) -> bool:
        """True when qual row is visible (after All-row funnel click)."""
        try:
            has_filter = (
                scope.get_by_role("button", name="Filter").count() > 0
                or scope.locator(
                    "input[type='button'][value='Filter'], "
                    "input[type='submit'][value='Filter']"
                ).count()
                > 0
            )
            if not has_filter:
                return False
            if self._find_qual_operator_select(scope) is not None:
                return True
            for label in ("Equals", "--"):
                if scope.get_by_text(label, exact=True).count() > 0:
                    return True
        except Exception:
            pass
        return False

    def _filter_qual_operator_row_visible(self) -> bool:
        """True when qual row (Equals/-- + Filter) is visible after funnel click."""
        for scan_frame in self._walk_page_frames():
            if self._scope_has_qual_operator_row(scan_frame):
                return True

        try:
            dialog = self._filter_condition_dialog_page_locator()
            if dialog.count() > 0 and self._scope_has_qual_operator_row(dialog):
                return True
        except Exception:
            pass
        return False

    def _click_qual_operator_box(self, scope, operator: str) -> bool:
        """Open qual operator dropdown (Equals or --), not the All-row funnel."""
        frame = self._designer_frame()
        qual_sel = self._find_qual_operator_select(scope)
        if qual_sel is not None:
            try:
                qual_sel.scroll_into_view_if_needed()
                qual_sel.click(timeout=3_000)
                frame.wait_for_timeout(500)
                logger.info("Clicked qual operator select")
                return True
            except PlaywrightTimeoutError:
                pass

        for label in ("Equals", "--"):
            try:
                loc = scope.get_by_text(label, exact=True)
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=3_000)
                frame.wait_for_timeout(500)
                logger.info("Clicked qual operator box %r", label)
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    def _apply_qual_on_dialog_locator(
        self, dialog, product: str, operator: str
    ) -> bool:
        """Qual box → Begins with → text → Filter via page dialog locator."""
        frame = self._designer_frame()
        if not self._click_qual_operator_box(dialog, operator):
            return False

        qual_sel = self._find_qual_operator_select(dialog)
        selected = False
        if qual_sel is not None:
            for alt in (operator, "Begins with", "Begins With"):
                try:
                    qual_sel.select_option(label=alt, timeout=2_000)
                    logger.info("Selected %r in qual operator dropdown", alt)
                    selected = True
                    break
                except PlaywrightTimeoutError:
                    continue
        if not selected:
            for alt in (operator, "Begins with", "Begins With"):
                try:
                    dialog.get_by_text(alt, exact=True).last.click(timeout=2_000)
                    logger.info("Clicked %r in qual operator list", alt)
                    selected = True
                    break
                except PlaywrightTimeoutError:
                    continue
        if not selected:
            return False

        text_input = None
        if qual_sel is not None:
            try:
                qual_box = qual_sel.bounding_box()
                if qual_box:
                    loc = dialog.locator("input[type='text']:visible")
                    best_left = float("inf")
                    for index in range(loc.count()):
                        item = loc.nth(index)
                        box = item.bounding_box()
                        if not box or box["x"] <= qual_box["x"]:
                            continue
                        if box["y"] > qual_box["y"] + 80:
                            continue
                        if box["x"] < best_left:
                            best_left = box["x"]
                            text_input = item
            except Exception:
                pass
        if text_input is None:
            loc = dialog.locator("input[type='text']:visible")
            if loc.count() == 0:
                return False
            text_input = loc.last

        text_input.fill(product)
        filter_btn = dialog.get_by_role("button", name="Filter")
        if filter_btn.count() == 0:
            filter_btn = dialog.locator(
                "input[type='button'][value='Filter'], "
                "input[type='submit'][value='Filter']"
            )
        if filter_btn.count() == 0:
            return False
        filter_btn.first.click(timeout=5_000)
        frame.wait_for_timeout(500)
        logger.info("Applied %r filter for %r via qual row", operator, product)
        return True

    def _filter_operator_text_input(self, scope):
        """Text input beside the qual Equals dropdown (purple condition row)."""
        qual_sel = self._find_qual_operator_select(scope)
        if qual_sel is not None:
            try:
                qual_box = qual_sel.bounding_box()
                if qual_box:
                    loc = scope.locator("input[type='text']:visible")
                    best = None
                    best_left = float("inf")
                    for index in range(loc.count()):
                        item = loc.nth(index)
                        box = item.bounding_box()
                        if not box:
                            continue
                        if box["x"] <= qual_box["x"]:
                            continue
                        if box["y"] > qual_box["y"] + 80:
                            continue
                        if box["x"] < best_left:
                            best_left = box["x"]
                            best = item
                    if best is not None:
                        return best
            except Exception:
                pass

        for selector in (
            "input[type='text']:visible",
            "input.rcbInput:visible",
            "textarea:visible",
        ):
            loc = scope.locator(selector)
            if loc.count() > 0:
                return loc.first
        return None

    def _select_begins_with_operator(self, scope, operator: str) -> bool:
        """Click qual box (Equals/--), then pick Begins with."""
        frame = self._designer_frame()
        if not self._click_qual_operator_box(scope, operator):
            return False

        qual_sel = self._find_qual_operator_select(scope)
        if qual_sel is not None:
            for alt in (operator, "Begins with", "Begins With"):
                try:
                    qual_sel.select_option(label=alt, timeout=2_000)
                    logger.info("Selected %r in qual operator dropdown", alt)
                    return True
                except PlaywrightTimeoutError:
                    try:
                        scope.get_by_text(alt, exact=True).last.click(timeout=2_000)
                        logger.info("Clicked %r in qual operator list", alt)
                        return True
                    except PlaywrightTimeoutError:
                        continue

        for alt in (operator, "Begins with", "Begins With"):
            try:
                scope.get_by_text(alt, exact=True).last.click(timeout=3_000)
                logger.info("Clicked %r after opening qual operator box", alt)
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    def _apply_equals_begins_with_in_scope(
        self, scope, product: str, operator: str
    ) -> bool:
        """Equals → Begins with → type term → Filter in one dialog scope."""
        if not self._select_begins_with_operator(scope, operator):
            applied = scope.evaluate(
                """
                ([operator, product]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let select = null;
                    for (const sel of document.querySelectorAll('select')) {
                        const opts = [...sel.options].map((o) =>
                            trim(o.textContent)
                        );
                        if (opts.includes('Equals')
                            && opts.some((o) => o.includes('Begins'))) {
                            select = sel;
                            break;
                        }
                    }
                    if (select) {
                        select.click();
                        for (const opt of select.options) {
                            const t = trim(opt.textContent);
                            if (t === operator || t === 'Begins with') {
                                select.value = opt.value;
                                select.dispatchEvent(
                                    new Event('change', { bubbles: true })
                                );
                                break;
                            }
                        }
                    } else {
                        for (const el of document.querySelectorAll(
                            'span, td, div, option, li'
                        )) {
                            if (trim(el.textContent) !== 'Equals') continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            el.click();
                            break;
                        }
                        for (const el of document.querySelectorAll(
                            'option, li, div, span'
                        )) {
                            const t = trim(el.textContent);
                            if (t !== operator && t !== 'Begins with') continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            el.click();
                            break;
                        }
                    }

                    let filterBtn = null;
                    for (const btn of document.querySelectorAll(
                        'input[type="button"], input[type="submit"], button'
                    )) {
                        const label = trim(btn.value || btn.textContent);
                        if (label !== 'Filter') continue;
                        const r = btn.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        filterBtn = btn;
                        break;
                    }
                    if (!filterBtn) return false;
                    const btnRect = filterBtn.getBoundingClientRect();

                    const candidates = [];
                    for (const inp of document.querySelectorAll('input')) {
                        const type = (inp.type || 'text').toLowerCase();
                        if (type !== 'text' && type !== 'search' && type !== '') {
                            continue;
                        }
                        const r = inp.getBoundingClientRect();
                        if (r.width <= 40 || r.height <= 0) continue;
                        if (r.bottom <= btnRect.top + 30) {
                            candidates.push({ inp, left: r.left, top: r.top });
                        }
                    }
                    candidates.sort((a, b) => a.left - b.left || a.top - b.top);
                    const textInput = candidates[0]?.inp || null;
                    if (!textInput) return false;

                    textInput.focus();
                    textInput.value = product;
                    textInput.dispatchEvent(
                        new Event('input', { bubbles: true })
                    );
                    textInput.dispatchEvent(
                        new Event('change', { bubbles: true })
                    );
                    filterBtn.click();
                    return true;
                }
                """,
                [operator, product],
            )
            if not applied:
                return False
            logger.info("Applied %r filter for %r via JS fallback", operator, product)
            return True

        text_input = self._filter_operator_text_input(scope)
        if text_input is None:
            return False
        text_input.fill(product)
        filter_btn = scope.get_by_role("button", name="Filter")
        if filter_btn.count() == 0:
            filter_btn = scope.locator(
                "input[type='button'][value='Filter'], "
                "input[type='submit'][value='Filter']"
            )
        if filter_btn.count() == 0:
            return False
        filter_btn.first.click(timeout=5_000)
        logger.info("Applied %r filter for %r via operator row", operator, product)
        return True

    def _apply_filter_qual_begins_with(self, product: str) -> None:
        """Click qual Equals box → Begins with → type first word → Filter."""
        product = product.strip()
        frame = self._designer_frame()
        operator = self.begins_with_filter_operator

        if not self._poll_until(
            frame, self._filter_qual_operator_row_visible, timeout_ms=20_000, poll_ms=300
        ):
            raise PlaywrightTimeoutError(
                "Filter qual operator row not visible after All-row funnel click"
            )

        dialog = self._filter_condition_dialog_page_locator()
        try:
            if self._apply_qual_on_dialog_locator(dialog, product, operator):
                frame.wait_for_timeout(1_000)
                return
        except PlaywrightTimeoutError:
            pass

        frames_to_try: list = []
        member_frame = self._product_member_filter_frame()
        if member_frame:
            frames_to_try.append(member_frame)
        frames_to_try.extend(
            f for f in self._walk_page_frames() if f not in frames_to_try
        )

        for scope in frames_to_try:
            try:
                if self._apply_equals_begins_with_in_scope(
                    scope, product, operator
                ):
                    frame.wait_for_timeout(1_000)
                    return
            except PlaywrightTimeoutError:
                continue

        raise PlaywrightTimeoutError(
            f"Could not apply {operator!r} filter for {product!r}"
        )

    def _check_product_in_filter_dialog(self, product: str) -> None:
        """Check only our product row in Filter Condition Settings after Begins with."""
        product = product.strip()
        frame = self._designer_frame()

        def member_visible() -> bool:
            for scope in self._filter_dialog_scopes():
                found = scope.evaluate(
                    """
                    ([product]) => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const norm = (s) => trim(s).toUpperCase();
                        const target = norm(product);
                        let dialog = null;
                        for (const el of document.querySelectorAll('div, table')) {
                            const text = el.textContent || '';
                            if (!text.includes('Filter Condition Settings')) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width > 200 && r.height > 120) {
                                dialog = el;
                                break;
                            }
                        }
                        if (!dialog) return false;
                        for (const el of dialog.querySelectorAll(
                            'span, td, label, nobr'
                        )) {
                            const t = norm(el.textContent);
                            if (t !== target && !t.startsWith(target)) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            return true;
                        }
                        return false;
                    }
                    """,
                    [product],
                )
                if found:
                    return True
            return False

        self._poll_until(frame, member_visible, timeout_ms=10_000, poll_ms=200)

        for scope in self._filter_dialog_scopes():
            if self._toggle_filter_dialog_row(scope, product):
                logger.info("Checked product %r in filter dialog", product)
                return
            if self._check_market_in_filter_dialog(product):
                logger.info("Checked product %r in filter dialog", product)
                return
            result = scope.evaluate(
                """
                ([product]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const norm = (s) => trim(s).toUpperCase();
                    const target = norm(product);
                    let dialog = null;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 200 && r.height > 120) {
                            dialog = el;
                            break;
                        }
                    }
                    if (!dialog) return false;

                    for (const row of dialog.querySelectorAll('tr, li, .rtLI')) {
                        let matched = false;
                        for (const cell of row.querySelectorAll(
                            'td, span, label, nobr'
                        )) {
                            const t = norm(cell.textContent);
                            if (t === target || t.startsWith(target)) {
                                matched = true;
                                break;
                            }
                        }
                        if (!matched) continue;
                        const cb = row.querySelector('input[type="checkbox"]');
                        const hit = cb || row;
                        hit.scrollIntoView({ block: 'center', inline: 'nearest' });
                        hit.click();
                        return true;
                    }
                    return false;
                }
                """,
                [product],
            )
            if result:
                logger.info("Checked product %r in filter dialog (JS)", product)
                return

        raise PlaywrightTimeoutError(
            f"Could not select product {product!r} in "
            f"{self.filter_condition_dialog_title!r} dialog"
        )

    def _filter_condition_dialog_dimension(self) -> str | None:
        """Read 'Dimension: …' from the open Filter Condition Settings dialog."""
        find_dim_js = """
        () => {
            const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            for (const el of document.querySelectorAll(
                'span, td, div, label, nobr, th'
            )) {
                const t = trim(el.textContent);
                if (t.startsWith('Dimension:')) {
                    return t.slice('Dimension:'.length).trim();
                }
            }
            const body = document.body?.innerText || '';
            const m = body.match(/Dimension:\\s*([^\\n\\r]+)/i);
            return m ? trim(m[1]) : null;
        }
        """
        for frame in self._walk_page_frames():
            try:
                dim = frame.evaluate(find_dim_js)
            except Exception:
                dim = None
            if dim:
                return str(dim).strip()
        return None

    def _wait_for_filter_condition_dialog_for_dimension(
        self, dimension: str, *, timeout_ms: int = 20_000
    ) -> None:
        """Wait until Filter Condition Settings is open for the expected dimension."""
        dimension = dimension.strip()
        frame = self._designer_frame()

        def ready() -> bool:
            if not self._filter_condition_dialog_open():
                return False
            dim = self._filter_condition_dialog_dimension()
            if dim and dim.lower() == dimension.lower():
                return True
            for scope in self._filter_dialog_scopes():
                try:
                    ok = scope.evaluate(
                        """
                        ([dimName]) => {
                            for (const el of document.querySelectorAll(
                                'div, table'
                            )) {
                                const t = el.textContent || '';
                                if (!t.includes('Filter Condition Settings')) {
                                    continue;
                                }
                                if (!t.includes('Set Filter Member')) continue;
                                if (!t.includes('All')) continue;
                                if (t.includes('Dimension:')
                                    && t.includes(dimName)) {
                                    return true;
                                }
                                if (dimName === 'Product' && !/\\b1\\d{3}\\b/.test(t)) {
                                    return true;
                                }
                                if (dimName !== 'Product' && t.includes(dimName)) {
                                    return true;
                                }
                            }
                            return false;
                        }
                        """,
                        [dimension],
                    )
                except Exception:
                    ok = False
                if ok:
                    return True
            return False

        if self._poll_until(frame, ready, timeout_ms=timeout_ms):
            logger.info(
                "%r dialog is open for dimension %r",
                self.filter_condition_dialog_title,
                dimension,
            )
            return
        if self._filter_condition_dialog_open():
            logger.info(
                "%r dialog open (dimension label not read) — continuing as %r",
                self.filter_condition_dialog_title,
                dimension,
            )
            return
        actual = self._filter_condition_dialog_dimension()
        raise PlaywrightTimeoutError(
            f"{self.filter_condition_dialog_title!r} dialog dimension "
            f"expected {dimension!r}, got {actual!r}"
        )

    def _dismiss_filter_condition_dialog(self) -> None:
        frame = self._designer_frame()
        for scope in self._filter_dialog_scopes():
            for label in ("Cancel", "Close"):
                try:
                    btn = scope.get_by_role("button", name=label)
                    if btn.count() > 0 and btn.first.is_visible(timeout=200):
                        btn.first.click(timeout=3_000)
                        frame.wait_for_timeout(300)
                        return
                except PlaywrightTimeoutError:
                    pass
        try:
            frame.page.keyboard.press("Escape")
            frame.wait_for_timeout(300)
        except Exception:
            pass

    def _click_product_row_chevron(self, frame) -> str:
        """Click the Product row field chevron (member-icon) in the pivot header band."""
        clicked = frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));

                const tryClickIcon = (td) => {
                    if (!td || !isVis(td)) return false;
                    const icon = td.querySelector(
                        'img.member-icon, img[onclick*="LevelContext"], '
                        + 'img[onclick*="ContextClick"]'
                    );
                    if (icon && isVis(icon)) {
                        icon.scrollIntoView({ block: 'center', inline: 'nearest' });
                        icon.click();
                        return true;
                    }
                    const span = td.querySelector('span[title*="Product"]');
                    if (span && isVis(span)) {
                        span.click();
                        return true;
                    }
                    return false;
                };

                let bestTd = null;
                let bestLeft = -1;
                for (const td of document.querySelectorAll('td[area="rows"]')) {
                    if (inTree(td) || !isVis(td)) continue;
                    const span = td.querySelector(
                        'span[title*="Product.Product"], span[title*="[Product]"]'
                    );
                    if (!span) continue;
                    const r = td.getBoundingClientRect();
                    if (r.top < 100 || r.top > 320) continue;
                    if (r.left > bestLeft) {
                        bestLeft = r.left;
                        bestTd = td;
                    }
                }
                if (tryClickIcon(bestTd)) return 'product-field-icon';

                for (const td of document.querySelectorAll('td[area="rows"]')) {
                    if (inTree(td) || !isVis(td)) continue;
                    const span = td.querySelector('span[istail="1"][axis="r"]');
                    if (!span) continue;
                    const text = trim(span.textContent);
                    if (text !== 'Product' && !text.includes('Product')) continue;
                    const r = td.getBoundingClientRect();
                    if (r.top < 100 || r.top > 320) continue;
                    if (tryClickIcon(td)) return 'product-istail-icon';
                }

                let best = null;
                for (const el of document.body.querySelectorAll('nobr, span, td')) {
                    const text = trim(el.textContent);
                    if (text !== 'Product' && !text.startsWith('Product (')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top < 100 || r.top > 320) {
                        continue;
                    }
                    if (!best || r.left > best.left) {
                        best = { el, left: r.left };
                    }
                }
                if (best) {
                    const cell = best.el.closest('td[area="rows"], td, th') || best.el;
                    if (tryClickIcon(cell)) return 'product-text-icon';
                    const row = cell.closest('tr') || cell.parentElement;
                    const icon = row?.querySelector(
                        'img.member-icon, img[onclick*="LevelContext"]'
                    );
                    if (icon && isVis(icon)) {
                        icon.click();
                        return 'product-row-icon';
                    }
                }
                return null;
            }
            """
        )
        if not clicked:
            raise PlaywrightTimeoutError(
                f"Could not click {self.product_dimension!r} row chevron in pivot"
            )
        frame.wait_for_timeout(400)
        logger.info(
            "Clicked %r row chevron (%s)",
            self.product_dimension,
            clicked,
        )
        return clicked

    def _open_product_set_filter_dialog(self) -> None:
        """Product row chevron → Set Filter… → Filter Condition Settings (Dimension: Product)."""
        frame = self._designer_frame()
        product = self.product_dimension

        for attempt in range(1, 6):
            self._clear_open_popups(frame)
            self._click_product_row_chevron(frame)

            if not self._poll_until(
                frame, self._pivot_row_filter_menu_open, timeout_ms=2_500
            ):
                logger.info(
                    "%r filter menu not open after chevron (attempt %d)",
                    product,
                    attempt,
                )
                continue

            if not self._set_filter_menu_open(frame):
                try:
                    self._open_filter_submenu_if_needed()
                except PlaywrightTimeoutError:
                    logger.info(
                        "Filter submenu missing for %r (attempt %d)", product, attempt
                    )
                    continue

            self._click_set_filter_menu()
            frame.wait_for_timeout(1_200)
            try:
                self._wait_for_filter_condition_dialog(timeout_ms=20_000)
            except PlaywrightTimeoutError as exc:
                logger.warning(
                    "Set Filter dialog not ready (attempt %d): %s",
                    attempt,
                    exc,
                )
                self._dismiss_filter_condition_dialog()
                frame.wait_for_timeout(400)
                continue

            actual = self._filter_condition_dialog_dimension()
            if actual and actual.lower() != product.lower():
                logger.warning(
                    "Expected %r filter dialog, got %r — retrying (attempt %d)",
                    product,
                    actual,
                    attempt,
                )
                self._dismiss_filter_condition_dialog()
                frame.wait_for_timeout(400)
                continue
            return

        raise PlaywrightTimeoutError(
            f"Could not open Set Filter for {product!r} row "
            f"(last dimension: {self._filter_condition_dialog_dimension()!r})"
        )

    def _product_row_filter_applied(self, frame, product: str) -> bool:
        """True when pivot data shows the chosen product after row filter."""
        product = product.strip()
        return frame.evaluate(
            """
            ([product]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const norm = (s) => trim(s).toUpperCase();
                const target = norm(product);
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));

                for (const el of document.body.querySelectorAll(
                    'span, td, div, a, label, li, nobr'
                )) {
                    const text = trim(el.textContent);
                    if (!text || text.length > 80) continue;
                    if (inTree(el)) continue;
                    if (norm(text) !== target && !norm(text).startsWith(target)) {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top < 180) continue;
                    return true;
                }
                return false;
            }
            """,
            [product],
        )

    def apply_product_show_only_top_custom(self, count: int | None = None) -> None:
        """Step 13 — Product → Show Only the Top → Custom → Count 5, Values."""
        top_count = self.product_top_count if count is None else count
        logger.info(
            "Step 13 — %r → %r → %r, Count=%s, Based on Measure=%r…",
            self.product_dimension,
            self.show_only_top_menu_text,
            self.custom_top_menu_text,
            top_count,
            self.custom_top_based_on_measure,
        )
        self.apply_product_show_only_top(top_count, via_custom=True)
        logger.info("Step 13 complete — Product top-%s filter applied", top_count)

    def _raise_if_error_dialog(self, frame) -> None:
        """Dismiss stale-db access popups instead of aborting the automation."""
        self._dismiss_stale_db_popup_aggressively(frame)

    def _wait_for_schema_tree_ready(self, frame, timeout_ms: int = 90_000) -> None:
        """
        Wait until the schema tree root node is visible with no Loading spinners.

        We only require 'Cubes' or 'Dimensions' text to be present — Period is
        inside a collapsed Cubes folder and won't appear until we expand it.
        90-second timeout to allow large cubes (e.g. 9443) time to load.
        """
        logger.info("Waiting for schema tree to finish loading…")
        try:
            frame.wait_for_function(
                """
                () => {
                    const root = document.getElementById('trvSchema')
                        || document.querySelector('[id*="trvSchema"]');
                    if (!root) return false;
                    const text = root.innerText || '';
                    if (!/Cubes|Dimensions/.test(text)) return false;
                    return !/Loading\\.\\.\\./.test(text);
                }
                """,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            sel = self._catalog_select(frame)
            catalog = (
                sel.evaluate(
                    "el => (el.options[el.selectedIndex]?.textContent || '').trim()"
                )
                if sel is not None
                else "unknown"
            )
            raise PlaywrightTimeoutError(
                "Schema tree did not load — check Database/Catalog selection "
                f"(select shows {catalog!r})"
            ) from exc
        logger.info("Schema tree ready")

    def _wait_for_tree_loaded(self, frame, timeout_ms: int = 60_000) -> None:
        for label in (self.cubes_folder, self.dimensions_folder):
            try:
                frame.get_by_text(label, exact=True).first.wait_for(
                    state="visible",
                    timeout=timeout_ms // 2,
                )
                return
            except PlaywrightTimeoutError:
                continue
        raise PlaywrightTimeoutError(
            f"Left menu tree did not load (expected {self.cubes_folder!r} or "
            f"{self.dimensions_folder!r})"
        )

    def _try_click_tree_node(self, frame, label: str, *, pick_last: bool = False) -> bool:
        try:
            loc = frame.get_by_text(label, exact=True)
            target = loc.last if pick_last else loc.first
            target.scroll_into_view_if_needed()
            target.click(timeout=5_000)
            return True
        except PlaywrightTimeoutError:
            pass

        result = frame.evaluate(
            """
            ([text, pickLast]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]')
                    || document.body;
                const hits = [];
                for (const el of root.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    const nodeText = trim(el.textContent);
                    if (nodeText !== text || nodeText.includes('Loading')) continue;
                    if (!isVisible(el)) continue;
                    hits.push(el);
                }
                if (!hits.length) return false;
                const target = pickLast ? hits[hits.length - 1] : hits[0];
                target.scrollIntoView({ block: 'center' });
                target.click();
                return true;
            }
            """,
            [label, pick_last],
        )
        return bool(result)

    def _click_tree_node(self, frame, label: str, *, pick_last: bool = False) -> None:
        if not self._try_click_tree_node(frame, label, pick_last=pick_last):
            hints = self._tree_node_hints(frame, label)
            raise PlaywrightTimeoutError(
                f"Could not click tree node {label!r} (similar: {hints})"
            )
        logger.info("Clicked tree node %r", label)

    def _collect_visible_tree_nodes(self, frame) -> list[dict]:
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]')
                    || document.body;

                const nodes = [];
                for (const el of root.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    const raw = trim(el.textContent);
                    if (!raw || raw.includes('Loading')) continue;
                    if (raw.length > 80) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    nodes.push({
                        text: raw,
                        top: r.top,
                        left: r.left,
                        x: r.x + r.width / 2,
                        y: r.y + r.height / 2,
                    });
                }
                return nodes;
            }
            """
        )

    def _pick_tree_node(
        self,
        nodes: list[dict],
        label: str,
        *,
        shallowest: bool = False,
        deepest: bool = False,
    ) -> dict | None:
        hits = [node for node in nodes if node["text"] == label]
        if not hits:
            return None
        if deepest:
            return max(hits, key=lambda node: (node["left"], node["top"]))
        if shallowest:
            return min(hits, key=lambda node: (node["left"], node["top"]))
        return min(hits, key=lambda node: (node["top"], node["left"]))

    def _find_tree_child_under_parent(
        self,
        frame,
        parent_label: str,
        child_label: str,
        *,
        grandparent_label: str | None = None,
    ) -> dict | None:
        nodes = self._collect_visible_tree_nodes(frame)
        if grandparent_label:
            grandparent = self._pick_tree_node(
                nodes, grandparent_label, shallowest=True
            )
            if not grandparent:
                return None
            parent_candidates = [
                node
                for node in nodes
                if node["text"] == parent_label
                and node["top"] > grandparent["top"] + 4
                and abs(node["left"] - grandparent["left"]) <= 64
            ]
            if not parent_candidates:
                return None
            parent = min(
                parent_candidates, key=lambda node: (node["top"], node["left"])
            )
        else:
            parent = self._pick_tree_node(nodes, parent_label, shallowest=True)
        if not parent:
            return None

        sibling_boundary = parent["top"] + 600
        for node in nodes:
            if node["text"] == parent_label:
                continue
            if node["top"] <= parent["top"]:
                continue
            if abs(node["left"] - parent["left"]) > 24:
                continue
            if node["left"] <= parent["left"] + 4:
                sibling_boundary = min(sibling_boundary, node["top"])
                continue
            break

        children = [
            node
            for node in nodes
            if node["text"] == child_label
            and node["top"] > parent["top"]
            and node["top"] < sibling_boundary
        ]
        if not children:
            children = [
                node
                for node in nodes
                if node["text"] == child_label
                and node["top"] > parent["top"]
                and node["left"] >= parent["left"]
            ]
        if not children:
            return None
        return min(children, key=lambda node: (node["top"], node["left"]))

    def _wait_for_tree_child_under_parent(
        self,
        frame,
        parent_label: str,
        child_label: str,
        timeout_ms: int = 15_000,
        *,
        grandparent_label: str | None = None,
    ) -> dict | None:
        for _ in range(timeout_ms // 300):
            child = self._find_tree_child_under_parent(
                frame,
                parent_label,
                child_label,
                grandparent_label=grandparent_label,
            )
            if child:
                return child
            frame.wait_for_timeout(300)
        return None

    def _click_tree_expand_toggle(
        self, frame, label: str, *, shallowest: bool = True
    ) -> bool:
        """Click the > chevron on the same row as a tree label."""
        return self._click_tree_row_chevron(
            frame, label, deepest=not shallowest
        )

    def _expand_tree_node_at(self, frame, node: dict) -> None:
        """Click the expand chevron for an already-located tree node."""
        page = frame.page
        page.mouse.click(max(4, node["x"] - 14), node["y"])
        frame.wait_for_timeout(500)

    def _expand_tree_node(
        self, frame, label: str, *, shallowest: bool = True
    ) -> None:
        """Expand a tree node using its dropdown chevron, or the label as fallback."""
        if not self._click_tree_expand_toggle(frame, label, shallowest=shallowest):
            self._click_tree_node_by_depth(frame, label, shallowest=shallowest)

    def _period_expand_verified(self, frame) -> bool:
        """True when Period is expanded enough for Hierarchies / Relative MAT."""
        if self._period_children_visible(frame):
            return True
        if self._is_tree_child_visible(
            frame, self.period_item, self.hierarchies_folder
        ):
            return True
        try:
            period = self._dimension_anchor_locator(frame, self.period_item)
            hier = frame.get_by_text(self.hierarchies_folder, exact=True).first
            if period and hier.is_visible(timeout=150):
                p_box = period.bounding_box()
                h_box = hier.bounding_box()
                if p_box and h_box and h_box["y"] > p_box["y"] + 2:
                    return True
        except PlaywrightTimeoutError:
            pass
        return False

    def _click_period_chevron_in_schema(self, frame) -> bool:
        """Click the expand control on Period inside trvSchema only."""
        return self._click_schema_tree_chevron(
            frame,
            self.period_item,
            require_below=self.dimensions_folder,
            deepest=False,
        )

    def _period_label_boxes(self, frame) -> list[dict]:
        """Candidate Period row boxes in trvSchema (page coordinates)."""
        boxes: list[dict] = []
        tree = frame.locator("#trvSchema, [id*='trvSchema']").first
        loc = tree.get_by_text(self.period_item, exact=True)
        for idx in range(loc.count()):
            node = loc.nth(idx)
            try:
                if not node.is_visible(timeout=150):
                    continue
                box = node.bounding_box()
            except PlaywrightTimeoutError:
                continue
            if box:
                boxes.append(box)
        boxes.sort(key=lambda b: (-b["x"], b["y"]))
        return boxes

    def _ensure_period_children_visible(self, frame) -> None:
        """Expand Period chevron only — Add already loaded the cube tree."""
        if self._period_expand_verified(frame):
            return

        verify = lambda: self._period_expand_verified(frame)
        tree = frame.locator("#trvSchema, [id*='trvSchema']").first
        try:
            tree.scroll_into_view_if_needed(timeout=5_000)
        except PlaywrightTimeoutError:
            pass

        if not self._period_in_schema(frame):
            logger.info(
                "Waiting for %r in tree after Add (not expanding %r manually)…",
                self.period_item,
                self.cubes_folder,
            )
            self._wait_for_period_in_tree(frame)

        if self._expand_tree_row_by_label(
            frame,
            self.period_item,
            deepest=False,
            require_below=self.dimensions_folder,
            verify=verify,
        ):
            logger.info("Expanded %r via in-frame row chevron", self.period_item)
            return

        for label_box in self._period_label_boxes(frame):
            if self._click_chevron_for_label_box(
                frame, label_box, self.period_item, verify=verify
            ):
                logger.info("Expanded %r via chevron", self.period_item)
                return

        if self._click_period_chevron_in_schema(frame):
            if self._poll_until_ui(frame, verify, idle_timeout_ms=3_000):
                logger.info("Expanded %r via schema chevron (JS)", self.period_item)
                return

        if self._expand_tree_chevron(
            frame,
            self.period_item,
            verify,
            deepest=False,
            below_label=self.dimensions_folder,
        ):
            logger.info("Expanded %r via chevron fallback", self.period_item)
            return

        raise PlaywrightTimeoutError(
            f"Could not expand {self.period_item!r} — "
            f"{self.hierarchies_folder!r} not visible under Period"
        )

    def _click_tree_node_box(self, frame, node: dict) -> None:
        clicked = frame.evaluate(
            """
            ([targetText, x, y]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                if (!root) return false;
                for (const el of root.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== targetText || text.includes('Loading')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const cx = r.x + r.width / 2;
                    const cy = r.y + r.height / 2;
                    if (Math.abs(cx - x) > 2 || Math.abs(cy - y) > 2) continue;
                    el.scrollIntoView({ block: 'center' });
                    el.click();
                    return true;
                }
                return false;
            }
            """,
            [node["text"], node["x"], node["y"]],
        )
        if not clicked:
            raise PlaywrightTimeoutError(
                f"Could not click tree node {node['text']!r} at ({node['x']}, {node['y']})"
            )

    def _click_tree_node_by_depth(
        self, frame, label: str, *, shallowest: bool = False, deepest: bool = False
    ) -> None:
        nodes = self._collect_visible_tree_nodes(frame)
        target = self._pick_tree_node(
            nodes, label, shallowest=shallowest, deepest=deepest
        )
        if not target:
            hints = self._tree_node_hints(frame, label)
            raise PlaywrightTimeoutError(
                f"Could not click tree node {label!r} (similar: {hints})"
            )
        self._click_tree_node_box(frame, target)
        logger.info("Clicked tree node %r", label)

    def _is_tree_child_visible(
        self, frame, parent_label: str, child_label: str
    ) -> bool:
        return self._find_tree_child_under_parent(frame, parent_label, child_label) is not None

    def _click_tree_child_under_parent(
        self, frame, parent_label: str, child_label: str
    ) -> bool:
        target = self._find_tree_child_under_parent(frame, parent_label, child_label)
        if not target:
            return False
        self._click_tree_node_box(frame, target)
        logger.info("Clicked %r under %r", child_label, parent_label)
        return True

    @staticmethod
    def _matches_tree_label(text: str, item_label: str) -> bool:
        """Exact label match; allow UI ellipsis truncation, not longer siblings."""
        if text == item_label:
            return True
        trimmed = text.rstrip(".")
        return (
            len(trimmed) >= 12
            and item_label.startswith(trimmed)
            and not text.startswith(item_label + " - ")
        )

    def _find_tree_descendant_under_parent(
        self,
        frame,
        parent_label: str,
        item_label: str,
        *,
        grandparent: str | None = None,
    ) -> dict | None:
        nodes = self._collect_visible_tree_nodes(frame)
        parent_candidates = [
            node for node in nodes if node["text"] == parent_label
        ]
        if grandparent:
            grandparent_node = self._pick_tree_node(
                nodes, grandparent, shallowest=True
            )
            if not grandparent_node:
                return None
            parent_candidates = [
                node
                for node in parent_candidates
                if node["top"] > grandparent_node["top"]
                and node["left"] > grandparent_node["left"]
            ]
        if not parent_candidates:
            return None
        parent = min(parent_candidates, key=lambda node: (node["top"], node["left"]))

        descendants = [
            node
            for node in nodes
            if node["top"] > parent["top"]
            and node["left"] > parent["left"]
            and self._matches_tree_label(node["text"], item_label)
        ]
        if not descendants:
            return None
        exact = [node for node in descendants if node["text"] == item_label]
        if exact:
            return min(exact, key=lambda node: (node["top"], node["left"]))
        return min(descendants, key=lambda node: (len(node["text"]), node["top"]))

    def _drag_tree_descendant_to_drop_zone(
        self,
        frame,
        parent_label: str,
        item_label: str,
        drop_zone_text: str,
        *,
        grandparent: str | None = None,
    ) -> None:
        source = self._find_tree_descendant_under_parent(
            frame,
            parent_label,
            item_label,
            grandparent=grandparent,
        )
        if not source:
            hints = self._tree_node_hints(frame, item_label)
            raise PlaywrightTimeoutError(
                f"Could not find {item_label!r} under {parent_label!r} "
                f"(similar: {hints})"
            )
        target = self._find_visible_node_box(
            frame, drop_zone_text, partial=True
        )
        if not target:
            raise PlaywrightTimeoutError(f"Drop zone {drop_zone_text!r} not found")
        self._mouse_drag(frame, source, target)
        logger.info("Dragged %r to %r", source["text"], drop_zone_text)

    def _expand_dimension_attributes(self, frame, dimension: str) -> None:
        """Expand Dimensions → dimension → the Attributes folder directly under it."""
        if not self._is_tree_node_visible(frame, self.dimensions_folder):
            self._click_tree_node(frame, self.dimensions_folder)
            frame.wait_for_timeout(800)

        self._click_tree_node_by_depth(frame, dimension, shallowest=True)
        frame.wait_for_timeout(800)

        if not self._is_tree_child_visible(frame, dimension, self.attributes_folder):
            self._click_tree_node_by_depth(frame, dimension, shallowest=True)
            frame.wait_for_timeout(800)

        if not self._click_tree_child_under_parent(
            frame, dimension, self.attributes_folder
        ):
            hints = self._tree_node_hints(frame, self.attributes_folder)
            raise PlaywrightTimeoutError(
                f"Could not click {self.attributes_folder!r} under {dimension!r} "
                f"(similar: {hints})"
            )
        logger.info("Opened %r under %r", self.attributes_folder, dimension)
        frame.wait_for_timeout(1_000)

    def _double_click_tree_node(
        self, frame, label: str, *, pick_last: bool = False
    ) -> None:
        source = self._find_visible_node_box(frame, label, pick_last=pick_last)
        if not source:
            hints = self._tree_node_hints(frame, label)
            raise PlaywrightTimeoutError(
                f"Could not double-click tree node {label!r} (similar: {hints})"
            )
        page = frame.page
        page.mouse.dblclick(source["x"], source["y"])
        logger.info("Double-clicked tree node %r", label)

    def _double_click_tree_node_by_depth(
        self, frame, label: str, *, deepest: bool = False, shallowest: bool = False
    ) -> None:
        nodes = self._collect_visible_tree_nodes(frame)
        target = self._pick_tree_node(
            nodes, label, deepest=deepest, shallowest=shallowest
        )
        if not target:
            hints = self._tree_node_hints(frame, label)
            raise PlaywrightTimeoutError(
                f"Could not double-click tree node {label!r} (similar: {hints})"
            )
        page = frame.page
        page.mouse.dblclick(target["x"], target["y"])
        logger.info("Double-clicked tree node %r", label)

    def _wait_for_pivot_field(
        self, frame, label: str, timeout_ms: int = 30_000
    ) -> None:
        for _ in range(timeout_ms // 500):
            if self._find_visible_node_box(frame, label, partial=True):
                logger.info("PivotTable field %r is visible", label)
                return
            frame.wait_for_timeout(500)
        raise PlaywrightTimeoutError(
            f"PivotTable field {label!r} did not appear under PivotTable"
        )

    def _drag_pivot_field_to_drop_zone(
        self,
        frame,
        field_label: str,
        drop_zone_text: str,
        *,
        target_pick_last: bool = False,
    ) -> None:
        """Drag a field from the PivotTable field list into a pivot drop cell."""
        source = self._find_visible_node_box(frame, field_label, partial=True)
        target = self._find_visible_node_box(
            frame,
            drop_zone_text,
            partial=True,
            pick_last=target_pick_last,
        )
        if not source:
            raise PlaywrightTimeoutError(
                f"PivotTable field {field_label!r} not found for drag"
            )
        if not target:
            raise PlaywrightTimeoutError(
                f"Pivot drop zone {drop_zone_text!r} not found"
            )
        self._mouse_drag(frame, source, target)
        logger.info("Dragged %r to %r", field_label, drop_zone_text)

    def _drag_item_to_measure_drop_zone(self, frame, item_label: str) -> None:
        self._drag_item_to_drop_zone(
            frame,
            item_label,
            self.measure_drop_zone_text,
            partial_item=False,
            target_pick_last=True,
        )

    def _tree_node_hints(self, frame, label: str) -> list[str]:
        return frame.evaluate(
            """
            ([text]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                if (!root) return [];
                return Array.from(root.querySelectorAll('span, a, td, div, label, li'))
                    .map((el) => trim(el.textContent))
                    .filter((t) => t && t.length <= 80
                        && t.toLowerCase().includes(text.toLowerCase()))
                    .slice(0, 15);
            }
            """,
            [label],
        )

    def _expand_period_branch(self, frame, timeout_ms: int = 120_000) -> None:
        """
        Expand Cubes → (cube node) → Dimensions → Period.

        Waits up to `timeout_ms` for the Cubes or Dimensions node to appear
        (handles lazy-loading trees) then expands step by step.
        """
        if self._is_tree_child_visible(
            frame, self.period_item, self.hierarchies_folder
        ):
            logger.info("Period branch already expanded")
            return

        if self._is_tree_node_visible(frame, self.period_item):
            logger.info("Expanding %r…", self.period_item)
            self._ensure_period_children_visible(frame)
            return

        deadline = max(1, timeout_ms // POLL_MS)
        for tick in range(deadline):
            if self._is_tree_node_visible(
                frame, self.cubes_folder
            ) or self._is_tree_node_visible(frame, self.dimensions_folder):
                break
            if tick > 0 and tick % 50 == 0:
                logger.info(
                    "Waiting for Cubes/Dimensions node… (%ds)",
                    (tick * POLL_MS) // 1_000,
                )
            frame.wait_for_timeout(POLL_MS)
        else:
            raise PlaywrightTimeoutError(
                f"Cubes/Dimensions node not visible after {timeout_ms // 1000}s "
                f"— Database/Catalog cube may not have loaded"
            )

        # Expand Cubes → cube child → Dimensions if Period not visible yet
        if not self._is_tree_node_visible(frame, self.period_item):
            if not self._is_tree_node_visible(frame, self.dimensions_folder):
                if self._is_tree_node_visible(frame, self.cubes_folder):
                    logger.info("Expanding %r…", self.cubes_folder)
                    self._click_tree_node(frame, self.cubes_folder)
                    self._poll_until_ui(
                        frame,
                        lambda: self._is_tree_node_visible(
                            frame, self.dimensions_folder
                        )
                        or self._is_tree_node_visible(frame, self.period_item),
                        idle_timeout_ms=1_500,
                    )

                if not self._is_tree_node_visible(frame, self.dimensions_folder):
                    nodes = self._collect_visible_tree_nodes(frame)
                    cubes = self._pick_tree_node(
                        nodes, self.cubes_folder, shallowest=True
                    )
                    if cubes:
                        children = [
                            node
                            for node in nodes
                            if node["top"] > cubes["top"]
                            and node["left"] > cubes["left"]
                        ]
                        if children:
                            cube_node = min(
                                children,
                                key=lambda node: (node["top"], node["left"]),
                            )
                            logger.info(
                                "Expanding cube node %r…", cube_node["text"]
                            )
                            self._click_tree_node_box(frame, cube_node)
                            self._poll_until_ui(
                                frame,
                                lambda: self._is_tree_node_visible(
                                    frame, self.dimensions_folder
                                )
                                or self._is_tree_node_visible(
                                    frame, self.period_item
                                ),
                                idle_timeout_ms=1_500,
                            )

            if self._is_tree_node_visible(frame, self.dimensions_folder):
                if not self._poll_until_ui(
                    frame,
                    lambda: self._is_tree_node_visible(
                        frame, self.period_item
                    ),
                    idle_timeout_ms=20_000,
                    busy_timeout_ms=60_000,
                ):
                    logger.info("Expanding %r…", self.dimensions_folder)
                    self._click_tree_node(frame, self.dimensions_folder)
                    self._poll_until_ui(
                        frame,
                        lambda: self._is_tree_node_visible(
                            frame, self.period_item
                        ),
                        idle_timeout_ms=20_000,
                        busy_timeout_ms=60_000,
                    )

        logger.info("Expanding %r…", self.period_item)
        if not self._poll_until_ui(
            frame,
            lambda: self._is_tree_node_visible(frame, self.period_item),
            idle_timeout_ms=20_000,
            busy_timeout_ms=60_000,
        ):
            hints = self._tree_node_hints(frame, self.period_item)
            raise PlaywrightTimeoutError(
                f"Could not click tree node {self.period_item!r} (similar: {hints})"
            )
        if self._expand_tree_row_by_label(
            frame,
            self.period_item,
            deepest=False,
            require_below=self.dimensions_folder,
            verify=lambda: self._is_tree_child_visible(
                frame, self.period_item, self.hierarchies_folder
            ),
        ):
            return
        self._click_tree_node_by_depth(
            frame, self.period_item, shallowest=False
        )

    def select_period(self) -> None:
        """Expand Cubes → Period in the left tree."""
        frame = self._designer_frame()
        logger.info("Waiting for left menu tree to load…")
        self._wait_for_tree_loaded(frame)
        # Give the tree a short settle time — don't block on full schema ready
        # because large cubes can show "Loading…" for a long time at child level
        frame.wait_for_timeout(2_000)
        self._expand_period_branch(frame)
        logger.info("Selected %r from left menu", self.period_item)
        frame.wait_for_timeout(1_000)

    def _is_tree_node_visible(self, frame, label: str, *, partial: bool = False) -> bool:
        try:
            loc = frame.get_by_text(label, exact=not partial)
            return loc.first.is_visible(timeout=150)
        except PlaywrightTimeoutError:
            pass
        if partial:
            return bool(
                self._find_visible_node_box(
                    frame, label, partial=partial, require_unique=False
                )
            )
        nodes = self._collect_visible_tree_nodes(frame)
        return self._pick_tree_node(nodes, label, shallowest=True) is not None

    def _find_visible_node_box(
        self,
        frame,
        label: str,
        *,
        partial: bool = False,
        require_unique: bool = True,
        pick_last: bool = False,
    ) -> dict | None:
        result = frame.evaluate(
            """
            ([label, partial, pickLast]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const match = (text) =>
                    partial ? text.includes(label) : text === label;
                const hits = [];
                for (const el of document.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (!text || !match(text)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    hits.push({
                        x: r.x + r.width / 2,
                        y: r.y + r.height / 2,
                        text,
                    });
                }
                if (!hits.length) return null;
                return pickLast ? hits[hits.length - 1] : hits[0];
            }
            """,
            [label, partial, pick_last],
        )
        if result is None and require_unique:
            return None
        return result

    def _locator_page_point(
        self,
        locator,
        *,
        x_ratio: float = 0.5,
        y_ratio: float = 0.5,
    ) -> tuple[float, float] | None:
        box = locator.bounding_box()
        if not box:
            return None
        return (
            box["x"] + box["width"] * x_ratio,
            box["y"] + box["height"] * y_ratio,
        )

    def _human_mouse_drag(
        self,
        page,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        steps: int = 22,
    ) -> None:
        """Click-hold-drag-release like a user (not a teleport jump)."""
        sx, sy = start
        ex, ey = end
        page.mouse.move(sx, sy)
        page.wait_for_timeout(80)
        page.mouse.down(button="left")
        page.wait_for_timeout(100)
        for step in range(1, steps + 1):
            t = step / steps
            page.mouse.move(sx + (ex - sx) * t, sy + (ey - sy) * t)
            page.wait_for_timeout(6)
        page.wait_for_timeout(50)
        page.mouse.move(ex, ey)
        page.wait_for_timeout(250)
        page.mouse.up(button="left")

    def _mouse_drag(
        self,
        frame,
        source: dict,
        target: dict,
        *,
        source_is_page: bool = True,
        target_is_page: bool = False,
    ) -> None:
        page = frame.page
        fbox = frame.frame_element().bounding_box()

        def to_page(x: float, y: float, is_page: bool) -> tuple[float, float]:
            if is_page or not fbox:
                return x, y
            return fbox["x"] + x, fbox["y"] + y

        start = to_page(source["x"], source["y"], source_is_page)
        end = to_page(target["x"], target["y"], target_is_page)
        self._human_mouse_drag(page, start, end)

    def _drag_tree_label_to_drop_zone(
        self,
        frame,
        item_label: str,
        drop_zone_text: str,
        *,
        deepest: bool = True,
        below_label: str | None = None,
        grandparent_label: str | None = None,
        drop_loc=None,
        verify=None,
        source_loc=None,
    ) -> None:
        """Click a tree row label, drag to a pivot drop zone, release."""
        if source_loc is None:
            source_loc = self._resolve_tree_label_locator(
                frame,
                item_label,
                deepest=deepest,
                below_label=below_label,
                grandparent_label=grandparent_label,
            )
        if source_loc is None:
            hints = self._tree_node_hints(frame, item_label)
            raise PlaywrightTimeoutError(
                f"Could not find {item_label!r} for drag (similar: {hints})"
            )

        if drop_loc is None:
            drop_loc = frame.get_by_text(drop_zone_text, exact=False).first
            drop_loc.wait_for(state="visible", timeout=15_000)
        self._scroll_schema_label_into_view(frame, item_label)
        drop_loc.scroll_into_view_if_needed()
        self._settle(frame)

        if verify is None:
            def verify() -> bool:
                return self._pivot_field_dropped(
                    frame, item_label, drop_zone_text
                )

        page = frame.page
        for attempt in range(1, 5):
            logger.info(
                "Dragging %r → %r (attempt %d)…",
                item_label,
                drop_zone_text,
                attempt,
            )
            try:
                source_loc.drag_to(
                    drop_loc,
                    force=True,
                    timeout=20_000,
                    source_position={"x": 28, "y": 10},
                    target_position={"x": 12, "y": 12},
                )
            except PlaywrightTimeoutError:
                logger.info(
                    "drag_to failed for %r — using manual click-drag-release",
                    item_label,
                )
                start = self._locator_page_point(
                    source_loc, x_ratio=0.32, y_ratio=0.5
                )
                end = self._locator_page_point(drop_loc, x_ratio=0.5, y_ratio=0.5)
                if not start or not end:
                    raise PlaywrightTimeoutError(
                        f"Could not read drag coordinates for {item_label!r}"
                    ) from None
                self._human_mouse_drag(page, start, end)

            if self._poll_until_ui(frame, verify, idle_timeout_ms=5_000):
                logger.info(
                    "Dropped %r on %r", item_label, drop_zone_text
                )
                return

            logger.info(
                "drag_to did not verify for %r — retrying with mouse drag",
                item_label,
            )
            start = self._locator_page_point(
                source_loc, x_ratio=0.32, y_ratio=0.5
            )
            end = self._locator_page_point(drop_loc, x_ratio=0.5, y_ratio=0.5)
            if start and end:
                self._human_mouse_drag(page, start, end)
            if self._poll_until_ui(frame, verify, idle_timeout_ms=5_000):
                logger.info(
                    "Dropped %r on %r (mouse)", item_label, drop_zone_text
                )
                return
            if attempt >= 4:
                raise PlaywrightTimeoutError(
                    f"Drop verification failed — {item_label!r} not found "
                    f"in {drop_zone_text!r}"
                )
            logger.info(
                "Drop not verified yet — retrying drag for %r", item_label
            )
            self._settle(frame, 200)

    def _drag_item_to_drop_zone(
        self,
        frame,
        item_label: str,
        drop_zone_text: str,
        *,
        partial_item: bool = False,
        fallback_needles: tuple[str, ...] = (),
        pick_last: bool = False,
        target_pick_last: bool = False,
    ) -> None:
        target = self._find_visible_node_box(
            frame,
            drop_zone_text,
            partial=True,
            pick_last=target_pick_last,
        )
        if not target:
            raise PlaywrightTimeoutError(f"Drop zone {drop_zone_text!r} not found")

        needles = (item_label, *fallback_needles)
        for needle in needles:
            source = self._find_visible_node_box(
                frame,
                needle,
                partial=partial_item,
                pick_last=pick_last,
            )
            if not source:
                continue
            if pick_last:
                self._mouse_drag(
                    frame,
                    source,
                    target,
                    source_is_page=False,
                    target_is_page=False,
                )
                logger.info(
                    "Dragged %r to %r (mouse)",
                    source["text"],
                    drop_zone_text,
                )
                return
            try:
                source_loc = frame.get_by_text(source["text"], exact=True).first
                target_loc = frame.get_by_text(drop_zone_text, exact=False).first
                source_loc.drag_to(target_loc, force=True, timeout=10_000)
                logger.info("Dragged %r to %r", source["text"], drop_zone_text)
                return
            except PlaywrightTimeoutError:
                logger.info(
                    "drag_to failed for %r — retrying with mouse drag",
                    needle,
                )
                self._mouse_drag(
                    frame,
                    source,
                    target,
                    source_is_page=False,
                    target_is_page=False,
                )
                logger.info(
                    "Dragged %r to %r (mouse)",
                    source["text"],
                    drop_zone_text,
                )
                return

        hints = self._tree_node_hints(frame, item_label)
        raise PlaywrightTimeoutError(
            f"Could not drag {item_label!r} to {drop_zone_text!r} (similar: {hints})"
        )

    def _drag_item_to_column_drop_zone(self, frame, item_label: str) -> None:
        self._drag_item_to_drop_zone(
            frame,
            item_label,
            self.column_drop_zone_text,
            partial_item=False,
        )

    def _drag_item_to_row_drop_zone(
        self, frame, item_label: str, *, pick_last: bool = False
    ) -> None:
        self._drag_item_to_drop_zone(
            frame,
            item_label,
            self.row_drop_zone_text,
            partial_item=False,
            pick_last=pick_last,
            target_pick_last=True,
        )

    def _menu_item_visible(self, scope, label: str) -> bool:
        try:
            scope.get_by_text(label, exact=True).first.wait_for(
                state="visible", timeout=1_000
            )
            return True
        except PlaywrightTimeoutError:
            return False

    def _frame_page_point(self, frame, x: float, y: float) -> tuple[float, float]:
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return x, y
        return fbox["x"] + x, fbox["y"] + y

    def _find_visible_menu_item(
        self, label: str, *, partial: bool = False
    ) -> tuple[object, dict] | tuple[None, None]:
        """Return (scope, {x, y, text}) for a visible RadMenu / context-menu row."""
        for scope in self._all_dialog_scopes():
            try:
                hit = scope.evaluate(
                    """
                    ([label, partial]) => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const match = (text) => {
                            if (!text) return false;
                            if (partial) {
                                const needle = label.replace(/\\.\\.\\.$/, '').toLowerCase();
                                return text.toLowerCase().includes(needle);
                            }
                            if (label.endsWith('...')) {
                                const base = label.slice(0, -3);
                                return text === label || text.startsWith(base);
                            }
                            return text === label;
                        };
                        const tree = document.getElementById('trvSchema')
                            || document.querySelector('[id*="trvSchema"]');
                        const inTree = (el) => !!(tree && tree.contains(el));

                        const consider = (el, priority) => {
                            const text = trim(el.textContent);
                            if (!text || text.length > 80) return;
                            if (!match(text)) return;
                            if (inTree(el)) return;
                            if (el.children.length > 8 && text.length > label.length + 12) {
                                return;
                            }
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) return;
                            const hit = {
                                x: r.x + r.width / 2,
                                y: r.y + r.height / 2,
                                text,
                                area: r.width * r.height,
                                priority,
                            };
                            if (
                                !best
                                || hit.priority < best.priority
                                || (
                                    hit.priority === best.priority
                                    && hit.area < best.area
                                )
                            ) {
                                best = hit;
                            }
                        };

                        let best = null;

                        for (const slide of document.querySelectorAll('.rmSlide')) {
                            const sr = slide.getBoundingClientRect();
                            if (sr.width <= 0 || sr.height <= 0) continue;
                            const style = window.getComputedStyle(slide);
                            if (
                                style.display === 'none'
                                || style.visibility === 'hidden'
                            ) {
                                continue;
                            }
                            for (const el of slide.querySelectorAll(
                                '.rmText, .rmLink, a, span'
                            )) {
                                consider(el, 0);
                            }
                        }

                        if (!best) {
                            for (const el of document.querySelectorAll(
                                '.RadMenu .rmText, .rmGroup .rmText, .rmLink'
                            )) {
                                consider(el, 1);
                            }
                        }

                        if (!best) {
                            for (const el of document.querySelectorAll(
                                'span, a, td, li, button'
                            )) {
                                consider(el, 2);
                            }
                        }

                        return best;
                    }
                    """,
                    [label, partial],
                )
            except Exception:
                hit = None
            if hit:
                return scope, hit
        return None, None

    def _is_menu_item_visible(self, label: str, *, partial: bool = False) -> bool:
        _, hit = self._find_visible_menu_item(label, partial=partial)
        return hit is not None

    def _hover_menu_item(self, label: str) -> None:
        frame = self._designer_frame()
        for partial in (False, True):
            for _ in range(4):
                scope, hit = self._find_visible_menu_item(label, partial=partial)
                if hit:
                    if scope is self.page:
                        px, py = hit["x"], hit["y"]
                    else:
                        px, py = self._frame_page_point(
                            frame, hit["x"], hit["y"]
                        )
                    self.page.mouse.move(px, py)
                    frame.wait_for_timeout(150)
                    logger.info("Hovered menu item %r (%r)", label, hit["text"])
                    return
                frame.wait_for_timeout(80)
        raise PlaywrightTimeoutError(f"Menu item {label!r} not found to hover")

    def _try_click_menu_item(self, label: str) -> bool:
        frame = self._designer_frame()
        for partial in (False, True):
            for _ in range(4):
                scope, hit = self._find_visible_menu_item(label, partial=partial)
                if hit:
                    if scope is self.page:
                        px, py = hit["x"], hit["y"]
                    else:
                        px, py = self._frame_page_point(
                            frame, hit["x"], hit["y"]
                        )
                    self.page.mouse.click(px, py)
                    logger.info("Clicked menu item %r (%r)", label, hit["text"])
                    frame.wait_for_timeout(150)
                    return True
                frame.wait_for_timeout(80)

        for scope in self._all_dialog_scopes():
            item = scope.get_by_text(label, exact=True)
            if item.count() > 0:
                try:
                    item.first.click(timeout=2_000)
                    logger.info("Clicked menu item %r", label)
                    return True
                except PlaywrightTimeoutError:
                    continue

        return False

    def _click_menu_item(self, label: str) -> None:
        if not self._try_click_menu_item(label):
            raise PlaywrightTimeoutError(f"Menu item {label!r} not found")

    def _open_filter_submenu_if_needed(self) -> None:
        """Hover Filter submenu when Show Only the Top is not visible yet."""
        frame = self._designer_frame()
        if self._is_menu_item_visible(self.show_only_top_menu_text, partial=True):
            return
        for label in (self.filter_menu_text, "Filters"):
            if self._is_menu_item_visible(label):
                logger.info("Opening %r submenu…", label)
                self._hover_menu_item(label)
                if self._poll_until(
                    frame,
                    lambda: self._is_menu_item_visible(
                        self.show_only_top_menu_text, partial=True
                    ),
                    timeout_ms=3_000,
                ):
                    logger.info("%r submenu opened", self.show_only_top_menu_text)
                    return
                logger.warning(
                    "%r hovered but %r submenu did not appear — retrying",
                    label,
                    self.show_only_top_menu_text,
                )
                self._hover_menu_item(label)
                if self._poll_until(
                    frame,
                    lambda: self._is_menu_item_visible(
                        self.show_only_top_menu_text, partial=True
                    ),
                    timeout_ms=2_000,
                ):
                    return
                raise PlaywrightTimeoutError(
                    f"{self.show_only_top_menu_text!r} did not appear under {label!r}"
                )
        raise PlaywrightTimeoutError(
            f"Neither {self.filter_menu_text!r} nor Filters menu item is visible"
        )

    def _wait_for_query_idle(self, frame, timeout_ms: int = 120_000) -> None:
        """Wait until no 'Query is running' spinner before opening pivot menus."""
        if not self._is_query_running_visible(frame):
            return
        logger.info("Waiting for query to finish before pivot menu…")
        elapsed = 0
        poll_ms = QUERY_IDLE_POLL_MS
        while elapsed < timeout_ms:
            if not self._is_query_running_visible(frame):
                logger.info("Query finished — continuing")
                return
            frame.wait_for_timeout(poll_ms)
            elapsed += poll_ms
        raise PlaywrightTimeoutError(
            f"Query still running after {timeout_ms // 1_000}s"
        )

    def _navigate_show_only_top_custom_menu(self) -> None:
        """Product menu: Filter → Show Only the Top → Custom…"""
        frame = self._designer_frame()
        try:
            self._open_filter_submenu_if_needed()
        except PlaywrightTimeoutError:
            if not self._is_menu_item_visible(
                self.show_only_top_menu_text, partial=True
            ):
                raise
        self._hover_menu_item(self.show_only_top_menu_text)
        if not self._poll_until(
            frame,
            lambda: self._is_menu_item_visible(self.custom_top_menu_text)
            or self._is_menu_item_visible("Custom", partial=True),
            timeout_ms=3_000,
        ):
            self._hover_menu_item(self.show_only_top_menu_text)
            if not self._poll_until(
                frame,
                lambda: self._is_menu_item_visible(self.custom_top_menu_text)
                or self._is_menu_item_visible("Custom", partial=True),
                timeout_ms=2_000,
            ):
                raise PlaywrightTimeoutError(
                    f"{self.custom_top_menu_text!r} submenu did not open"
                )
        self._click_menu_item(self.custom_top_menu_text)
        if not self._poll_until(
            frame,
            lambda: self._scope_for_open_dialog(
                self.customize_filter_dialog_title
            )
            is not None,
            timeout_ms=12_000,
        ):
            raise PlaywrightTimeoutError(
                f"{self.customize_filter_dialog_title!r} dialog did not open"
            )
        logger.info("%r dialog opened", self.customize_filter_dialog_title)

    def _open_pivot_row_dimension_dropdown(self, frame, dimension: str) -> None:
        """Open the pivot grid row-dimension header menu (Product / Pack / Brick).

        Permissive finder (matches the original working flow): locate the field
        header label anywhere in the pivot header band and click its dropdown
        arrow image, falling back to clicking the label itself.
        """
        opened = frame.evaluate(
            """
            ([dimension]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const matchesDim = (text) =>
                    text === dimension
                    || text.startsWith(dimension + ' (')
                    || text.startsWith(dimension + '(');
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const clickArrowNear = (host) => {
                    if (!host) return false;
                    for (const img of host.querySelectorAll('img')) {
                        if (!isVisible(img)) continue;
                        img.scrollIntoView({ block: 'center' });
                        img.click();
                        return true;
                    }
                    return false;
                };

                for (const el of document.body.querySelectorAll(
                    'td, th, span, div, a, nobr, label'
                )) {
                    const text = trim(el.textContent);
                    if (!matchesDim(text) || text.length > 60) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 720) continue;
                    const cell = el.closest('td, th') || el.parentElement;
                    if (clickArrowNear(cell)) return 'header-arrow';
                    const row = (cell || el).closest('tr');
                    if (clickArrowNear(row)) return 'row-arrow';
                    el.scrollIntoView({ block: 'center' });
                    el.click();
                    return 'label';
                }

                for (const row of document.querySelectorAll('tr')) {
                    const rowText = trim(row.textContent);
                    if (!rowText.includes(dimension)) continue;
                    for (const el of row.querySelectorAll('td, th, span, div, a')) {
                        if (!matchesDim(trim(el.textContent))) continue;
                        if (inTree(el) || !isVisible(el)) continue;
                        const cell = el.closest('td, th') || el;
                        if (clickArrowNear(cell)) return 'grid-arrow';
                        el.scrollIntoView({ block: 'center' });
                        el.click();
                        return 'grid-label';
                    }
                }
                return null;
            }
            """,
            [dimension],
        )
        if not opened:
            raise PlaywrightTimeoutError(
                f"Could not open pivot row dropdown for {dimension!r}"
            )
        logger.info("Opened pivot row dropdown for %r (%s)", dimension, opened)
        frame.wait_for_timeout(600)

    def _find_row_dimension_field_arrow_coords(
        self, frame, dimension: str
    ) -> dict | None:
        """Arrow/label coords for a row-dimension field in the pivot layout panel."""
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([dimension, treeRight, minY, maxY]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;
                const matches = (text) =>
                    text === dimension
                    || text.startsWith(dimension + ' (')
                    || text.startsWith(dimension + '(');

                let bestHost = null;
                let bestScore = null;
                for (const el of document.body.querySelectorAll(
                    'nobr, span, td, div, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (!matches(text) || text.length > 60) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top < minY || r.top > maxY) continue;
                    if (r.left < minLeft) continue;
                    const score = r.top * 1000 + r.left;
                    if (bestScore === null || score > bestScore) {
                        bestScore = score;
                        bestHost = el.closest('tr, td, div, li') || el.parentElement;
                    }
                }
                if (!bestHost) return null;

                let arrowX = null;
                let arrowY = null;
                let labelX = null;
                let labelY = null;
                for (const img of bestHost.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    arrowX = r.x + r.width / 2;
                    arrowY = r.y + r.height / 2;
                    break;
                }
                for (const el of bestHost.querySelectorAll('nobr, a, span, td')) {
                    const text = trim(el.textContent);
                    if (!matches(text) && !text.includes(dimension)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    labelX = r.x + r.width / 2;
                    labelY = r.y + r.height / 2;
                    break;
                }
                if (labelX === null && arrowX !== null) {
                    labelX = arrowX;
                    labelY = arrowY;
                }
                if (labelX === null) return null;
                return {
                    text: trim(bestHost.textContent).slice(0, 60),
                    arrowX: arrowX ?? labelX,
                    arrowY: arrowY ?? labelY,
                    labelX,
                    labelY,
                };
            }
            """,
            [dimension, tree_right, 90, 720],
        )

    def _click_row_dimension_field_trigger(
        self, frame, dimension: str
    ) -> str | None:
        """Click the dropdown arrow on a row-dimension field (Filter / Show Only Top menu)."""
        loc = self._resolve_pivot_field_locator(frame, dimension)
        if loc is not None:
            try:
                box = loc.bounding_box()
                if box and 90 <= box["y"] <= 720:
                    row = loc.locator(
                        "xpath=ancestor::tr[1] | ancestor::td[1]"
                    ).first
                    arrow = row.locator("img").first
                    if arrow.count() > 0:
                        try:
                            if arrow.is_visible(timeout=400):
                                arrow.click(timeout=3_000)
                                self._settle(frame, 150)
                                return "locator-img"
                        except PlaywrightTimeoutError:
                            pass
                    loc.click(timeout=3_000)
                    self._settle(frame, 150)
                    return "locator-label"
            except PlaywrightTimeoutError:
                pass

        coords = self._find_row_dimension_field_arrow_coords(frame, dimension)
        if not coords:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        page = frame.page
        for name, fx, fy in (
            ("js-arrow", coords["arrowX"], coords["arrowY"]),
            ("js-label", coords["labelX"], coords["labelY"]),
        ):
            page.mouse.click(fbox["x"] + fx, fbox["y"] + fy)
            self._settle(frame, 150)
            return name
        return None

    def _pivot_row_filter_menu_open(self) -> bool:
        return (
            self._is_menu_item_visible(self.show_only_top_menu_text, partial=True)
            or self._is_menu_item_visible(self.filter_menu_text)
            or self._is_menu_item_visible(self.expand_members_menu_text, partial=True)
        )

    def _try_open_pivot_row_dimension_dropdown(
        self, frame, dimension: str
    ) -> bool:
        try:
            self._open_pivot_row_dimension_dropdown(frame, dimension)
            return True
        except PlaywrightTimeoutError:
            return False

    def _resolve_pivot_row_field_coords(
        self, frame, dimension: str
    ) -> dict | None:
        """Coords for a row-dimension header in the pivot layout (not grid cells)."""
        finders: list = []
        if dimension == self.product_dimension:
            finders.extend(
                (
                    lambda f: self._find_row_field_header_loose(
                        f, self.product_dimension
                    ),
                    lambda f: self._last_product_row_coords,
                    self._find_pivot_row_product_header_coords,
                    lambda f: self._find_pivot_row_field_coords(
                        f, self.product_dimension
                    ),
                )
            )
        elif dimension == self.pack_attribute:
            finders.extend(
                (
                    self._find_pivot_row_pack_header_coords,
                    lambda f: self._last_pack_row_coords,
                    lambda f: self._find_row_field_header_loose(
                        f, self.pack_attribute
                    ),
                    lambda f: self._find_pivot_row_field_coords(
                        f, self.pack_attribute
                    ),
                    self._find_pivot_row_pack_on_brick_header_coords,
                )
            )
        else:
            finders.append(
                lambda f: self._find_row_field_header_loose(f, dimension)
            )
            finders.append(
                lambda f: self._find_pivot_row_field_coords(f, dimension)
            )

        for finder in finders:
            coords = finder(frame) if callable(finder) else finder
            if coords and self._pivot_row_header_coords_valid(frame, coords):
                return coords
        return None

    def _dispatch_contextmenu_at(self, frame, page_x: float, page_y: float) -> bool:
        """Fire a native contextmenu event at frame-local coords (Telerik RadMenu)."""
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return False
        fx = page_x - fbox["x"]
        fy = page_y - fbox["y"]
        return bool(
            frame.evaluate(
                """
                ([fx, fy]) => {
                    const el = document.elementFromPoint(fx, fy);
                    if (!el) return false;
                    const opts = {
                        bubbles: true,
                        cancelable: true,
                        clientX: fx,
                        clientY: fy,
                        button: 2,
                    };
                    el.dispatchEvent(new MouseEvent('mousedown', { ...opts, button: 2 }));
                    el.dispatchEvent(new MouseEvent('mouseup', { ...opts, button: 2 }));
                    el.dispatchEvent(new MouseEvent('contextmenu', opts));
                    return true;
                }
                """,
                [fx, fy],
            )
        )

    def _clear_open_popups(self, frame) -> None:
        try:
            frame.page.keyboard.press("Escape")
            frame.wait_for_timeout(120)
        except Exception:
            pass

    def _open_row_dimension_contextmenu_js(
        self, frame, dimension: str
    ) -> bool:
        """Right-click a row-dimension header via JS (avoids guard/mouse coord drift)."""
        tree_right = self._schema_tree_right_edge(frame)
        fired = frame.evaluate(
            """
            ([dimension, treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = (treeRight > 0 ? treeRight : 280) + 48;
                const matches = (text) =>
                    text === dimension
                    || text.startsWith(dimension + ' (')
                    || text.startsWith(dimension + '(');

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'td, th, span, div, nobr, a, label'
                )) {
                    const text = trim(el.textContent);
                    if (!matches(text) || text.length > 80) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top < 80 || r.top > 430) continue;
                    if (r.left < minLeft) continue;
                    const score = r.top * 1000 + r.left;
                    if (!best || score > best.score) {
                        best = { el, score, text };
                    }
                }
                if (!best) return false;

                const cell = best.el.closest('td, th') || best.el;
                const r = cell.getBoundingClientRect();
                const x = r.x + Math.min(r.width * 0.75, r.width - 6);
                const y = r.y + r.height / 2;
                cell.scrollIntoView({ block: 'center', inline: 'nearest' });
                const opts = {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                    button: 2,
                    buttons: 2,
                    clientX: x,
                    clientY: y,
                    screenX: x,
                    screenY: y,
                };
                cell.dispatchEvent(new MouseEvent('mousedown', opts));
                cell.dispatchEvent(new MouseEvent('mouseup', opts));
                cell.dispatchEvent(new MouseEvent('contextmenu', opts));
                return true;
            }
            """,
            [dimension, tree_right],
        )
        if fired:
            logger.info(
                "Dispatched contextmenu on %r row header (JS)", dimension
            )
        return bool(fired)

    def _ensure_pivot_row_menu_open(self, frame, dimension: str) -> None:
        """Open the row-dimension Filter context menu (right-click first, then arrow)."""
        if self._pivot_row_filter_menu_open():
            return

        menu_ready = self._pivot_row_filter_menu_open
        page = frame.page
        for attempt in range(1, 5):
            if self._open_row_dimension_contextmenu_js(frame, dimension):
                frame.wait_for_timeout(250)
                if self._poll_until(frame, menu_ready, timeout_ms=1_800):
                    logger.info(
                        "Opened pivot row menu for %r (JS contextmenu)", dimension
                    )
                    return
                self._clear_open_popups(frame)

            coords = self._resolve_pivot_row_field_coords(frame, dimension)
            if coords is None:
                if dimension == self.pack_attribute:
                    coords = self._find_pivot_row_pack_header_coords(frame)
                elif dimension == self.product_dimension:
                    coords = self._find_pivot_row_product_coords(frame)

            if coords:
                px, py = coords["page_x"], coords["page_y"]
                # Right-click is the reliable trigger for the Filter context menu.
                page.mouse.click(px, py, button="right")
                frame.wait_for_timeout(250)
                if self._poll_until(frame, menu_ready, timeout_ms=1_800):
                    logger.info(
                        "Opened pivot row menu for %r (right-click)", dimension
                    )
                    return
                self._clear_open_popups(frame)

                if self._dispatch_contextmenu_at(frame, px, py):
                    frame.wait_for_timeout(250)
                    if self._poll_until(frame, menu_ready, timeout_ms=1_800):
                        logger.info(
                            "Opened pivot row menu for %r (contextmenu-event)",
                            dimension,
                        )
                        return
                    self._clear_open_popups(frame)

                # Left-click the dropdown arrow (member/filter popup).
                page.mouse.click(px - 36, py)
                frame.wait_for_timeout(250)
                if self._poll_until(frame, menu_ready, timeout_ms=1_500):
                    logger.info(
                        "Opened pivot row menu for %r (field-arrow)", dimension
                    )
                    return
                self._clear_open_popups(frame)

            method = self._click_row_dimension_field_trigger(frame, dimension)
            if method:
                if self._poll_until(frame, menu_ready, timeout_ms=1_500):
                    logger.info(
                        "Opened pivot row menu for %r (%s)", dimension, method
                    )
                    return
                self._clear_open_popups(frame)

            if self._try_open_pivot_row_dimension_dropdown(frame, dimension):
                if self._poll_until(frame, menu_ready, timeout_ms=1_800):
                    return
                self._clear_open_popups(frame)

            logger.info(
                "Pivot row filter menu for %r not open yet (attempt %d)",
                dimension,
                attempt,
            )
            frame.wait_for_timeout(300)

    def _dump_open_menu_state(self, frame, tag: str) -> None:
        """Diagnostic: log visible popup/menu text and save a screenshot."""
        try:
            items = frame.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const out = [];
                    const seen = new Set();
                    const sels = [
                        '.rmSlide', '.RadMenu', '.rmGroup', '.rmRootGroup',
                        '[class*="Menu"]', '[class*="menu"]', '[class*="Slide"]',
                        '[class*="Popup"]', '[class*="popup"]', '[class*=" contextmenu"]',
                        '[class*="ContextMenu"]', 'ul[role="menu"]', '[role="menu"]'
                    ];
                    for (const sel of sels) {
                        for (const root of document.querySelectorAll(sel)) {
                            const r = root.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            const style = window.getComputedStyle(root);
                            if (style.display === 'none' || style.visibility === 'hidden') continue;
                            const cls = (root.className || '').toString().slice(0, 60);
                            for (const el of root.querySelectorAll(
                                'a, span, td, li, .rmText, .rmLink, button, div'
                            )) {
                                const t = trim(el.textContent);
                                if (!t || t.length > 50) continue;
                                if (el.children.length > 2) continue;
                                const er = el.getBoundingClientRect();
                                if (er.width <= 0 || er.height <= 0) continue;
                                const key = cls + '|' + t;
                                if (seen.has(key)) continue;
                                seen.add(key);
                                out.push(`[${cls}] ${t} @(${Math.round(er.x)},${Math.round(er.y)})`);
                            }
                        }
                    }
                    return out.slice(0, 80);
                }
                """
            )
        except Exception as exc:
            items = [f"<dump failed: {exc}>"]
        logger.info("MENU DUMP (%s): %d visible items", tag, len(items))
        for line in items:
            logger.info("  MENU %s", line)
        try:
            kw = frame.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const tree = document.getElementById('trvSchema')
                        || document.querySelector('[id*="trvSchema"]');
                    const inTree = (el) => !!(tree && tree.contains(el));
                    const words = ['sort','filter','show only','top','expand',
                        'collapse','remove','move','hide','field','dimension',
                        'choose','level','member'];
                    const out = [];
                    const seen = new Set();
                    for (const el of document.body.querySelectorAll(
                        'a, span, td, li, div, button'
                    )) {
                        if (inTree(el)) continue;
                        if (el.children.length > 1) continue;
                        const t = trim(el.textContent);
                        if (!t || t.length > 40) continue;
                        const low = t.toLowerCase();
                        if (!words.some((w) => low.includes(w))) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.top < 80) continue;
                        const key = t + '@' + Math.round(r.y);
                        if (seen.has(key)) continue;
                        seen.add(key);
                        const cls = (el.className || '').toString().slice(0, 30);
                        out.push(`${el.tagName}.${cls} "${t}" @(${Math.round(r.x)},${Math.round(r.y)})`);
                    }
                    return out.slice(0, 60);
                }
                """
            )
            logger.info("MENU KEYWORDS (%s): %d", tag, len(kw))
            for line in kw:
                logger.info("  KW %s", line)
        except Exception as exc:
            logger.info("MENU keyword dump failed: %s", exc)
        try:
            tree_right = self._schema_tree_right_edge(frame)
            header = frame.evaluate(
                """
                ([treeRight, label]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const tree = document.getElementById('trvSchema')
                        || document.querySelector('[id*="trvSchema"]');
                    const inTree = (el) => !!(tree && tree.contains(el));
                    const minLeft = treeRight + 24;
                    let hit = null;
                    for (const el of document.body.querySelectorAll(
                        'span, td, div, nobr, a, label'
                    )) {
                        const t = trim(el.textContent);
                        if (t !== label && !t.startsWith(label + ' (')) continue;
                        if (inTree(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.top > 200) continue;
                        if (r.left < minLeft) continue;
                        hit = el;
                        break;
                    }
                    if (!hit) return null;
                    const cell = hit.closest('td, th') || hit.parentElement;
                    const kids = [];
                    for (const el of cell.querySelectorAll('*')) {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        kids.push(
                            `${el.tagName}.${(el.className||'').toString().slice(0,30)} `
                            + `t="${trim(el.textContent).slice(0,18)}" `
                            + `@(${Math.round(r.x)},${Math.round(r.y)} ${Math.round(r.width)}x${Math.round(r.height)})`
                        );
                    }
                    return {
                        html: cell.outerHTML.slice(0, 600),
                        kids: kids.slice(0, 25),
                    };
                }
                """,
                [tree_right, self.product_dimension],
            )
            if header:
                logger.info("PRODUCT HEADER kids (%d):", len(header.get("kids", [])))
                for k in header.get("kids", []):
                    logger.info("  HDR %s", k)
                logger.info("PRODUCT HEADER html: %s", header.get("html"))
            else:
                logger.info("PRODUCT HEADER not found in dump")
        except Exception as exc:
            logger.info("PRODUCT HEADER dump failed: %s", exc)
        try:
            shot = f"data/logs/menu_{tag}.png"
            frame.page.screenshot(path=shot, full_page=False)
            logger.info("MENU SCREENSHOT saved → %s", shot)
        except Exception as exc:
            logger.info("MENU screenshot failed: %s", exc)

    def apply_product_show_only_top(
        self, count: int | None = None, *, via_custom: bool = False
    ) -> None:
        """Product row dropdown → Filter → Show Only the Top → Custom → Count + Values."""
        top_count = self.product_top_count if count is None else count
        frame = self._designer_frame()

        logger.info(
            "Applying Product Show Only the Top filter (count=%s)…", top_count
        )
        self._wait_for_query_idle(frame)
        refreshed = (
            self._find_row_field_header_loose(frame, self.product_dimension)
            or self._find_pivot_row_product_header_coords(frame)
        )
        if refreshed:
            self._last_product_row_coords = refreshed
            logger.info(
                "Product row header at (%.0f, %.0f) %r",
                refreshed["page_x"],
                refreshed["page_y"],
                refreshed.get("text"),
            )

        menu_ready = lambda: (
            self._is_menu_item_visible(self.filter_menu_text)
            or self._is_menu_item_visible(
                self.show_only_top_menu_text, partial=True
            )
        )
        opened = False
        for attempt in range(1, 9):
            if menu_ready():
                opened = True
                break
            self._wait_for_query_idle(frame)
            try:
                self._open_pivot_row_dimension_dropdown(
                    frame, self.product_dimension
                )
            except PlaywrightTimeoutError:
                try:
                    self._ensure_pivot_row_menu_open(
                        frame, self.product_dimension
                    )
                except PlaywrightTimeoutError:
                    pass
            if self._poll_until(frame, menu_ready, timeout_ms=3_500):
                opened = True
                break
            logger.info(
                "Product row menu not ready yet (attempt %d) — retrying", attempt
            )
            self._dump_open_menu_state(frame, f"product_menu_attempt{attempt}")
            self._clear_open_popups(frame)
            frame.wait_for_timeout(500)
        if not opened:
            self._ensure_pivot_row_menu_open(frame, self.product_dimension)
            if not self._poll_until(frame, menu_ready, timeout_ms=3_000):
                self._dump_open_menu_state(frame, "product_menu_final")
                raise PlaywrightTimeoutError(
                    "Product row context menu did not open "
                    f"(expected {self.filter_menu_text!r} or "
                    f"{self.show_only_top_menu_text!r})"
                )

        if (
            not via_custom
            and self._is_menu_item_visible(
                self.show_only_top_menu_text, partial=True
            )
            and self._try_click_top_count_option(top_count)
        ):
            self._wait_for_query_idle(frame)
            logger.info("Product Show Only the Top (%s) applied", top_count)
            return

        logger.info(
            "Opening %r — Display Range=%r, Count=%s, Based on Measure=%r",
            self.custom_top_menu_text,
            self.custom_top_display_range_value,
            top_count,
            self.custom_top_based_on_measure,
        )
        self._navigate_show_only_top_custom_menu()
        self._apply_customize_top_count(top_count)
        self._wait_for_query_idle(frame)
        if self._customize_filter_dialog_open():
            raise PlaywrightTimeoutError(
                f"{self.customize_filter_dialog_title!r} dialog still open after OK"
            )
        logger.info("Product Show Only the Top (%s) applied", top_count)

    def _try_click_top_count_option(self, count: int) -> bool:
        count_text = str(count)
        frame = self._designer_frame()
        for scope in (frame, self.page):
            clicked = scope.evaluate(
                """
                ([countText]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const tryClick = (root) => {
                        for (const el of root.querySelectorAll(
                            '.rmText, .rmLink, span, a, td, li'
                        )) {
                            if (trim(el.textContent) !== countText) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            el.click();
                            return true;
                        }
                        return false;
                    };
                    for (const slide of document.querySelectorAll('.rmSlide')) {
                        const sr = slide.getBoundingClientRect();
                        if (sr.width <= 0 || sr.height <= 0) continue;
                        if (tryClick(slide)) return true;
                    }
                    return tryClick(document.body);
                }
                """,
                [count_text],
            )
            if clicked:
                logger.info("Selected Show Only the Top → %s", count_text)
                return True
        return False

    def _wait_for_dialog(self, title: str, timeout_ms: int = 8_000) -> None:
        scope = self._scope_for_open_dialog(title)
        if scope is not None:
            logger.info("%r dialog is open", title)
            return
        for scope in self._filter_dialog_scopes():
            try:
                scope.get_by_text(title, exact=False).first.wait_for(
                    state="visible", timeout=timeout_ms
                )
                logger.info("%r dialog is open", title)
                return
            except PlaywrightTimeoutError:
                continue
        raise PlaywrightTimeoutError(f"{title!r} dialog did not open")

    def _click_dialog_ok_js(self, scope, dialog_title: str) -> bool:
        return bool(
            scope.evaluate(
                """
                ([dialogTitle]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let dialog = null;
                    let bestArea = Infinity;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes(dialogTitle)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const area = r.width * r.height;
                        if (area < bestArea) {
                            bestArea = area;
                            dialog = el;
                        }
                    }
                    const roots = dialog
                        ? [dialog, dialog.closest('.rwWindow'), dialog.parentElement]
                        : [document.body];
                    for (const root of roots) {
                        if (!root) continue;
                        for (const el of root.querySelectorAll(
                            'input[type="button"], input[type="submit"], '
                            + 'button, a'
                        )) {
                            const label = trim(el.value || el.textContent);
                            if (label.toUpperCase() !== 'OK') continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }
                """,
                [dialog_title],
            )
        )

    def _click_rw_window_ok_js(self, scope) -> bool:
        """Click OK inside a visible Telerik RadWindow (Customize Filter, etc.)."""
        return bool(
            scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const clickOk = (root) => {
                        for (const el of root.querySelectorAll(
                            'input[type="button"], input[type="submit"], '
                            + 'button, a.rwPopupButton, .rwPopupButton, '
                            + 'span.rwInnerSpan, a'
                        )) {
                            const label = trim(el.value || el.textContent);
                            if (label !== 'OK') continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            el.click();
                            return true;
                        }
                        return false;
                    };

                    for (const win of document.querySelectorAll(
                        'div.rwWindow, div.rwDialog, div.rwContent'
                    )) {
                        const text = win.textContent || '';
                        if (
                            !text.includes('Customize Filter')
                            && !text.includes('Based on Measure')
                            && !text.includes('Count Manner')
                        ) {
                            continue;
                        }
                        const r = win.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (clickOk(win)) return true;
                    }

                    if (document.querySelector('#ddlTBBase')) {
                        for (const win of document.querySelectorAll(
                            'div.rwWindow, div.rwDialog'
                        )) {
                            const r = win.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            if (clickOk(win)) return true;
                        }
                        return clickOk(document.body);
                    }
                    return false;
                }
                """
            )
        )

    def _click_ok_in_any_frame(self, dialog_title: str) -> bool:
        """Click OK across frames, but only inside a frame that actually hosts
        the target dialog (matched by its title or a customize-form signature).

        Survives ASP.NET postbacks that detach/reload the dialog iframe, without
        clicking an unrelated OK button elsewhere on the page."""
        js = """
            ([dialogTitle]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const bodyText = document.body ? document.body.innerText : '';
                const hosts = bodyText.includes(dialogTitle)
                    || bodyText.includes('Based on Measure');
                if (!hosts) return false;
                for (const el of document.querySelectorAll(
                    'input[type="submit"], input[type="button"], button, a'
                )) {
                    if (trim(el.value || el.textContent).toUpperCase() !== 'OK') {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    el.click();
                    return true;
                }
                return false;
            }
        """
        targets = [self.page]
        try:
            targets.extend(self.page.frames)
        except Exception:
            pass
        for target in targets:
            try:
                if target.evaluate(js, [dialog_title]):
                    logger.info("Clicked OK button (frame scan)")
                    return True
            except Exception:
                continue
        return False

    def _click_dialog_ok_once(self, dialog_title: str, preferred_scope) -> bool:
        scopes: list = []
        if preferred_scope is not None:
            scopes.append(preferred_scope)
        found = self._scope_for_open_dialog(dialog_title)
        if found is not None and found not in scopes:
            scopes.append(found)
        for scope in self._all_dialog_scopes():
            if scope not in scopes:
                scopes.append(scope)

        for scope in scopes:
            try:
                if self._click_dialog_ok_js(scope, dialog_title):
                    logger.info("Clicked OK on %r dialog (JS)", dialog_title)
                    return True
                if self._click_rw_window_ok_js(scope):
                    logger.info(
                        "Clicked OK on %r dialog (RadWindow JS)", dialog_title
                    )
                    return True
            except Exception:
                pass

            ok_buttons = scope.get_by_role("button", name="OK")
            for index in range(ok_buttons.count()):
                button = ok_buttons.nth(index)
                try:
                    if button.is_visible(timeout=300):
                        button.click(timeout=2_000)
                        logger.info("Clicked OK on %r dialog", dialog_title)
                        return True
                except PlaywrightTimeoutError:
                    continue

            for sel in (
                'input[type="button"][value="OK"]',
                'input[type="submit"][value="OK"]',
                'input.rwOkBtn',
                'a.rwPopupButton',
            ):
                input_ok = scope.locator(sel)
                for index in range(input_ok.count()):
                    button = input_ok.nth(index)
                    try:
                        if button.is_visible(timeout=300):
                            button.click(timeout=2_000)
                            logger.info("Clicked OK on %r dialog", dialog_title)
                            return True
                    except PlaywrightTimeoutError:
                        continue

        # Last resort: scan frames that host this dialog for a submit OK.
        if self._click_ok_in_any_frame(dialog_title):
            return True
        return False

    def _click_dialog_ok(
        self, dialog_title: str, preferred_scope: object | None = None
    ) -> None:
        # The customize/filter dialogs trigger ASP.NET postbacks (e.g. after
        # changing "Based on Measure") that briefly detach the OK button while
        # the iframe re-renders — poll until the click lands.
        for attempt in range(14):
            if self._click_dialog_ok_once(dialog_title, preferred_scope):
                return
            try:
                self.page.wait_for_timeout(700)
            except Exception:
                pass
        raise PlaywrightTimeoutError(
            f"Could not find OK button on {dialog_title!r} dialog"
        )

    def _set_customize_filter_field(self, field_label: str, value: str) -> None:
        for scope in self._filter_dialog_scopes():
            row = scope.locator("tr").filter(has_text=field_label).first
            if row.count() > 0:
                select = row.locator("select").first
                if select.count() > 0:
                    select.select_option(label=value, force=True)
                    logger.info(
                        "Set %r to %r in Customize Filter Condition dialog",
                        field_label,
                        value,
                    )
                    return

                combo_input = row.locator(
                    "input.rcbInput, input[type='text']"
                ).first
                if combo_input.count() > 0:
                    combo_input.click()
                    scope.wait_for_timeout(400)
                    option = scope.get_by_text(value, exact=True)
                    if option.count() > 0:
                        option.first.click(timeout=5_000)
                        logger.info(
                            "Set %r to %r in Customize Filter Condition dialog",
                            field_label,
                            value,
                        )
                        return

            updated = scope.evaluate(
                """
                ([fieldLabel, fieldValue]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const label of document.querySelectorAll('td, label, span')) {
                        if (trim(label.textContent) !== fieldLabel) continue;
                        const row = label.closest('tr') || label.parentElement;
                        if (!row) continue;

                        const select = row.querySelector('select');
                        if (select) {
                            for (const opt of select.options) {
                                const text = trim(opt.textContent);
                                if (text === fieldValue || text.includes(fieldValue)) {
                                    select.value = opt.value;
                                    select.dispatchEvent(
                                        new Event('change', { bubbles: true })
                                    );
                                    return true;
                                }
                            }
                        }
                    }
                    return false;
                }
                """,
                [field_label, value],
            )
            if updated:
                logger.info(
                    "Set %r to %r in Customize Filter Condition dialog (JS)",
                    field_label,
                    value,
                )
                return

        raise PlaywrightTimeoutError(
            f"Could not set {field_label!r} to {value!r} in "
            f"{self.customize_filter_dialog_title!r} dialog"
        )

    def _customize_filter_dialog_scope(self, scope):
        """Return scope for Customize Filter — avoid over-narrowing to title bar only."""
        try:
            scope.get_by_text(
                self.customize_filter_dialog_title, exact=False
            ).first.wait_for(state="visible", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        return scope

    def _customize_filter_dialog_scopes(self) -> list:
        """Frames/pages where Customize Filter Condition is visible."""
        scopes: list = []
        for scope in self._filter_dialog_scopes():
            if scope not in scopes:
                scopes.append(scope)
        found = self._scope_for_open_dialog(self.customize_filter_dialog_title)
        if found is not None and found not in scopes:
            scopes.insert(0, found)
        for scope in self._all_dialog_scopes():
            if scope in scopes:
                continue
            try:
                if scope.evaluate(
                    """
                    ([title]) => (document.body?.innerText || '').includes(title)
                    """,
                    [self.customize_filter_dialog_title],
                ):
                    scopes.append(scope)
            except Exception:
                continue
        return scopes

    def _find_customize_measure_click_point(self, scope) -> dict | None:
        """Return {x, y} for the Based on Measure Units combobox."""
        title = self.customize_filter_dialog_title
        try:
            return scope.evaluate(
                """
                ([dialogTitle]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let dialogRoot = null;
                    let bestArea = 0;
                    const body = document.body?.innerText || '';
                    if (!body.includes('Based on Measure')
                        && !body.includes('Count Manner')
                        && !body.includes(dialogTitle)) {
                        return null;
                    }
                    for (const el of document.querySelectorAll(
                        'div.rwWindow, div.rwDialog, div, table'
                    )) {
                        const text = el.textContent || '';
                        if (!text.includes('Based on Measure')
                            && !text.includes('Count Manner')) {
                            continue;
                        }
                        const r = el.getBoundingClientRect();
                        if (r.width < 180 || r.height < 120) continue;
                        const area = r.width * r.height;
                        if (area > bestArea) {
                            bestArea = area;
                            dialogRoot = el;
                        }
                    }
                    if (!dialogRoot) return null;

                    const pointFor = (el) => {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return null;
                        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                    };

                    for (const label of dialogRoot.querySelectorAll(
                        'td, label, span, div'
                    )) {
                        const t = trim(label.textContent);
                        if (t !== 'Based on Measure' && t !== 'Based on Measure:') {
                            continue;
                        }
                        const row = label.closest('tr') || label.parentElement;
                        if (!row) continue;
                        for (const sel of [
                            'input.rcbInput',
                            '.rcbArrowCell a',
                            '.RadComboBox',
                            'input[type="text"]',
                        ]) {
                            const el = row.querySelector(sel);
                            const pt = el ? pointFor(el) : null;
                            if (pt) return pt;
                        }
                    }

                    for (const inp of dialogRoot.querySelectorAll(
                        'input.rcbInput, input[type="text"]'
                    )) {
                        if (trim(inp.value) !== 'Units') continue;
                        const pt = pointFor(inp);
                        if (pt) return pt;
                    }
                    return null;
                }
                """,
                [title],
            )
        except Exception:
            return None

    def _wait_for_customize_measure_ready(self) -> object | None:
        """Wait until Based on Measure row is visible."""
        for scope in self._customize_filter_dialog_scopes():
            try:
                scope.get_by_text("Based on Measure", exact=False).first.wait_for(
                    state="visible", timeout=12_000
                )
                logger.info("Based on Measure row ready")
                return scope
            except PlaywrightTimeoutError:
                pass
            for _ in range(12):
                if self._find_customize_measure_click_point(scope):
                    logger.info("Based on Measure combobox ready")
                    return scope
                try:
                    scope.wait_for_timeout(500)
                except Exception:
                    break
        logger.info("Based on Measure row not confirmed — continuing")
        return None

    def _open_units_measure_dropdown(self, scope=None) -> bool:
        """Click the Based on Measure 'Units' box to open the dropdown list."""
        scopes = self._customize_filter_dialog_scopes()
        if scope is not None and scope not in scopes:
            scopes.insert(0, scope)

        for try_scope in scopes:
            if self._click_customize_units_dropdown_playwright(try_scope):
                logger.info("Opened Based on Measure dropdown (label offset)")
                return True
            if self._click_customize_units_dropdown(try_scope):
                logger.info("Opened Based on Measure dropdown (JS click)")
                return True
            point = self._find_customize_measure_click_point(try_scope)
            if point:
                logger.info("Clicking Based on Measure 'Units' dropdown…")
                self._click_scope_point(try_scope, point["x"], point["y"])
                return True

            row = try_scope.locator("tr").filter(has_text="Based on Measure").first
            if row.count() > 0:
                combo = row.locator(".RadComboBox").first
                try:
                    if combo.count() > 0 and combo.is_visible(timeout=800):
                        logger.info("Clicking Based on Measure RadCombo…")
                        combo.click(timeout=3_000, force=True)
                        return True
                except PlaywrightTimeoutError:
                    pass
        return False

    def _set_customize_filter_count(self, count: int) -> None:
        count_text = str(count)
        for scope in self._filter_dialog_scopes():
            for label in ("Count", "Count:"):
                row = scope.locator("tr").filter(
                    has=scope.get_by_text(label, exact=True)
                ).first
                if row.count() == 0:
                    continue
                for inp_sel in (
                    "input.riTextBox",
                    "input[type='text']:not(.rcbInput)",
                    "input:not([type='hidden']):not(.rcbInput)",
                ):
                    inp = row.locator(inp_sel).first
                    try:
                        if inp.count() == 0 or not inp.is_visible(timeout=500):
                            continue
                        inp.click(timeout=2_000)
                        inp.fill("")
                        inp.fill(count_text)
                        logger.info(
                            "Set Count to %s in Customize Filter Condition dialog",
                            count_text,
                        )
                        return
                    except PlaywrightTimeoutError:
                        continue

            result = scope.evaluate(
                """
                ([countText, dialogTitle]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const isCountLabel = (t) => t === 'Count' || t === 'Count:';
                    let dialog = null;
                    let bestArea = Infinity;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes(dialogTitle)) continue;
                        if (!text.includes('Based on Measure')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const area = r.width * r.height;
                        if (area < bestArea) {
                            bestArea = area;
                            dialog = el;
                        }
                    }
                    if (!dialog) return false;

                    for (const label of dialog.querySelectorAll(
                        'td, label, span, div'
                    )) {
                        const t = trim(label.textContent);
                        if (!isCountLabel(t)) continue;
                        const row = label.closest('tr') || label.parentElement;
                        const input = row?.querySelector(
                            'input.riTextBox, input[type="text"]:not(.rcbInput), '
                            + 'input:not([type="hidden"]):not(.rcbInput)'
                        );
                        if (!input) continue;
                        input.focus();
                        input.value = countText;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    return false;
                }
                """,
                [count_text, self.customize_filter_dialog_title],
            )
            if result:
                logger.info(
                    "Set Count to %s in Customize Filter Condition dialog (JS)",
                    count_text,
                )
                return

        raise PlaywrightTimeoutError(
            f"Could not set Count to {count_text} in "
            f"{self.customize_filter_dialog_title!r} dialog"
        )

    def _click_scope_point(self, scope, x: float, y: float) -> None:
        if scope is self.page:
            self.page.mouse.click(x, y)
        else:
            px, py = self._frame_page_point(scope, x, y)
            self.page.mouse.click(px, py)

    def _customize_filter_count_current(self, scope) -> str:
        return scope.evaluate(
            """
            ([dialogTitle]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                for (const el of document.querySelectorAll('div, table')) {
                    const text = el.textContent || '';
                    if (!text.includes(dialogTitle)) continue;
                    if (!text.includes('Based on Measure')) continue;
                    for (const inp of el.querySelectorAll('input')) {
                        const r = inp.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (inp.classList.contains('rcbInput')) continue;
                        const v = trim(inp.value);
                        if (/^\\d+$/.test(v)) return v;
                    }
                }
                return '';
            }
            """,
            [self.customize_filter_dialog_title],
        )

    def _apply_customize_filter_via_js(self, scope, count: int, measure: str) -> bool:
        """Set Display Range + Count + Based on Measure + OK in one JS pass."""
        result = scope.evaluate(
            """
            ([countText, measureName, dialogTitle, displayRangeLabel, displayRangeValue]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                let dialog = null;
                let bestArea = Infinity;
                for (const el of document.querySelectorAll(
                    'div.rwWindow, div.rwDialog, div, table, form'
                )) {
                    const text = el.textContent || '';
                    const hasForm = text.includes('Based on Measure')
                        || text.includes('Count Manner');
                    if (!hasForm) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const area = r.width * r.height;
                    if (area < bestArea) {
                        bestArea = area;
                        dialog = el;
                    }
                }
                if (!dialog) return false;

                let displayRangeSet = false;
                const rangeMatch = displayRangeValue.toLowerCase();
                for (const label of dialog.querySelectorAll('td, label, span')) {
                    const t = trim(label.textContent);
                    if (t !== displayRangeLabel && t !== displayRangeLabel + ':') {
                        continue;
                    }
                    const row = label.closest('tr') || label.parentElement;
                    const select = row?.querySelector('select');
                    if (!select) continue;
                    for (const opt of select.options) {
                        const text = trim(opt.textContent).toLowerCase();
                        if (
                            text === rangeMatch
                            || text.includes('show top')
                            || text.includes('top only')
                        ) {
                            select.value = opt.value;
                            select.dispatchEvent(
                                new Event('change', { bubbles: true })
                            );
                            displayRangeSet = true;
                            break;
                        }
                    }
                    if (displayRangeSet) break;
                }
                if (!displayRangeSet) return false;

                for (const label of dialog.querySelectorAll(
                    'td, label, span, div'
                )) {
                    const t = trim(label.textContent);
                    if (t !== 'Count' && t !== 'Count:') continue;
                    const row = label.closest('tr') || label.parentElement;
                    const input = row?.querySelector(
                        'input.riTextBox, input[type="text"]:not(.rcbInput), '
                        + 'input:not([type="hidden"]):not(.rcbInput)'
                    );
                    if (!input) continue;
                    if (trim(input.value) !== countText) {
                        input.focus();
                        input.value = countText;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    break;
                }

                let measureSet = false;
                for (const sel of dialog.querySelectorAll('select')) {
                    const opts = [...sel.options].map((o) => trim(o.textContent));
                    if (!opts.includes('Units') || !opts.includes('Values')) {
                        continue;
                    }
                    for (const opt of sel.options) {
                        const text = trim(opt.textContent);
                        if (text !== measureName && !text.includes(measureName)) {
                            continue;
                        }
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        measureSet = true;
                        break;
                    }
                    if (measureSet) break;
                }

                if (!measureSet) {
                    for (const combo of dialog.querySelectorAll('.RadComboBox')) {
                        const input = combo.querySelector('input.rcbInput, input');
                        if (!input) continue;
                        const current = trim(input.value);
                        if (current === measureName) {
                            measureSet = true;
                            break;
                        }
                        if (current !== 'Units' && !current.startsWith('Units')) {
                            continue;
                        }
                        const widget = window.$find ? window.$find(combo.id) : null;
                        if (widget && widget.findItemByText) {
                            const item = widget.findItemByText(measureName);
                            if (item) {
                                widget.selectItem(item.get_index());
                                measureSet = true;
                                break;
                            }
                        }
                        if (widget && widget.get_items) {
                            const items = widget.get_items();
                            for (let i = 0; i < items.get_count(); i++) {
                                const text = trim(items.getItem(i).get_text());
                                if (text !== measureName) continue;
                                widget.selectItem(i);
                                measureSet = true;
                                break;
                            }
                        }
                        if (measureSet) break;
                    }
                }
                if (!measureSet) return false;

                const roots = [
                    dialog,
                    dialog.closest('.rwWindow'),
                    dialog.parentElement,
                ];
                for (const root of roots) {
                    if (!root) continue;
                    for (const el of root.querySelectorAll(
                        'input[type="button"], button, a'
                    )) {
                        const label = trim(el.value || el.textContent);
                        if (label !== 'OK') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        el.click();
                        return true;
                    }
                }
                return false;
            }
            """,
            [str(count), measure, self.customize_filter_dialog_title,
             self.customize_display_range_label, self.custom_top_display_range_value],
        )
        return bool(result)

    def _try_set_customize_filter_count(self, count: int) -> None:
        """Best-effort Count — Custom dialog often defaults to 5."""
        count_text = str(count)
        for scope in self._all_dialog_scopes():
            current = self._customize_filter_count_current(scope)
            if current == count_text:
                logger.info(
                    "Count already %s in Customize Filter Condition dialog",
                    count_text,
                )
                return
        try:
            self._set_customize_filter_count(count)
        except PlaywrightTimeoutError:
            logger.info(
                "Count not changed — continuing (dialog may already show %s)",
                count_text,
            )

    def _scopes_for_customize_dialog(self) -> list:
        """All frames/pages that contain the Customize Filter Condition form."""
        title = self.customize_filter_dialog_title
        scopes: list = []
        for scope in self._all_dialog_scopes():
            try:
                has_form = scope.evaluate(
                    """
                    ([dialogTitle]) => {
                        const text = document.body?.innerText || '';
                        return text.includes(dialogTitle)
                            && text.includes('Based on Measure');
                    }
                    """,
                    [title],
                )
                if has_form:
                    scopes.append(scope)
            except Exception:
                continue
        if scopes:
            return scopes
        found = self._scope_for_open_dialog(title)
        return [found] if found else self._filter_dialog_scopes()

    def _customize_measure_current(self, scope) -> str:
        try:
            return scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const label of document.querySelectorAll(
                        'td, label, span, div'
                    )) {
                        const t = trim(label.textContent);
                        if (t !== 'Based on Measure' && t !== 'Based on Measure:') {
                            continue;
                        }
                        const row = label.closest('tr') || label.parentElement;
                        if (!row) continue;
                        const sel = row.querySelector('select');
                        if (sel && sel.selectedIndex >= 0) {
                            return trim(sel.options[sel.selectedIndex].textContent);
                        }
                        const input = row.querySelector(
                            'input.rcbInput, input[type="text"]'
                        );
                        if (input) return trim(input.value);
                    }
                    for (const inp of document.querySelectorAll('input.rcbInput')) {
                        const v = trim(inp.value);
                        if (v === 'Units' || v === 'Values') return v;
                    }
                    return '';
                }
                """
            )
        except Exception:
            return ""

    def _pick_customize_measure_combo_js(self, scope, measure: str) -> bool:
        """Open Units RadCombo beside Based on Measure and pick Values."""
        return bool(
            scope.evaluate(
                """
                ([measureName]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();

                    const pickFromList = () => {
                        for (const item of document.querySelectorAll(
                            '.rcbItem, .RadComboBoxDropDown li, .rcbSlide li, '
                            + 'div.rcbList li, li.rcbItem'
                        )) {
                            if (trim(item.textContent) !== measureName) continue;
                            const r = item.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            item.click();
                            return true;
                        }
                        return false;
                    };

                    const tryRow = (row) => {
                        if (!row) return false;
                        const input = row.querySelector('input.rcbInput, input[type="text"]');
                        const current = input ? trim(input.value) : '';
                        if (current === measureName) return true;

                        const arrow = row.querySelector(
                            '.rcbArrowCell a, .rcbArrowCell, td.rcbArrowCell'
                        );
                        const widgetHost = row.querySelector('.RadComboBox');
                        const widget = widgetHost?.id && window.$find
                            ? window.$find(widgetHost.id)
                            : null;

                        if (widget?.findItemByText) {
                            const item = widget.findItemByText(measureName);
                            if (item) {
                                widget.selectItem(item.get_index());
                                return true;
                            }
                        }
                        if (widget?.showDropDown) {
                            widget.showDropDown();
                            if (pickFromList()) return true;
                        }
                        if (arrow) {
                            arrow.click();
                            if (pickFromList()) return true;
                        }
                        if (input) {
                            input.focus();
                            input.click();
                            if (pickFromList()) return true;
                            input.dispatchEvent(new KeyboardEvent('keydown', {
                                key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, bubbles: true,
                            }));
                            input.dispatchEvent(new KeyboardEvent('keydown', {
                                key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true,
                            }));
                            return trim(input.value) === measureName
                                || trim(input.value).includes(measureName);
                        }
                        const sel = row.querySelector('select');
                        if (sel) {
                            for (const opt of sel.options) {
                                if (trim(opt.textContent) !== measureName) continue;
                                sel.value = opt.value;
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                return true;
                            }
                        }
                        return false;
                    };

                    for (const label of document.querySelectorAll(
                        'td, label, span, div'
                    )) {
                        const t = trim(label.textContent);
                        if (t !== 'Based on Measure' && t !== 'Based on Measure:') continue;
                        if (tryRow(label.closest('tr') || label.parentElement)) {
                            return true;
                        }
                    }

                    for (const combo of document.querySelectorAll('.RadComboBox')) {
                        const input = combo.querySelector('input.rcbInput, input');
                        if (!input) continue;
                        const current = trim(input.value);
                        if (current === measureName) return true;
                        if (current !== 'Units' && !current.startsWith('Units')) continue;
                        if (tryRow(combo.closest('tr') || combo)) return true;
                    }
                    return false;
                }
                """,
                [measure],
            )
        )

    def _measure_selection_matches(self, scope, measure: str) -> bool:
        current = self._customize_measure_current(scope)
        return current == measure or measure in current

    def _click_values_in_measure_dropdown(self) -> bool:
        """Pick 'Values' from the open Based on Measure dropdown list."""
        for frame_scope in self._all_dialog_scopes():
            point = frame_scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const item of document.querySelectorAll(
                        '.rcbItem, .RadComboBoxDropDown li, div.rcbSlide li, li.rcbItem'
                    )) {
                        if (trim(item.textContent) !== 'Values') continue;
                        const r = item.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                    }
                    return null;
                }
                """
            )
            if point:
                self._click_scope_point(frame_scope, point["x"], point["y"])
                logger.info(
                    "Selected 'Values' from Based on Measure dropdown"
                )
                return True

        for frame_scope in self._all_dialog_scopes():
            for panel_sel in (
                ".RadComboBoxDropDown",
                "div.rcbSlide",
                "div[id*='DropDown']",
            ):
                panels = frame_scope.locator(panel_sel)
                for panel_index in range(panels.count()):
                    panel = panels.nth(panel_index)
                    try:
                        if not panel.is_visible(timeout=250):
                            continue
                    except PlaywrightTimeoutError:
                        continue
                    option = panel.get_by_text("Values", exact=True).first
                    try:
                        if option.count() > 0 and option.is_visible(timeout=800):
                            option.click(force=True, timeout=2_000)
                            logger.info(
                                "Selected 'Values' from Based on Measure dropdown"
                            )
                            return True
                    except PlaywrightTimeoutError:
                        continue

            for sel in (".rcbItem", "li.rcbItem", ".RadComboBoxDropDown li"):
                items = frame_scope.locator(sel)
                for index in range(items.count()):
                    item = items.nth(index)
                    try:
                        if not item.is_visible(timeout=200):
                            continue
                        if item.inner_text(timeout=300).strip() != "Values":
                            continue
                        item.click(force=True, timeout=2_000)
                        logger.info("Selected 'Values' from dropdown list")
                        return True
                    except PlaywrightTimeoutError:
                        continue
        return False

    def _select_measure_from_units_combobox(self, scope, measure: str) -> bool:
        """Click Units → select Values from the dropdown (manual user path)."""
        if measure != "Values":
            return False

        for try_scope in self._customize_filter_dialog_scopes():
            if self._measure_selection_matches(try_scope, measure):
                return True

            if not self._open_units_measure_dropdown(try_scope):
                continue

            try_scope.wait_for_timeout(500)
            if self._click_values_in_measure_dropdown():
                try_scope.wait_for_timeout(200)
                if self._measure_selection_matches(try_scope, measure):
                    return True

            try_scope.page.keyboard.press("ArrowDown")
            try_scope.page.keyboard.press("Enter")
            try_scope.wait_for_timeout(300)
            if self._measure_selection_matches(try_scope, measure):
                logger.info(
                    "Set Based on Measure to %r (Units dropdown + keyboard)",
                    measure,
                )
                return True
        return False

    def _select_customize_measure_native_select(
        self, scope, measure: str
    ) -> bool:
        """Pick Values from a native <select> (IQVIA uses selects, not RadCombo here)."""
        selects = scope.locator("select")
        for index in range(selects.count()):
            sel = selects.nth(index)
            try:
                opts = sel.evaluate(
                    """
                    (el) => [...el.options].map(
                        (o) => (o.textContent || '').replace(/\\s+/g, ' ').trim()
                    )
                    """
                )
            except PlaywrightTimeoutError:
                continue
            if not any(o in opts for o in ("Units", "Values")) and not (
                any("units" in o.lower() for o in opts)
                and any("values" in o.lower() for o in opts)
            ):
                continue
            if not any(
                measure == opt
                or measure.lower() in opt.lower()
                for opt in opts
            ):
                continue
            try:
                sel.select_option(label=measure, force=True)
                logger.info(
                    "Set Based on Measure to %r (native select #%s, opts=%s)",
                    measure,
                    index,
                    opts,
                )
                return True
            except PlaywrightTimeoutError:
                pass
            try:
                sel.select_option(value=measure, force=True)
                logger.info(
                    "Set Based on Measure to %r (native select value #%s)",
                    measure,
                    index,
                )
                return True
            except PlaywrightTimeoutError:
                pass
            picked = sel.evaluate(
                """
                (el, measureName) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const opt of el.options) {
                        const text = trim(opt.textContent);
                        if (text === measureName || text.includes(measureName)) {
                            el.value = opt.value;
                            el.dispatchEvent(
                                new Event('change', { bubbles: true })
                            );
                            el.dispatchEvent(
                                new Event('blur', { bubbles: true })
                            );
                            return true;
                        }
                    }
                    return false;
                }
                """,
                measure,
            )
            if picked:
                logger.info(
                    "Set Based on Measure to %r (native select JS #%s)",
                    measure,
                    index,
                )
                return True
        return False

    def _select_customize_measure_js(self, scope, measure: str) -> bool:
        """Set Based on Measure via hidden select, RadCombo API, or Units→Values click."""
        title = self.customize_filter_dialog_title
        try:
            result = scope.evaluate(
                """
                ([dialogTitle, measureName]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const body = document.body?.innerText || '';
                    const inForm = body.includes('Based on Measure')
                        || body.includes('Count Manner')
                        || body.includes('Based on Member')
                        || body.includes(dialogTitle)
                        || document.querySelectorAll('select').length > 0;
                    if (!inForm) {
                        return { ok: false, reason: 'no form', snippet: body.slice(0, 180) };
                    }

                    const syncCombo = (sel) => {
                        const host = sel.closest('.RadComboBox')
                            || (sel.id && document.querySelector(
                                `.RadComboBox[id$='${sel.id.split('_').pop()}']`
                            ));
                        const comboId = host?.id || sel.id;
                        const widget = comboId && window.$find
                            ? window.$find(comboId)
                            : null;
                        if (widget?.findItemByText) {
                            const item = widget.findItemByText(measureName);
                            if (item) {
                                widget.selectItem(item.get_index());
                                widget.closeDropDown?.();
                                return true;
                            }
                        }
                        return false;
                    };

                    for (const sel of document.querySelectorAll('select')) {
                        const opts = [...sel.options].map((o) =>
                            trim(o.textContent)
                        );
                        if (!opts.includes('Units') || !opts.includes('Values')) {
                            continue;
                        }
                        for (const opt of sel.options) {
                            if (trim(opt.textContent) !== measureName) continue;
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            syncCombo(sel);
                            return { ok: true, via: 'select' };
                        }
                    }

                    const pickValues = () => {
                        for (const item of document.querySelectorAll(
                            '.rcbItem, .RadComboBoxDropDown li, div.rcbSlide li'
                        )) {
                            if (trim(item.textContent) !== measureName) continue;
                            const r = item.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            item.click();
                            return true;
                        }
                        return false;
                    };

                    for (const label of document.querySelectorAll(
                        'td, label, span, div'
                    )) {
                        const t = trim(label.textContent);
                        if (t !== 'Based on Measure' && t !== 'Based on Measure:') {
                            continue;
                        }
                        const row = label.closest('tr') || label.parentElement;
                        if (!row) continue;
                        for (const sel of [
                            'input.rcbInput',
                            '.rcbArrowCell a',
                            '.RadComboBox',
                            'input[type="text"]',
                        ]) {
                            const el = row.querySelector(sel);
                            if (!el) continue;
                            el.click();
                            if (pickValues()) {
                                return { ok: true, via: 'row click' };
                            }
                        }
                    }

                    for (const inp of document.querySelectorAll(
                        'input.rcbInput, input[type="text"]'
                    )) {
                        if (trim(inp.value) !== 'Units') continue;
                        inp.click();
                        if (pickValues()) {
                            return { ok: true, via: 'units input click' };
                        }
                    }

                    return {
                        ok: false,
                        reason: 'controls not found',
                        hasMeasure: body.includes('Based on Measure'),
                        selects: document.querySelectorAll('select').length,
                        combos: document.querySelectorAll('.RadComboBox').length,
                    };
                }
                """,
                [title, measure],
            )
        except Exception as exc:
            logger.info("Measure JS failed: %s", exc)
            return False

        if result and result.get("ok"):
            logger.info(
                "Set Based on Measure to %r (JS %s)", measure, result.get("via")
            )
            return True
        if result:
            logger.info("Measure JS diagnostic: %s", result)
        return False

    def _select_customize_based_on_measure(self, measure: str) -> None:
        """Set Based on Measure — click Units, then pick Values from dropdown."""
        measure = measure.strip()
        scopes: list = []
        dialog_scope = self._scope_for_open_dialog(
            self.customize_filter_dialog_title
        )
        if dialog_scope is not None:
            scopes.append(dialog_scope)
        for scope in self._customize_filter_dialog_scopes():
            if scope not in scopes:
                scopes.append(scope)
        if not scopes:
            scopes = self._scopes_for_customize_dialog()

        for scope in scopes:
            current = self._customize_measure_current(scope)
            if current == measure or measure in current:
                logger.info("Based on Measure already %r", current)
                return
            if self._select_customize_measure_native_select(scope, measure):
                if self._measure_selection_matches(scope, measure):
                    return
            if self._select_customize_measure_js(scope, measure):
                if self._measure_selection_matches(scope, measure):
                    return
            if self._select_measure_from_units_combobox(scope, measure):
                return
            if self._set_customize_measure_via_select(scope, measure):
                logger.info(
                    "Set Based on Measure to %r (hidden select)", measure
                )
                return
            if self._pick_customize_measure_combo_js(scope, measure):
                if self._measure_selection_matches(scope, measure):
                    logger.info("Set Based on Measure to %r (combo JS)", measure)
                    return

        try:
            self._set_customize_filter_based_on_measure(measure)
            return
        except PlaywrightTimeoutError:
            pass

        raise PlaywrightTimeoutError(
            f"Could not set Based on Measure to {measure!r} in "
            f"{self.customize_filter_dialog_title!r} dialog"
        )

    def _wait_for_real_customize_dialog(self, timeout_ms: int = 20_000):
        """Wait until the visible Customize Filter form (with Units/Values) exists."""
        title = self.customize_filter_dialog_title
        poll_ms = 400
        elapsed = 0
        while elapsed < timeout_ms:
            for scope in self._walk_page_frames():
                try:
                    info = scope.evaluate(
                        """
                        () => {
                            const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                            const body = document.body?.innerText || '';
                            const hasForm = body.includes('Based on Measure')
                                || body.includes('Count Manner');
                            if (!hasForm) return { ready: false };

                            for (const sel of document.querySelectorAll('select')) {
                                const opts = [...sel.options].map((o) =>
                                    trim(o.textContent)
                                );
                                if (opts.includes('Units') && opts.includes('Values')) {
                                    return { ready: true, via: 'measure-select', opts };
                                }
                            }
                            for (const inp of document.querySelectorAll('input')) {
                                const v = trim(inp.value);
                                if (v === 'Units' || v === 'Values') {
                                    return { ready: true, via: 'input', value: v };
                                }
                            }
                            if (body.includes('Display Range') || body.includes('Count')) {
                                return { ready: true, via: 'dialog-text' };
                            }
                            return { ready: false };
                        }
                        """
                    )
                    if info and info.get("ready"):
                        logger.info("Customize Filter form verified: %s", info)
                        return scope
                except Exception:
                    continue
            try:
                self._designer_frame().wait_for_timeout(poll_ms)
            except Exception:
                self.page.wait_for_timeout(poll_ms)
            elapsed += poll_ms
        return None

    def _resolve_customize_filter_scope(
        self, timeout_ms: int = 15_000
    ) -> tuple[object | None, dict | None]:
        """Return the frame/page that owns the Customize Filter form controls."""
        poll_ms = 200
        elapsed = 0
        while elapsed < timeout_ms:
            for scope in self._walk_page_frames():
                try:
                    info = scope.evaluate(
                        """
                        ([dialogTitle]) => {
                            const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                            const body = document.body?.innerText || '';

                            for (const sel of document.querySelectorAll('select')) {
                                const opts = [...sel.options].map((o) =>
                                    trim(o.textContent)
                                );
                                const lower = opts.map((o) => o.toLowerCase());
                                const hasUnits = lower.some((o) =>
                                    o.includes('units')
                                );
                                const hasValues = lower.some((o) =>
                                    o.includes('values')
                                );
                                if (hasUnits && hasValues) {
                                    return {
                                        ready: true,
                                        via: 'measure-select',
                                        opts,
                                        selectId: sel.id || '',
                                    };
                                }
                            }

                            for (const inp of document.querySelectorAll(
                                'input.rcbInput, input[type="text"]'
                            )) {
                                const v = trim(inp.value);
                                if (v === 'Units' || v === 'Values') {
                                    return { ready: true, via: 'rcbInput', value: v };
                                }
                            }

                            if (
                                body.includes(dialogTitle)
                                && (
                                    body.includes('Based on Measure')
                                    || body.includes('Count Manner')
                                    || body.includes('Based on Member')
                                    || body.includes('Display Range')
                                )
                            ) {
                                return { ready: true, via: 'dialog-text' };
                            }

                            for (const win of document.querySelectorAll(
                                'div.rwWindow, div.rwDialog'
                            )) {
                                const text = win.textContent || '';
                                if (!text.includes(dialogTitle)) continue;
                                const r = win.getBoundingClientRect();
                                if (r.width < 180 || r.height < 100) continue;
                                if (
                                    text.includes('Based on Measure')
                                    || text.includes('Count Manner')
                                ) {
                                    return { ready: true, via: 'rwWindow' };
                                }
                            }
                            return { ready: false };
                        }
                        """,
                        [self.customize_filter_dialog_title],
                    )
                except Exception:
                    continue
                if info and info.get("ready"):
                    logger.info("Customize Filter scope resolved: %s", info)
                    return scope, info
            try:
                self._designer_frame().wait_for_timeout(poll_ms)
            except Exception:
                self.page.wait_for_timeout(poll_ms)
            elapsed += poll_ms
        return None, None

    def _force_set_measure_on_any_select(self, measure: str) -> bool:
        """Last resort — pick *measure* from any visible select in any frame."""
        measure = measure.strip()
        for scope in self._all_dialog_scopes():
            try:
                picked = scope.evaluate(
                    """
                    (measureName) => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const match = (text) =>
                            text === measureName
                            || text.toLowerCase().includes(
                                measureName.toLowerCase()
                            );
                        for (const sel of document.querySelectorAll('select')) {
                            const r = sel.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            for (const opt of sel.options) {
                                const text = trim(opt.textContent);
                                if (!match(text)) continue;
                                sel.value = opt.value;
                                sel.dispatchEvent(
                                    new Event('change', { bubbles: true })
                                );
                                sel.dispatchEvent(
                                    new Event('blur', { bubbles: true })
                                );
                                return { ok: true, text, selectId: sel.id || '' };
                            }
                        }
                        return { ok: false };
                    }
                    """,
                    measure,
                )
            except Exception:
                continue
            if picked and picked.get("ok"):
                logger.info(
                    "Set Based on Measure to %r (force select %s)",
                    measure,
                    picked.get("selectId") or picked.get("text"),
                )
                return True
        return False

    def _set_measure_on_customize_scope(self, scope, measure: str) -> None:
        """Set Based on Measure on a verified Customize Filter scope."""
        measure = measure.strip()
        current = self._customize_measure_current(scope)
        if current == measure or measure in current:
            logger.info("Based on Measure already %r", current or measure)
            return

        if self._select_customize_measure_native_select(scope, measure):
            if self._measure_selection_matches(scope, measure):
                return
        if self._pick_customize_measure_combo_js(scope, measure):
            if self._measure_selection_matches(scope, measure):
                logger.info("Set Based on Measure to %r (combo JS)", measure)
                return
        if self._select_customize_measure_js(scope, measure):
            if self._measure_selection_matches(scope, measure):
                return
        if self._select_measure_from_units_combobox(scope, measure):
            return
        if self._set_customize_measure_via_select(scope, measure):
            logger.info("Set Based on Measure to %r (hidden select)", measure)
            return

        try:
            self._set_customize_filter_based_on_measure(measure)
            return
        except PlaywrightTimeoutError:
            pass
        if self._force_set_measure_on_any_select(measure):
            return

        raise PlaywrightTimeoutError(
            f"Could not set Based on Measure to {measure!r} in "
            f"{self.customize_filter_dialog_title!r} dialog"
        )

    def _dump_customize_filter_inputs(self, scope) -> None:
        """Log every visible input in the Customize Filter dialog + its label."""
        try:
            rows = scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const out = [];
                    for (const inp of document.querySelectorAll(
                        'input, select'
                    )) {
                        const r = inp.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const tag = inp.tagName.toLowerCase();
                        const type = inp.getAttribute('type') || '';
                        const cls = inp.className || '';
                        let label = '';
                        let node = inp.closest('tr') || inp.parentElement;
                        if (node) label = trim(node.textContent).slice(0, 40);
                        out.push({
                            tag, type, cls,
                            value: trim(inp.value),
                            x: Math.round(r.left),
                            y: Math.round(r.top),
                            w: Math.round(r.width),
                            label,
                        });
                    }
                    return out;
                }
                """
            )
        except Exception:
            rows = None
        if not rows:
            logger.info("CUSTOMIZE FILTER inputs: none captured")
            return
        logger.info("CUSTOMIZE FILTER inputs (%d):", len(rows))
        for r in rows:
            logger.info(
                "  %s[type=%s cls=%s] val=%r @(%d,%d w%d) label=%r",
                r.get("tag"),
                r.get("type"),
                (r.get("cls") or "")[:24],
                r.get("value"),
                r.get("x"),
                r.get("y"),
                r.get("w"),
                r.get("label"),
            )

    def _read_customize_count_value(self, scope) -> str:
        """Value of the Count text input (matched by a nearby 'Count' label)."""
        try:
            return scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const inp of document.querySelectorAll(
                        'input[type="text"], input.TextBox, input.riTextBox'
                    )) {
                        if (inp.classList.contains('rcbInput')) continue;
                        const r = inp.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const row = inp.closest('tr') || inp.parentElement;
                        const ctx = trim(row ? row.textContent : '');
                        if (/count/i.test(ctx)) return trim(inp.value);
                    }
                    return '';
                }
                """
            ) or ""
        except Exception:
            return ""

    def _set_customize_filter_count_robust(self, scope, count: int) -> None:
        """Set the Count field to *count* and verify; tolerant of layout changes.

        The dialog defaults to 5; if it already shows the target value we leave
        it completely untouched — dispatching input/change/blur events here
        triggers an ASP.NET postback that detaches the OK button.
        """
        count_text = str(count)
        for sc in (scope, *self._all_dialog_scopes()):
            if self._read_customize_count_value(sc) == count_text:
                logger.info(
                    "Count already %s — leaving Customize Filter dialog untouched",
                    count_text,
                )
                return

        # First try the structured (label-based) setter.
        try:
            self._set_customize_filter_count(count)
        except PlaywrightTimeoutError:
            pass

        for sc in self._all_dialog_scopes():
            if self._read_customize_count_value(sc) == count_text:
                logger.info("Count confirmed = %s", count_text)
                return

        # Fallback: set the most plausible numeric text input directly via JS.
        set_ok = False
        for sc in self._all_dialog_scopes():
            try:
                set_ok = bool(
                    sc.evaluate(
                        """
                        ([countText]) => {
                            const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                            const cands = [];
                            for (const inp of document.querySelectorAll('input')) {
                                if (inp.classList.contains('rcbInput')) continue;
                                const type = (inp.getAttribute('type') || '').toLowerCase();
                                if (type === 'hidden' || type === 'checkbox'
                                    || type === 'radio' || type === 'button') continue;
                                const r = inp.getBoundingClientRect();
                                if (r.width <= 0 || r.height <= 0) continue;
                                if (r.width > 120) continue;
                                const row = inp.closest('tr') || inp.parentElement;
                                const ctx = trim(row ? row.textContent : '');
                                const isCount = /count/i.test(ctx);
                                const numeric = /^\\d+$/.test(trim(inp.value));
                                cands.push({ inp, isCount, numeric, left: r.left });
                            }
                            cands.sort((a, b) => {
                                if (a.isCount !== b.isCount) return a.isCount ? -1 : 1;
                                if (a.numeric !== b.numeric) return a.numeric ? -1 : 1;
                                return a.left - b.left;
                            });
                            if (!cands.length) return false;
                            const inp = cands[0].inp;
                            inp.focus();
                            inp.value = '';
                            inp.value = countText;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                            inp.dispatchEvent(new Event('blur', { bubbles: true }));
                            return true;
                        }
                        """,
                        [count_text],
                    )
                )
            except Exception:
                set_ok = False
            if set_ok:
                break

        for sc in self._all_dialog_scopes():
            if self._customize_filter_count_current(sc) == count_text:
                logger.info("Count set to %s (JS fallback)", count_text)
                return
        logger.warning(
            "Could not confirm Count=%s in Customize Filter dialog (set_ok=%s)",
            count_text,
            set_ok,
        )

    def _set_customize_filter_display_range(self, scope) -> None:
        """Set Display Range to Show Top Only (not All Data)."""
        value = self.custom_top_display_range_value
        for candidate in (value, "Show Only the Top"):
            if scope.evaluate(
                """
                ([fieldLabel, fieldValue]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const matchValue = fieldValue.toLowerCase();
                    for (const label of document.querySelectorAll(
                        'td, label, span'
                    )) {
                        const t = trim(label.textContent);
                        if (t !== fieldLabel && t !== fieldLabel + ':') continue;
                        const row = label.closest('tr') || label.parentElement;
                        if (!row) continue;
                        const select = row.querySelector('select');
                        if (!select) continue;
                        for (const opt of select.options) {
                            const text = trim(opt.textContent).toLowerCase();
                            if (
                                text === matchValue
                                || text.includes('show top')
                                || text.includes('top only')
                            ) {
                                select.value = opt.value;
                                select.dispatchEvent(
                                    new Event('change', { bubbles: true })
                                );
                                return true;
                            }
                        }
                    }
                    return false;
                }
                """,
                [self.customize_display_range_label, candidate],
            ):
                logger.info(
                    "Set %r to %r in Customize Filter Condition dialog",
                    self.customize_display_range_label,
                    candidate,
                )
                return
            try:
                row = scope.locator("tr").filter(
                    has_text=self.customize_display_range_label
                ).first
                if row.count() > 0:
                    row.locator("select").first.select_option(
                        label=candidate, force=True
                    )
                    logger.info(
                        "Set %r to %r via select",
                        self.customize_display_range_label,
                        candidate,
                    )
                    return
            except Exception:
                pass
        raise PlaywrightTimeoutError(
            f"Could not set {self.customize_display_range_label!r} to "
            f"{value!r} in {self.customize_filter_dialog_title!r} dialog"
        )

    def _apply_customize_top_count(self, count: int) -> None:
        measure = self.custom_top_based_on_measure
        poll_ms = 400
        deadline = time.time() + 60

        # Form often loads in a nested iframe — try JS apply on every frame.
        while time.time() < deadline:
            for scope in self._walk_page_frames():
                try:
                    if self._apply_customize_filter_via_js(scope, count, measure):
                        logger.info(
                            "Customize Filter applied via JS (Count=%s, Measure=%r)",
                            count,
                            measure,
                        )
                        self._wait_for_query_idle(self._designer_frame())
                        return
                except Exception:
                    continue
            try:
                self._designer_frame().wait_for_timeout(poll_ms)
            except Exception:
                self.page.wait_for_timeout(poll_ms)

        dialog_scope = self._wait_for_real_customize_dialog(timeout_ms=15_000)
        scope_info = None
        if dialog_scope is None:
            dialog_scope, scope_info = self._resolve_customize_filter_scope(
                timeout_ms=15_000
            )
        if dialog_scope is None:
            raise PlaywrightTimeoutError(
                f"{self.customize_filter_dialog_title!r} form controls not found"
            )

        self._dump_customize_filter_inputs(dialog_scope)
        self._set_customize_filter_display_range(dialog_scope)
        self._set_customize_filter_count_robust(dialog_scope, count)
        self._set_measure_on_customize_scope(dialog_scope, measure)
        self._click_dialog_ok(
            self.customize_filter_dialog_title,
            preferred_scope=dialog_scope,
        )

    def _scope_for_open_dialog(self, title: str):
        """Return the frame/page scope where *title* dialog is visible."""
        for scope in self._all_dialog_scopes():
            try:
                scope.get_by_text(title, exact=False).first.wait_for(
                    state="visible", timeout=800
                )
                return scope
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        return None

    def _customize_filter_dialog_open(self) -> bool:
        return self._scope_for_open_dialog(
            self.customize_filter_dialog_title
        ) is not None

    def _set_customize_measure_via_select(self, scope, measure: str) -> bool:
        return bool(
            scope.evaluate(
                """
                ([dialogTitle, measureName]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const pickSelect = (sel) => {
                        for (const opt of sel.options) {
                            const text = trim(opt.textContent);
                            if (text !== measureName && !text.includes(measureName)) {
                                continue;
                            }
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            sel.dispatchEvent(new Event('blur', { bubbles: true }));
                            return true;
                        }
                        return false;
                    };

                    for (const label of document.querySelectorAll(
                        'td, label, span, div'
                    )) {
                        const t = trim(label.textContent);
                        if (t !== 'Based on Measure' && t !== 'Based on Measure:') {
                            continue;
                        }
                        const row = label.closest('tr') || label.parentElement;
                        if (!row) continue;
                        const sel = row.querySelector('select');
                        if (sel && pickSelect(sel)) return true;
                        const input = row.querySelector(
                            'input.rcbInput, input[type="text"]'
                        );
                        if (input && trim(input.value) === measureName) {
                            return true;
                        }
                    }

                    for (const sel of document.querySelectorAll('select')) {
                        const opts = [...sel.options].map((o) =>
                            trim(o.textContent)
                        );
                        if (!opts.includes('Units') || !opts.includes('Values')) {
                            continue;
                        }
                        if (pickSelect(sel)) return true;
                    }
                    return false;
                }
                """,
                [self.customize_filter_dialog_title, measure],
            )
        )

    def _wait_for_customize_dialog_form(self) -> None:
        """Wait until Customize Filter Condition form fields are rendered."""
        title = self.customize_filter_dialog_title.replace("'", "\\'")
        for scope in self._all_dialog_scopes():
            try:
                scope.wait_for_function(
                    f"""
                    () => {{
                        const text = document.body?.innerText || '';
                        return text.includes('{title}')
                            && text.includes('Based on Measure');
                    }}
                    """,
                    timeout=3_000,
                )
                logger.info("Customize Filter Condition form ready")
                return
            except PlaywrightTimeoutError:
                continue
        logger.info(
            "Customize Filter Condition form not confirmed — continuing"
        )

    def _click_customize_units_dropdown_playwright(self, scope) -> bool:
        """Click the Units dropdown beside the Based on Measure label."""
        for label_text in ("Based on Measure", "Based on Measure:"):
            label = scope.get_by_text(label_text, exact=True)
            if label.count() == 0:
                continue
            try:
                label.first.wait_for(state="visible", timeout=2_000)
            except PlaywrightTimeoutError:
                continue

            row = scope.locator("tr").filter(
                has=scope.get_by_text(label_text, exact=True)
            ).first
            for sel in (
                ".rcbArrowCell a",
                "input.rcbInput",
                "input[type='text']",
                "select",
            ):
                target = row.locator(sel).first
                try:
                    if target.count() > 0 and target.is_visible(timeout=500):
                        target.click(timeout=3_000, force=True)
                        return True
                except PlaywrightTimeoutError:
                    continue

            box = label.first.bounding_box()
            if box:
                click_x = box["x"] + box["width"] + 70
                click_y = box["y"] + box["height"] / 2
                self._click_scope_point(scope, click_x, click_y)
                return True
        return False

    def _click_customize_units_dropdown(self, scope) -> bool:
        return bool(
            scope.evaluate(
                """
                ([dialogTitle]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const bodyText = document.body?.innerText || '';
                    if (!bodyText.includes(dialogTitle)) return false;

                    const isMeasureLabel = (t) =>
                        t === 'Based on Measure' || t === 'Based on Measure:';

                    for (const label of document.querySelectorAll(
                        'td, label, span, div'
                    )) {
                        if (!isMeasureLabel(trim(label.textContent))) continue;
                        const container = label.closest('tr')
                            || label.parentElement?.parentElement
                            || label.parentElement;
                        if (!container) continue;
                        for (const sel of [
                            '.rcbArrowCell a',
                            '.rcbArrowCell',
                            'input.rcbInput',
                            'input[type="text"]',
                            'select',
                        ]) {
                            const el = container.querySelector(sel);
                            if (!el) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            el.click();
                            return true;
                        }
                    }

                    for (const combo of document.querySelectorAll('.RadComboBox')) {
                        const input = combo.querySelector('input.rcbInput, input');
                        if (!input || trim(input.value) !== 'Units') continue;
                        const widget = window.$find ? window.$find(combo.id) : null;
                        if (widget?.showDropDown) {
                            widget.showDropDown();
                            return true;
                        }
                        input.click();
                        return true;
                    }

                    for (const inp of document.querySelectorAll(
                        'input.rcbInput, input[type="text"]'
                    )) {
                        if (trim(inp.value) !== 'Units') continue;
                        const r = inp.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        inp.click();
                        return true;
                    }
                    return false;
                }
                """,
                [self.customize_filter_dialog_title],
            )
        )

    def _set_customize_filter_based_on_measure(self, measure: str) -> None:
        """Set Based on Measure in Customize Filter Condition (RadCombo or select)."""
        for scope in self._filter_dialog_scopes():
            try:
                scope.get_by_text(
                    self.customize_filter_dialog_title, exact=False
                ).first.wait_for(state="visible", timeout=2_000)
            except PlaywrightTimeoutError:
                continue

            row = scope.locator("tr").filter(has_text="Based on Measure").first
            if row.count() > 0:
                select = row.locator("select").first
                if select.count() > 0:
                    select.select_option(label=measure, force=True)
                    logger.info(
                        "Set Based on Measure to %r (select)", measure
                    )
                    return

                arrow = row.locator(
                    ".rcbArrowCell a, .rcbArrowCell, "
                    + "[class*='ArrowCell'] a, img"
                ).first
                if arrow.count() > 0:
                    arrow.click(timeout=5_000)
                    scope.wait_for_timeout(400)
                    option = scope.get_by_text(measure, exact=True)
                    if option.count() > 0:
                        option.first.click(timeout=5_000)
                        logger.info(
                            "Set Based on Measure to %r (combo)", measure
                        )
                        return

            picked = scope.evaluate(
                """
                ([measureName]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const label of document.querySelectorAll('td, label, span')) {
                        if (trim(label.textContent) !== 'Based on Measure') continue;
                        const row = label.closest('tr') || label.parentElement;
                        if (!row) continue;

                        const select = row.querySelector('select');
                        if (select) {
                            for (const opt of select.options) {
                                const text = trim(opt.textContent);
                                if (text === measureName || text.includes(measureName)) {
                                    select.value = opt.value;
                                    select.dispatchEvent(
                                        new Event('change', { bubbles: true })
                                    );
                                    return true;
                                }
                            }
                        }

                        const input = row.querySelector(
                            'input.rcbInput, input[type="text"]'
                        );
                        if (input) {
                            input.click();
                            for (const item of document.querySelectorAll(
                                '.rcbItem, .rcbSlide li, .RadComboBoxDropDown li'
                            )) {
                                if (trim(item.textContent) !== measureName) continue;
                                item.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
                """,
                [measure],
            )
            if picked:
                logger.info(
                    "Set Based on Measure to %r (JS)", measure
                )
                return

        raise PlaywrightTimeoutError(
            f"Could not set Based on Measure to {measure!r} in "
            f"{self.customize_filter_dialog_title!r} dialog"
        )

    def _open_pivot_table_caption_click(self, frame) -> str | None:
        """Click the PivotTable1 caption label (opens Analyze menu per UI)."""
        opened = frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                for (const el of document.querySelectorAll(
                    'span, td, div, nobr, a'
                )) {
                    const t = trim(el.textContent);
                    if (t !== 'PivotTable1' && !/^PivotTable\\d*$/.test(t)) continue;
                    if (!isVis(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.top > 120) continue;
                    el.click();
                    return 'caption-label';
                }
                return null;
            }
            """
        )
        if opened:
            logger.info("Opened PivotTable menu (%s)", opened)
            frame.wait_for_timeout(700)
        return opened

    def _open_pivot_table_dropdown(self, frame) -> None:
        # The PivotTable caption exposes a (normally visibility:hidden) down
        # arrow whose onclick="_alz_onMainContextClick()" opens the Analyze
        # context menu (Create Chart … Display Totals …). Click that image
        # directly, falling back to invoking the analyzer handler.
        opened = frame.evaluate(
            """
            () => {
                const imgs = document.querySelectorAll(
                    'img[onclick*="_alz_onMainContextClick"]'
                );
                for (const img of imgs) {
                    img.style.visibility = 'visible';
                    try { img.scrollIntoView({ block: 'center' }); } catch (e) {}
                    img.click();
                    return 'main-context-img';
                }
                if (typeof _alz_onMainContextClick === 'function') {
                    _alz_onMainContextClick();
                    return 'main-context-fn';
                }
                return null;
            }
            """
        )
        if not opened:
            # Last-ditch: click the caption label itself.
            try:
                frame.locator("text=/PivotTable1|PivotTable/").first.click(
                    timeout=5_000
                )
                opened = "label"
            except PlaywrightTimeoutError:
                opened = None
        logger.info("Opened PivotTable dropdown (%s)", opened)
        frame.wait_for_timeout(700)

    def _hover_pivot_menu_tab_by_index(self, frame, tab_index: int) -> None:
        """PivotTable popup tabs switch on hover — no click needed."""
        tab_box = frame.evaluate(
            """
            ([tabIndex]) => {
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };

                const popupRoots = Array.from(
                    document.querySelectorAll('div, table')
                ).filter((el) => {
                    if (!isVisible(el)) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 120 || r.height < 120) return false;
                    const tabs = Array.from(
                        el.querySelectorAll('img, span, td, div, a')
                    ).filter((node) => {
                        if (!isVisible(node)) return false;
                        const box = node.getBoundingClientRect();
                        return box.width > 0 && box.height > 0 && box.height < 40;
                    });
                    return tabs.length >= 3;
                });

                for (const root of popupRoots) {
                    const tabs = Array.from(
                        root.querySelectorAll('img, span, td, div, a')
                    ).filter((el) => {
                        if (!isVisible(el)) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && r.height < 40;
                    });
                    if (tabs.length > tabIndex) {
                        const r = tabs[tabIndex].getBoundingClientRect();
                        return {
                            x: r.x + r.width / 2,
                            y: r.y + r.height / 2,
                        };
                    }
                }
                return null;
            }
            """,
            [tab_index],
        )
        if not tab_box:
            raise PlaywrightTimeoutError(
                f"Could not find PivotTable menu tab index {tab_index} to hover"
            )
        frame.page.mouse.move(tab_box["x"], tab_box["y"])
        logger.info("Hovered PivotTable menu tab index %s", tab_index)
        frame.wait_for_timeout(500)

    def _dump_pivot_caption_area(self, frame) -> None:
        """Log the DOM around the 'PivotTable1' caption to locate the menu trigger."""
        try:
            info = frame.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const isVis = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    let cap = null;
                    for (const el of document.querySelectorAll(
                        'span, td, div, a, label'
                    )) {
                        const t = trim(el.textContent);
                        if (t !== 'PivotTable1' && !t.startsWith('PivotTable')) continue;
                        if (!isVis(el)) continue;
                        if (el.querySelector('*')
                            && trim(el.textContent).length > 20) continue;
                        cap = el;
                        break;
                    }
                    if (!cap) return { found: false };
                    const cr = cap.getBoundingClientRect();
                    const row = cap.closest('tr') || cap.parentElement;
                    const clickable = [];
                    const scan = row ? row.querySelectorAll('*')
                        : document.querySelectorAll('*');
                    for (const el of scan) {
                        if (!isVis(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (Math.abs(r.top - cr.top) > 40) continue;
                        if (r.left < cr.left - 30 || r.left > cr.right + 160) continue;
                        const onclick = el.getAttribute('onclick') || '';
                        const cls = el.className || '';
                        const tag = el.tagName.toLowerCase();
                        const src = el.getAttribute('src') || '';
                        if (!onclick && tag !== 'img' && tag !== 'a'
                            && !/arrow|menu|drop|down|btn/i.test(cls)) continue;
                        clickable.push({
                            tag, cls: (cls || '').slice(0, 30),
                            onclick: onclick.slice(0, 60),
                            src: src.slice(0, 50),
                            x: Math.round(r.left), y: Math.round(r.top),
                            w: Math.round(r.width), h: Math.round(r.height),
                        });
                    }
                    return {
                        found: true,
                        capTag: cap.tagName.toLowerCase(),
                        capCls: (cap.className || '').slice(0, 40),
                        capOnclick: (cap.getAttribute('onclick') || '').slice(0, 60),
                        cap: { x: Math.round(cr.left), y: Math.round(cr.top),
                               w: Math.round(cr.width), h: Math.round(cr.height) },
                        rowHtml: (row ? row.outerHTML : '').slice(0, 700),
                        clickable,
                    };
                }
                """
            )
        except Exception as exc:
            logger.info("PIVOT CAPTION dump failed: %s", exc)
            return
        if not info or not info.get("found"):
            logger.info("PIVOT CAPTION 'PivotTable1' not found")
            return
        logger.info(
            "PIVOT CAPTION %s cls=%r onclick=%r @(%d,%d %dx%d)",
            info.get("capTag"),
            info.get("capCls"),
            info.get("capOnclick"),
            info["cap"]["x"], info["cap"]["y"],
            info["cap"]["w"], info["cap"]["h"],
        )
        for c in info.get("clickable", []):
            logger.info(
                "  CLICKABLE %s cls=%r onclick=%r src=%r @(%d,%d %dx%d)",
                c["tag"], c["cls"], c["onclick"], c["src"],
                c["x"], c["y"], c["w"], c["h"],
            )
        logger.info("PIVOT CAPTION rowHtml: %s", info.get("rowHtml"))

    def _dump_analyzer_menu_tabs(self, frame) -> None:
        """Log the small tab icons at the top of the open analyzer context menu."""
        try:
            tabs = frame.evaluate(
                """
                () => {
                    const isVis = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const out = [];
                    for (const img of document.querySelectorAll(
                        'img, a, span, td, div'
                    )) {
                        if (!isVis(img)) continue;
                        const r = img.getBoundingClientRect();
                        if (r.top > 130 || r.left > 220) continue;
                        if (r.height > 28 || r.width > 60) continue;
                        const onclick = img.getAttribute
                            ? (img.getAttribute('onclick') || '') : '';
                        const src = img.getAttribute
                            ? (img.getAttribute('src') || '') : '';
                        const title = img.getAttribute
                            ? (img.getAttribute('title') || '') : '';
                        if (!onclick && !src && !title) continue;
                        out.push({
                            tag: img.tagName.toLowerCase(),
                            onclick: onclick.slice(0, 55),
                            src: src.slice(-28),
                            title: title.slice(0, 24),
                            x: Math.round(r.left + r.width / 2),
                            y: Math.round(r.top + r.height / 2),
                            w: Math.round(r.width),
                            h: Math.round(r.height),
                        });
                    }
                    return out;
                }
                """
            )
        except Exception:
            tabs = None
        if not tabs:
            logger.info("ANALYZER TABS: none captured")
            return
        logger.info("ANALYZER TABS (%d):", len(tabs))
        for t in tabs:
            logger.info(
                "  TAB %s src=%r title=%r onclick=%r @(%d,%d %dx%d)",
                t["tag"], t["src"], t["title"], t["onclick"],
                t["x"], t["y"], t["w"], t["h"],
            )

    def _analyzer_popup_has_analyze_items(self, frame) -> bool:
        """True when the open PivotTable context menu shows Analyze actions."""
        try:
            return bool(
                frame.evaluate(
                    """
                    () => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const isVis = (el) => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        };
                        for (const el of document.querySelectorAll(
                            'td, div, span, a, nobr'
                        )) {
                            if (!isVis(el)) continue;
                            const t = trim(el.textContent);
                            if (!t || t.length > 60) continue;
                            if (
                                t.includes('Display Totals')
                                || t.startsWith('Create Chart')
                                || t.startsWith('Detail Settings')
                                || t.startsWith('Move or Copy')
                            ) {
                                return true;
                            }
                        }
                        return false;
                    }
                    """
                )
            )
        except Exception:
            return False

    def _find_analyzer_popup_tabs(self, frame) -> list[dict]:
        """Page-coords of the 3 tab icons on the open analyzer popup."""
        raw = frame.evaluate(
            """
            () => {
                const isVis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                let popup = null;
                let bestArea = Infinity;
                for (const el of document.querySelectorAll('div, table')) {
                    if (!isVis(el)) continue;
                    const t = el.textContent || '';
                    if (!/Field Drop Zone|Back to Top Level|Export\\.\\.\\.|Rows Per Page|Design|Create Chart|Display Totals/.test(t)) {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width < 80 || r.width > 450) continue;
                    const area = r.width * r.height;
                    if (area < bestArea) { bestArea = area; popup = el; }
                }
                if (!popup) return [];
                const pr = popup.getBoundingClientRect();
                const tabs = [];
                for (const el of popup.querySelectorAll('img, td, div')) {
                    if (!isVis(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.top > pr.top + 32) continue;
                    if (r.left > pr.left + 130) continue;
                    if (r.width < 10 || r.width > 44) continue;
                    if (r.height < 10 || r.height > 44) continue;
                    tabs.push({
                        left: r.left,
                        cx: r.left + r.width / 2,
                        cy: r.top + r.height / 2,
                    });
                }
                tabs.sort((a, b) => a.left - b.left);
                return tabs.slice(0, 3);
            }
            """
        )
        if not raw:
            return []
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return []
        return [
            {
                "page_x": fbox["x"] + t["cx"],
                "page_y": fbox["y"] + t["cy"],
                "index": i,
            }
            for i, t in enumerate(raw)
        ]

    def _hover_analyzer_menu_tab(self, frame, tab_index: int = 1) -> bool:
        """Hover the analyzer popup tab — tabs switch on hover, no click needed.

        Index 1 is the bar-chart icon (= Analyze: Display Totals, Create Chart…).
        Only Display Totals etc. need a click after the menu appears.
        """
        if self._analyzer_popup_has_analyze_items(frame):
            logger.info("Analyze menu already visible")
            return True

        tabs = self._find_analyzer_popup_tabs(frame)
        if not tabs:
            # Fallback: legacy hover-by-popup-root finder.
            try:
                self._hover_pivot_menu_tab_by_index(frame, tab_index)
                logger.info("Hovered analyzer tab index %d (legacy finder)", tab_index)
            except PlaywrightTimeoutError:
                logger.info("Could not hover Analyze tab: no-tabs")
                return False
        else:
            # Tab 2 (bar-chart) is most reliable in headed runs.
            order = [2, 1, 0]
            page = frame.page
            hovered = False
            for idx in order:
                match = next((t for t in tabs if t["index"] == idx), None)
                if not match:
                    continue
                page.mouse.move(match["page_x"], match["page_y"])
                logger.info(
                    "Hovering analyzer tab index %d @(%.0f, %.0f)…",
                    idx,
                    match["page_x"],
                    match["page_y"],
                )
                hovered = True
                if self._poll_until(
                    frame,
                    lambda: self._analyzer_popup_has_analyze_items(frame),
                    timeout_ms=5_000,
                ):
                    logger.info("Analyze menu visible after hovering tab %d", idx)
                    return True
                page.mouse.click(match["page_x"], match["page_y"])
                frame.wait_for_timeout(250)
                if self._poll_until(
                    frame,
                    lambda: self._analyzer_popup_has_analyze_items(frame),
                    timeout_ms=5_000,
                ):
                    logger.info("Analyze menu visible after clicking tab %d", idx)
                    return True
                frame.wait_for_timeout(300)
            if not hovered:
                logger.info("Could not hover Analyze tab: no-tab-coords")
                return False

        if self._poll_until(
            frame,
            lambda: self._analyzer_popup_has_analyze_items(frame),
            timeout_ms=2_500,
        ):
            logger.info("Analyze menu visible after hover")
            return True
        logger.info("Analyze menu did not appear after hovering tab(s)")
        return False

    def _switch_analyzer_menu_to_analyze(self, frame) -> bool:
        """Hover the Analyze (bar-chart) tab — do not click it."""
        return self._hover_analyzer_menu_tab(frame, tab_index=1)

    def _open_pivot_analyze_menu(self, frame) -> None:
        analyze_ready = lambda: self._analyzer_popup_has_analyze_items(frame)

        for outer in range(1, 6):
            # Open the PivotTable popup (down-arrow is most reliable).
            self._open_pivot_table_dropdown(frame)
            frame.wait_for_timeout(400)
            if self._poll_until(frame, analyze_ready, timeout_ms=1_000):
                logger.info("Pivot Analyze menu open (default tab)")
                return
            # Hover the Analyze tab — cursor only, no click.
            if self._hover_analyzer_menu_tab(frame, tab_index=1):
                if self._poll_until(frame, analyze_ready, timeout_ms=2_000):
                    logger.info("Pivot Analyze menu open (after hover)")
                    return
            if outer == 1:
                self._dump_open_menu_state(frame, "pivot_analyze_menu")
            self._clear_open_popups(frame)
            frame.wait_for_timeout(400)
        raise PlaywrightTimeoutError(
            f"Could not open {self.analyze_menu_text!r} menu on PivotTable"
        )

    def _click_dialog_tab(self, dialog_title: str, tab_text: str) -> None:
        for scope in self._filter_dialog_scopes():
            tab = scope.get_by_text(tab_text, exact=True)
            if tab.count() == 0:
                continue
            try:
                if tab.first.is_visible():
                    tab.first.click(timeout=5_000)
                    logger.info("Opened %r tab on %r dialog", tab_text, dialog_title)
                    return
            except PlaywrightTimeoutError:
                continue
        raise PlaywrightTimeoutError(
            f"Could not open {tab_text!r} tab on {dialog_title!r} dialog"
        )

    def _select_detail_settings_measure(self, measure: str) -> None:
        for scope in self._filter_dialog_scopes():
            row = scope.locator("tr").filter(has_text="Measure").first
            if row.count() > 0:
                select = row.locator("select").first
                if select.count() > 0:
                    select.select_option(label=measure, force=True)
                    logger.info("Selected measure %r in Detail Settings", measure)
                    return

            updated = scope.evaluate(
                """
                ([measureName]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const label of document.querySelectorAll('td, label, span')) {
                        if (trim(label.textContent) !== 'Measure') continue;
                        const row = label.closest('tr') || label.parentElement;
                        const select = row?.querySelector('select');
                        if (!select) continue;
                        for (const opt of select.options) {
                            const text = trim(opt.textContent);
                            if (text === measureName || text.includes(measureName)) {
                                select.value = opt.value;
                                select.dispatchEvent(
                                    new Event('change', { bubbles: true })
                                );
                                return true;
                            }
                        }
                    }
                    return false;
                }
                """,
                [measure],
            )
            if updated:
                logger.info(
                    "Selected measure %r in Detail Settings (JS)", measure
                )
                return

        raise PlaywrightTimeoutError(
            f"Could not select measure {measure!r} in Detail Settings dialog"
        )

    def _set_detail_settings_text_field(self, field_label: str, value: str) -> None:
        for scope in self._filter_dialog_scopes():
            row = scope.locator("tr").filter(has_text=field_label).first
            if row.count() > 0:
                text_input = row.locator(
                    "input[type='text'], input:not([type='hidden'])"
                ).first
                if text_input.count() > 0:
                    text_input.fill(value)
                    logger.info("Set %r to %r in Detail Settings", field_label, value)
                    return

            updated = scope.evaluate(
                """
                ([fieldLabel, fieldValue]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const label of document.querySelectorAll('td, label, span')) {
                        if (trim(label.textContent) !== fieldLabel) continue;
                        const row = label.closest('tr') || label.parentElement;
                        const input = row?.querySelector(
                            'input[type="text"], input:not([type="hidden"])'
                        );
                        if (!input) continue;
                        input.focus();
                        input.value = fieldValue;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    return false;
                }
                """,
                [field_label, value],
            )
            if updated:
                logger.info(
                    "Set %r to %r in Detail Settings (JS)", field_label, value
                )
                return

        raise PlaywrightTimeoutError(
            f"Could not set {field_label!r} to {value!r} in Detail Settings dialog"
        )

    def _uncheck_all_display_totals_options(self) -> None:
        self._wait_for_dialog(self.display_totals_dialog_title)
        unchecked = 0
        result = None

        js = """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                let dialogRoot = null;
                let bestArea = Infinity;

                for (const el of document.querySelectorAll(
                    'div, table, form, fieldset'
                )) {
                    if (!isVis(el)) continue;
                    const t = el.textContent || '';
                    if (!t.includes('Display Totals')) continue;
                    if (!t.includes('Grand Totals') && !t.includes('Subtotals')) {
                        continue;
                    }
                    const boxes = el.querySelectorAll('input[type="checkbox"]');
                    if (boxes.length < 2) continue;
                    const r = el.getBoundingClientRect();
                    const area = r.width * r.height;
                    if (area < bestArea) {
                        bestArea = area;
                        dialogRoot = el;
                    }
                }

                if (!dialogRoot) {
                    for (const cb of document.querySelectorAll(
                        'input[type="checkbox"]'
                    )) {
                        if (!isVis(cb)) continue;
                        let node = cb.parentElement;
                        for (let d = 0; d < 12 && node; d++) {
                            const t = node.textContent || '';
                            if (
                                t.includes('Grand Totals')
                                || t.includes('Subtotals')
                            ) {
                                dialogRoot = node;
                                break;
                            }
                            node = node.parentElement;
                        }
                        if (dialogRoot) break;
                    }
                }

                if (!dialogRoot) return { ok: false, count: 0 };

                let count = 0;
                for (const cb of dialogRoot.querySelectorAll(
                    'input[type="checkbox"]'
                )) {
                    if (!cb.checked) continue;
                    cb.click();
                    count += 1;
                }
                return { ok: true, count };
            }
        """

        for scope in self._all_dialog_scopes():
            try:
                result = scope.evaluate(js)
            except Exception:
                result = None
            if result and result.get("ok"):
                unchecked += result.get("count", 0)
                break

        # Second pass — some IQVIA skins need multiple clicks to clear every box.
        for _ in range(4):
            remaining = 0
            for scope in self._all_dialog_scopes():
                try:
                    remaining = scope.evaluate(
                        """
                        () => {
                            const isVis = (el) => {
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            };
                            let dialogRoot = null;
                            let bestArea = Infinity;
                            for (const el of document.querySelectorAll(
                                'div, table, form, fieldset'
                            )) {
                                if (!isVis(el)) continue;
                                const t = el.textContent || '';
                                if (!t.includes('Display Totals')) continue;
                                if (
                                    !t.includes('Grand Totals')
                                    && !t.includes('Subtotals')
                                ) {
                                    continue;
                                }
                                const boxes = el.querySelectorAll(
                                    'input[type="checkbox"]'
                                );
                                if (boxes.length < 1) continue;
                                const r = el.getBoundingClientRect();
                                const area = r.width * r.height;
                                if (area < bestArea) {
                                    bestArea = area;
                                    dialogRoot = el;
                                }
                            }
                            if (!dialogRoot) return 0;
                            let count = 0;
                            for (const cb of dialogRoot.querySelectorAll(
                                'input[type="checkbox"]'
                            )) {
                                if (!cb.checked) continue;
                                cb.click();
                                count += 1;
                            }
                            return count;
                        }
                        """
                    )
                except Exception:
                    remaining = 0
                if remaining:
                    unchecked += remaining
                    break
            if not remaining:
                break

        if not result or not result.get("ok"):
            raise PlaywrightTimeoutError(
                f"Could not find checkboxes in {self.display_totals_dialog_title!r} dialog"
            )

        logger.info(
            "Unchecked %d option(s) in %r dialog",
            unchecked,
            self.display_totals_dialog_title,
        )

    def _display_totals_dialog_visible(self) -> bool:
        return (
            self._scope_for_open_dialog(self.display_totals_dialog_title)
            is not None
        )

    def _click_display_totals_ok(self) -> bool:
        """Click Ok on the Display Totals dialog (fast — no long retry loop)."""
        title = self.display_totals_dialog_title
        for scope in self._all_dialog_scopes():
            try:
                if self._click_dialog_ok_js(scope, title):
                    logger.info("Clicked Ok on %r dialog", title)
                    return True
            except Exception:
                pass
        if self._click_ok_in_any_frame(title):
            return True
        return False

    def _click_display_totals_hide_totals(self) -> bool:
        """Click the 'Hide Totals' button inside the Display Totals dialog."""
        label = self.display_totals_hide_button_text
        for scope in self._all_dialog_scopes():
            for pattern in (label, "Hide totals", "Hide Total"):
                try:
                    btn = scope.get_by_role("button", name=pattern)
                    if btn.count() > 0 and btn.first.is_visible(timeout=300):
                        btn.first.click(timeout=5_000)
                        logger.info("Clicked %r in Display Totals dialog", label)
                        return True
                except PlaywrightTimeoutError:
                    pass
                try:
                    inp = scope.locator(
                        f'input[type="button"][value="{pattern}"], '
                        f'input[type="submit"][value="{pattern}"]'
                    )
                    if inp.count() > 0 and inp.first.is_visible(timeout=300):
                        inp.first.click(timeout=5_000)
                        logger.info("Clicked %r in Display Totals dialog", label)
                        return True
                except PlaywrightTimeoutError:
                    pass

            try:
                clicked = bool(
                    scope.evaluate(
                        """
                        ([label]) => {
                            const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                            const needle = label.toLowerCase();
                            for (const el of document.querySelectorAll(
                                'input[type="button"], input[type="submit"], '
                                + 'button, a'
                            )) {
                                const text = trim(el.value || el.textContent);
                                if (text.toLowerCase() !== needle) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width <= 0 || r.height <= 0) continue;
                                el.click();
                                return true;
                            }
                            return false;
                        }
                        """,
                        [label],
                    )
                )
            except Exception:
                clicked = False
            if clicked:
                logger.info("Clicked %r in Display Totals dialog (JS)", label)
                return True
        return False

    def _click_analyzer_menu_item(self, frame, label: str) -> bool:
        """Click a row inside the open PivotTable analyzer context menu."""
        try:
            clicked = frame.evaluate(
                """
                ([label]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const needle = label.replace(/\\.\\.\\.$/, '').toLowerCase();
                    const isVis = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    for (const el of document.querySelectorAll(
                        'td, div, span, a, nobr'
                    )) {
                        if (!isVis(el)) continue;
                        const t = trim(el.textContent);
                        if (!t || t.length > 60) continue;
                        if (!t.toLowerCase().includes(needle)) continue;
                        el.click();
                        return t;
                    }
                    return null;
                }
                """,
                [label],
            )
        except Exception:
            clicked = None
        if clicked:
            logger.info("Clicked analyzer menu item %r (%r)", label, clicked)
            return True
        return False

    def _dismiss_stale_filter_ui(self, frame) -> None:
        """Close leftover filter popups before pivot Analyze menu."""
        for _ in range(5):
            if not self._filter_condition_dialog_open() and not self._market_filter_ui_open(
                frame
            ):
                return
            self._clear_open_popups(frame)
            frame.page.keyboard.press("Escape")
            frame.wait_for_timeout(350)
        self._wait_for_query_idle(frame)

    def configure_display_totals_off(self) -> None:
        """PivotTable menu → Analyze → Display Totals → Hide Totals."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self._dismiss_stale_filter_ui(frame)
        logger.info("Opening PivotTable menu to configure Display Totals…")

        self._open_pivot_analyze_menu(frame)
        if not self._click_analyzer_menu_item(frame, self.display_totals_menu_text):
            self._click_menu_item(self.display_totals_menu_text)
        frame.wait_for_timeout(DIALOG_SETTLE_MS)

        self._wait_for_dialog(self.display_totals_dialog_title)
        if not self._click_display_totals_hide_totals():
            raise PlaywrightTimeoutError(
                f"Could not click {self.display_totals_hide_button_text!r} on "
                f"{self.display_totals_dialog_title!r} dialog"
            )
        frame.wait_for_timeout(DIALOG_SETTLE_MS)
        if self._display_totals_dialog_visible():
            if not self._click_display_totals_ok():
                logger.info(
                    "%r still open after Hide Totals — continuing",
                    self.display_totals_dialog_title,
                )
        frame.wait_for_timeout(DIALOG_SETTLE_MS)
        self._clear_open_popups(frame)
        self._wait_for_query_idle(frame)
        logger.info("Display Totals hidden — proceeding to sheet rename/copy")

    def configure_detail_settings_measure_format(self) -> None:
        """PivotTable → Analyze → Detail Settings → Measure Format → null/inf = 0."""
        frame = self._designer_frame()
        logger.info("Opening Detail Settings → Measure Format…")

        self._open_pivot_analyze_menu(frame)
        self._click_menu_item(self.detail_settings_menu_text)
        frame.wait_for_timeout(1_000)
        self._wait_for_dialog(self.detail_settings_dialog_title)
        self._click_dialog_tab(
            self.detail_settings_dialog_title,
            self.measure_format_tab_text,
        )
        frame.wait_for_timeout(500)

        for measure in (self.units_item, self.values_item):
            self._select_detail_settings_measure(measure)
            self._set_detail_settings_text_field(
                self.display_of_null_label,
                self.null_infinity_display_value,
            )
            self._set_detail_settings_text_field(
                self.display_of_infinity_label,
                self.null_infinity_display_value,
            )

        self._click_dialog_ok(self.detail_settings_dialog_title)
        frame.wait_for_timeout(1_500)
        logger.info(
            "Detail Settings updated — Display of Null/Infinity set to %r for "
            "Units and Values",
            self.null_infinity_display_value,
        )

    def _set_move_or_copy_sheet_name(self, sheet_name: str) -> None:
        field_labels = ("Name", "Sheet name", "New name", "Report name")
        for scope in self._filter_dialog_scopes():
            for field_label in field_labels:
                row = scope.locator("tr").filter(has_text=field_label).first
                if row.count() == 0:
                    continue
                text_input = row.locator(
                    "input[type='text'], input:not([type='hidden'])"
                ).first
                if text_input.count() == 0:
                    continue
                text_input.fill(sheet_name)
                logger.info(
                    "Set %r to %r in Move or Copy dialog",
                    field_label,
                    sheet_name,
                )
                return

            updated = scope.evaluate(
                """
                ([sheetName]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };

                    const dialogRoots = [];
                    for (const el of document.querySelectorAll('div, table, form')) {
                        const text = trim(el.textContent);
                        if (!text.includes('Move or Copy')) continue;
                        if (!isVisible(el)) continue;
                        dialogRoots.push(el);
                    }

                    for (const root of dialogRoots) {
                        const inputs = Array.from(
                            root.querySelectorAll(
                                'input[type="text"], input:not([type="hidden"])'
                            )
                        ).filter(isVisible);
                        for (const input of inputs) {
                            input.focus();
                            input.value = sheetName;
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                    }
                    return false;
                }
                """,
                [sheet_name],
            )
            if updated:
                logger.info(
                    "Set sheet name to %r in Move or Copy dialog (JS)",
                    sheet_name,
                )
                return

        raise PlaywrightTimeoutError(
            f"Could not set sheet name {sheet_name!r} in Move or Copy dialog"
        )

    def rename_pivot_sheet(self, sheet_name: str | None = None) -> None:
        """PivotTable menu → Analyze → Move or Copy → rename sheet (default ``C``)."""
        target_name = (sheet_name or self.export_sheet_name).strip()
        if not target_name:
            raise ValueError("Sheet name must not be empty")

        frame = self._designer_frame()
        logger.info("Renaming pivot sheet to %r…", target_name)
        self._open_pivot_analyze_menu(frame)
        self._click_menu_item(self.move_or_copy_menu_text)
        frame.wait_for_timeout(1_000)
        self._wait_for_dialog(self.move_or_copy_dialog_title)
        self._set_move_or_copy_sheet_name(target_name)
        self._click_dialog_ok(self.move_or_copy_dialog_title)
        frame.wait_for_timeout(1_500)
        logger.info("Pivot sheet renamed to %r", target_name)

    def apply_market_filter(self, market: str) -> None:
        """Step 9 — click Market on pivot → Filter input → pick MARKET from TSV."""
        market = market.strip()
        if not market:
            logger.warning(
                "MARKET value is empty in report_sources.tsv — selecting 'All'"
            )
        logger.info("Step 9 — filter Market to %r…", market or "All")
        self.set_market_filter(market)
        logger.info("Step 9 complete — Market filter set to %r", market or "All")

    def _safe_scope_evaluate(self, scope, script: str, arg=None):
        """Run evaluate on a scope; skip detached or stale frames."""
        try:
            if arg is None:
                return scope.evaluate(script)
            return scope.evaluate(script, arg)
        except Exception as exc:
            if "detached" in str(exc).lower():
                return None
            raise

    def _filter_condition_dialog_open(self) -> bool:
        for scope in self._filter_dialog_scopes():
            try:
                scope.get_by_text(
                    self.filter_condition_dialog_title, exact=False
                ).first.wait_for(state="visible", timeout=300)
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    def _market_two_word_prefix(self, market: str) -> str | None:
        words = market.split()
        if len(words) >= 2:
            return " ".join(words[:2])
        return None

    def _market_option_visible(self, market: str, *, search: str | None = None) -> bool:
        market = market.strip()
        needle = (search or market).strip()
        market_u = market.upper()
        needle_u = needle.upper()
        for scope in self._filter_dialog_scopes():
            try:
                scope.get_by_text(market, exact=True).first.wait_for(
                    state="visible", timeout=300
                )
                return True
            except PlaywrightTimeoutError:
                pass
            found = scope.evaluate(
                """
                ([marketU, needleU]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const el of document.querySelectorAll(
                        'span, td, div, li, a, label, nobr'
                    )) {
                        const text = trim(el.textContent);
                        if (!text || text.length > 80 || el.children.length > 8) {
                            continue;
                        }
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const tu = text.toUpperCase();
                        if (tu === marketU) return true;
                        if (tu.includes(needleU) || needleU.includes(tu)) {
                            return true;
                        }
                    }
                    return false;
                }
                """,
                [market_u, needle_u],
            )
            if found:
                return True
        return False

    def _market_filter_ui_open(self, frame, market: str | None = None) -> bool:
        if self._set_filter_menu_open(frame):
            return True
        if self._filter_condition_dialog_open():
            return True
        if market and self._market_option_visible(market):
            return True
        if frame.evaluate(
            """
            () => {
                for (const el of document.body.querySelectorAll(
                    'div, table, ul, span, a, li, td'
                )) {
                    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!text) continue;
                    if (text !== 'Set Filter' && !text.startsWith('Filter Condition')) {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
                for (const el of document.body.querySelectorAll(
                    '[class*="Menu"], [class*="menu"], [class*="Slide"], [class*="Popup"]'
                )) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 60 && r.height > 20) return true;
                }
                return false;
            }
            """
        ):
            return True
        return False

    def _set_filter_menu_open(self, frame) -> bool:
        for scope in (self.page, frame):
            for pattern in (
                "Set Filter...",
                "Set Filter",
                "Set filter...",
                "Set filter",
            ):
                try:
                    scope.get_by_text(pattern, exact=True).first.wait_for(
                        state="visible", timeout=300
                    )
                    return True
                except PlaywrightTimeoutError:
                    continue
            try:
                scope.locator("text=/Set\\s+Filter/i").first.wait_for(
                    state="visible", timeout=300
                )
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    def _click_market_filter_trigger(self, frame) -> str:
        """Click the Market filter IMG arrow / NOBR row under PivotTable1."""
        opened = self._click_market_filter_trigger_js(frame)
        if opened:
            self._settle(frame)
            return opened

        row = frame.locator("nobr").filter(has_text="Market (None)").first
        try:
            if row.is_visible(timeout=500):
                arrow = row.locator("xpath=preceding-sibling::img[1]")
                if arrow.count() > 0 and arrow.is_visible(timeout=300):
                    arrow.click(timeout=5_000)
                    self._settle(frame)
                    return "locator-img"
                row.click(timeout=5_000)
                self._settle(frame)
                return "locator-nobr"
        except PlaywrightTimeoutError:
            pass

        coords = self._find_market_filter_field_coords(frame)
        if coords is None:
            coords = self._wait_for_market_filter_field(frame)

        fbox = frame.frame_element().bounding_box()
        if not fbox:
            raise PlaywrightTimeoutError("Could not read designer frame bounds")
        page = frame.page

        for name, fx, fy in (
            ("arrow-img", coords["arrowX"], coords["arrowY"]),
            ("market-nobr", coords["noneX"], coords["noneY"]),
        ):
            page.mouse.click(fbox["x"] + fx, fbox["y"] + fy)
            self._settle(frame)
            return name

        raise PlaywrightTimeoutError(
            f"Could not click {self.pivot_market_field!r} filter trigger"
        )

    def _click_market_filter_trigger_js(self, frame) -> str | None:
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));

                let row = null;
                for (const el of document.body.querySelectorAll('nobr, span, td, div')) {
                    const text = trim(el.textContent);
                    if (text !== 'Market (None)' && !text.startsWith('Market (None)')) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 400) continue;
                    row = el.closest('tr, td, div, li') || el.parentElement;
                    break;
                }
                if (!row) return null;

                for (const img of row.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    img.click();
                    return 'js-img';
                }
                for (const el of row.querySelectorAll('nobr, a')) {
                    const text = trim(el.textContent);
                    if (!text.includes('Market')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    el.click();
                    return 'js-nobr';
                }
                return null;
            }
            """
        )

    def _ensure_market_filter_menu_open(
        self, frame, market: str | None = None, *, attempts: int = 3
    ) -> bool:
        if self._market_filter_ui_open(frame, market):
            return True
        for attempt in range(1, attempts + 1):
            method = self._click_market_filter_trigger(frame)
            logger.info(
                "Clicked Market filter trigger (%s, attempt %d)",
                method,
                attempt,
            )
            if self._poll_until(
                frame,
                lambda: self._market_filter_ui_open(frame, market),
                timeout_ms=10_000,
            ):
                return True
        return self._market_filter_ui_open(frame, market)

    def _click_set_filter_menu(self) -> None:
        frame = self._designer_frame()
        labels = (
            "Set Filter...",
            "Set Filter",
            "Set filter...",
            "Set filter",
        )
        for scope in (frame, self.page):
            for label in labels:
                item = scope.get_by_text(label, exact=True)
                if item.count() == 0:
                    continue
                try:
                    item.first.click(timeout=5_000)
                    logger.info("Clicked menu item %r", label)
                    return
                except PlaywrightTimeoutError:
                    continue

        clicked = frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                for (const el of document.querySelectorAll(
                    'span, a, td, div, li, button'
                )) {
                    const text = trim(el.textContent);
                    if (!text.toLowerCase().startsWith('set filter')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    el.click();
                    return text;
                }
                return null;
            }
            """
        )
        if clicked:
            logger.info("Clicked menu item %r (JS)", clicked)
            return
        raise PlaywrightTimeoutError(
            f"Menu item {self.set_filter_menu_text!r} not found"
        )

    def _try_select_market_from_popup(self, market: str) -> bool:
        """Select market directly if the dropdown list opens (no Set Filter dialog)."""
        market = market.strip()
        for scope in self._filter_dialog_scopes():
            labels = scope.get_by_text(market, exact=True)
            for index in range(labels.count()):
                label = labels.nth(index)
                try:
                    if not label.is_visible(timeout=500):
                        continue
                except PlaywrightTimeoutError:
                    continue
                try:
                    label.click(timeout=5_000)
                    logger.info("Selected market %r from popup list", market)
                    return True
                except PlaywrightTimeoutError:
                    continue
        return False

    def _open_market_filter_dropdown(self, frame, market: str | None = None) -> None:
        """Open the Market (None) pivot filter picker."""
        if not self._ensure_market_filter_menu_open(frame, market):
            raise PlaywrightTimeoutError(
                "Could not open Market filter menu after clicking "
                f"{self.pivot_market_field!r}"
            )
        logger.info("Market filter picker is open")

    def _wait_for_filter_condition_dialog(self, timeout_ms: int = 20_000) -> None:
        frame = self._designer_frame()

        def visible() -> bool:
            for scan_frame in self._walk_page_frames():
                try:
                    scan_frame.get_by_text(
                        self.filter_condition_dialog_title, exact=False
                    ).first.wait_for(state="visible", timeout=400)
                    return True
                except PlaywrightTimeoutError:
                    continue
            try:
                self.page.get_by_text(
                    self.filter_condition_dialog_title, exact=False
                ).first.wait_for(state="visible", timeout=400)
                return True
            except PlaywrightTimeoutError:
                return False

        if not self._poll_until(frame, visible, timeout_ms=timeout_ms, poll_ms=250):
            raise PlaywrightTimeoutError(
                f"{self.filter_condition_dialog_title!r} dialog did not open"
            )
        logger.info("%r dialog is open", self.filter_condition_dialog_title)

    def _wait_for_filter_dialog_ready(self, timeout_ms: int = 20_000) -> None:
        """Wait until Filter Condition Settings tree (All row) is loaded."""

        def ready() -> bool:
            for scope in self._filter_dialog_scopes():
                loaded = self._safe_scope_evaluate(
                    scope,
                    """
                    () => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        let dialog = null;
                        let bestArea = Infinity;
                        for (const el of document.querySelectorAll('div, table, form')) {
                            const text = el.textContent || '';
                            if (!text.includes('Filter Condition Settings')) continue;
                            if (!text.includes('Filter Method')) continue;
                            if (!text.includes('OK')) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 180 || r.height < 100) continue;
                            const area = r.width * r.height;
                            if (area < bestArea) {
                                bestArea = area;
                                dialog = el;
                            }
                        }
                        if (!dialog) return false;
                        if (/Loading/i.test(dialog.textContent || '')) return false;
                        for (const row of dialog.querySelectorAll('tr, li, .rtLI')) {
                            for (const cell of row.querySelectorAll(
                                'td, span, label, nobr'
                            )) {
                                const t = trim(cell.textContent);
                                if (t === 'All' || t.startsWith('All ')) return true;
                            }
                        }
                        return true;
                    }
                    """,
                )
                if loaded:
                    return True
            return False

        frame = self._designer_frame()
        if not self._poll_until(frame, ready, timeout_ms=timeout_ms):
            logger.info("Filter dialog ready wait timed out — continuing")

    def _expand_filter_dialog_tree(self) -> None:
        """Expand collapsed nodes (especially All) in the filter dialog."""
        for scope in self._filter_dialog_scopes():
            expanded = scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let dialog = null;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 200 && r.height > 120) {
                            dialog = el;
                            break;
                        }
                    }
                    if (!dialog) return 0;
                    let count = 0;
                    const fireClick = (el) => {
                        el.scrollIntoView({ block: 'center', inline: 'nearest' });
                        el.click();
                        count += 1;
                    };
                    for (const label of dialog.querySelectorAll(
                        'span, td, div, label, li, a'
                    )) {
                        const text = trim(label.textContent);
                        if (text !== 'All' && !text.startsWith('All ')) continue;
                        const row = label.closest('tr, li, div') || label.parentElement;
                        if (!row) continue;
                        for (const img of row.querySelectorAll('img')) {
                            const r = img.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            const src = (img.src || '').toLowerCase();
                            const alt = (img.alt || '').toLowerCase();
                            if (
                                src.includes('plus')
                                || src.includes('expand')
                                || alt.includes('expand')
                                || img.width <= 16
                            ) {
                                fireClick(img);
                                break;
                            }
                        }
                    }
                    for (const img of dialog.querySelectorAll('img')) {
                        const src = (img.src || '').toLowerCase();
                        const alt = (img.alt || '').toLowerCase();
                        if (
                            !src.includes('plus')
                            && !src.includes('expand')
                            && !alt.includes('expand')
                        ) {
                            continue;
                        }
                        const r = img.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        fireClick(img);
                        if (count >= 8) break;
                    }
                    return count;
                }
                """
            )
            if expanded:
                scope.wait_for_timeout(SLOW_POLL_MS)
                logger.info("Expanded %d node(s) in filter dialog tree", expanded)
                return

    def _filter_dialog_search(self, market: str) -> None:
        """Type into the Filter Condition Settings search / filter box."""
        market = market.strip()
        for scope in self._filter_dialog_scopes():
            try:
                scope.get_by_text(
                    self.filter_condition_dialog_title, exact=False
                ).first.wait_for(state="visible", timeout=2_000)
            except PlaywrightTimeoutError:
                continue

            dialog_inputs = scope.locator(
                "xpath=//*[contains(normalize-space(.), "
                "'Filter Condition Settings')]"
                "//input[@type='text' or @type='search' "
                "or not(@type) or @type='']"
            )
            for index in range(dialog_inputs.count()):
                inp = dialog_inputs.nth(index)
                try:
                    if not inp.is_visible(timeout=300):
                        continue
                except PlaywrightTimeoutError:
                    continue
                inp.click(timeout=3_000)
                inp.fill("")
                inp.type(market, delay=30)
                inp.press("Enter")
                scope.wait_for_timeout(SLOW_POLL_MS)
                logger.info(
                    "Typed %r into filter dialog search box (locator)", market
                )
                return

            typed = scope.evaluate(
                """
                ([market]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let dialog = null;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 200 && r.height > 120) {
                            dialog = el;
                            break;
                        }
                    }
                    if (!dialog) return false;

                    const candidates = [];
                    for (const inp of dialog.querySelectorAll('input')) {
                        const type = (inp.type || 'text').toLowerCase();
                        if (type === 'hidden' || type === 'checkbox' || type === 'button') {
                            continue;
                        }
                        const r = inp.getBoundingClientRect();
                        if (r.width <= 40 || r.height <= 0) continue;
                        candidates.push({ inp, area: r.width * r.height, top: r.top });
                    }
                    candidates.sort((a, b) => a.top - b.top || b.area - a.area);
                    const pick = candidates[0]?.inp;
                    if (!pick) return false;
                    pick.focus();
                    pick.value = '';
                    pick.dispatchEvent(new Event('input', { bubbles: true }));
                    pick.value = market;
                    pick.dispatchEvent(new Event('input', { bubbles: true }));
                    pick.dispatchEvent(new Event('change', { bubbles: true }));
                    pick.dispatchEvent(
                        new KeyboardEvent('keydown', { bubbles: true, key: 'Enter' })
                    );
                    pick.dispatchEvent(
                        new KeyboardEvent('keyup', { bubbles: true, key: 'Enter' })
                    );
                    return true;
                }
                """,
                [market],
            )
            if typed:
                scope.wait_for_timeout(SLOW_POLL_MS)
                logger.info("Typed %r into filter dialog search box (JS)", market)
                return

        logger.warning(
            "Filter dialog search box not found — will expand tree and scan"
        )

    def _filter_condition_dialog_locator(self, scope):
        """Modal body only — excludes pivot row text that also contains 'All'."""
        return (
            scope.locator("div, table")
            .filter(has_text=self.filter_condition_dialog_title)
            .filter(has_text="Filter Method")
            .filter(has_text="OK")
            .last
        )

    def _toggle_filter_dialog_row(self, scope, label_text: str) -> bool:
        """Click a tree row checkbox/label inside Filter Condition Settings."""
        dialog = self._filter_condition_dialog_locator(scope)
        try:
            dialog.first.wait_for(state="visible", timeout=2_000)
        except PlaywrightTimeoutError:
            return False

        checkbox = dialog.get_by_role("checkbox", name=label_text, exact=True)
        if checkbox.count() > 0:
            target = checkbox.first
            target.scroll_into_view_if_needed()
            target.click(force=True, timeout=5_000)
            return True

        label = dialog.get_by_text(label_text, exact=True)
        for index in range(label.count()):
            item = label.nth(index)
            try:
                if not item.is_visible(timeout=300):
                    continue
            except PlaywrightTimeoutError:
                continue
            item.scroll_into_view_if_needed()
            row = item.locator(
                "xpath=ancestor::tr[1] | ancestor::li[1] | "
                "ancestor::*[contains(@class,'rtLI')][1]"
            )
            for cb_sel in (
                "input[type='checkbox']",
                "span[class*='Chk']",
                "img",
            ):
                cb = row.locator(cb_sel).first
                if cb.count() > 0:
                    try:
                        if cb.is_visible(timeout=200):
                            cb.click(force=True, timeout=5_000)
                            return True
                    except PlaywrightTimeoutError:
                        continue
            try:
                item.click(timeout=5_000)
                return True
            except PlaywrightTimeoutError:
                continue

        toggled = scope.evaluate(
            """
            ([labelText]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                let titleEl = null;
                for (const el of document.querySelectorAll(
                    'span, td, div, label, nobr, th'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Filter Condition Settings') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    titleEl = el;
                    break;
                }
                if (!titleEl) return false;
                let dialog = titleEl.closest('div, table');
                while (dialog?.parentElement) {
                    const parent = dialog.parentElement.closest('div, table');
                    if (!parent) break;
                    const text = parent.textContent || '';
                    if (!text.includes('Filter Condition Settings')) break;
                    if (!text.includes('Filter Method')) break;
                    const r = parent.getBoundingClientRect();
                    if (r.width > 800 || r.height > 600) break;
                    dialog = parent;
                }
                if (!dialog) return false;

                const clickRow = (text) => {
                    for (const el of dialog.querySelectorAll(
                        'span, td, label, nobr, a'
                    )) {
                        if (trim(el.textContent) !== text) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const row = el.closest(
                            'tr, li, .rtLI, div[class*="Row"]'
                        ) || el.parentElement;
                        const cb = row?.querySelector(
                            'input[type="checkbox"], span[class*="Chk"], img'
                        );
                        const target = cb || el;
                        target.scrollIntoView({ block: 'center', inline: 'nearest' });
                        target.click();
                        return true;
                    }
                    return false;
                };
                return clickRow(labelText);
            }
            """,
            [label_text],
        )
        return bool(toggled)

    def _uncheck_all_in_filter_dialog(self) -> None:
        """Uncheck the top-level 'All' checkbox so only one market can be picked."""
        for scope in self._filter_dialog_scopes():
            all_cb = self._filter_condition_dialog_locator(scope).get_by_role(
                "checkbox", name="All", exact=True
            )
            if all_cb.count() > 0:
                try:
                    target = all_cb.first
                    if target.is_checked(timeout=500):
                        target.uncheck(force=True)
                        scope.wait_for_timeout(POLL_MS)
                        logger.info("Unchecked 'All' in filter dialog")
                        return
                except PlaywrightTimeoutError:
                    pass

            if self._toggle_filter_dialog_row(scope, "All"):
                scope.wait_for_timeout(POLL_MS)
                logger.info("Toggled 'All' row in filter dialog")
                return

            toggled = scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let titleEl = null;
                    for (const el of document.querySelectorAll(
                        'span, td, div, label, nobr, th'
                    )) {
                        if (trim(el.textContent) !== 'Filter Condition Settings') {
                            continue;
                        }
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        titleEl = el;
                        break;
                    }
                    if (!titleEl) return false;
                    let dialog = titleEl.closest('div, table');
                    while (dialog?.parentElement) {
                        const parent = dialog.parentElement.closest('div, table');
                        if (!parent) break;
                        const text = parent.textContent || '';
                        if (!text.includes('Filter Condition Settings')) break;
                        if (!text.includes('Filter Method')) break;
                        const r = parent.getBoundingClientRect();
                        if (r.width > 800 || r.height > 600) break;
                        dialog = parent;
                    }
                    if (!dialog) return false;

                    const firstCb = dialog.querySelector('input[type="checkbox"]');
                    if (firstCb && !firstCb.checked) return false;

                    for (const el of dialog.querySelectorAll(
                        'span, td, label, nobr'
                    )) {
                        if (trim(el.textContent) !== 'All') continue;
                        const row = el.closest('tr, li, .rtLI') || el.parentElement;
                        const cb = row?.querySelector(
                            'input[type="checkbox"], span[class*="Chk"], img'
                        ) || el;
                        cb.scrollIntoView({ block: 'center', inline: 'nearest' });
                        cb.click();
                        return true;
                    }
                    return false;
                }
                """
            )
            if toggled:
                scope.wait_for_timeout(POLL_MS)
                logger.info("Unchecked 'All' in filter dialog (JS)")
                return

        logger.warning("Could not find 'All' checkbox in filter dialog")

    def _check_market_in_filter_dialog(self, market: str) -> bool:
        """Check only the target market row after 'All' is cleared."""
        market = market.strip()
        for scope in self._filter_dialog_scopes():
            market_cb = self._filter_condition_dialog_locator(scope).get_by_role(
                "checkbox", name=market, exact=True
            )
            if market_cb.count() > 0:
                target = market_cb.first
                target.scroll_into_view_if_needed()
                if not target.is_checked():
                    target.check(force=True)
                logger.info(
                    "Checked market %r in %r dialog",
                    market,
                    self.filter_condition_dialog_title,
                )
                return True

            if self._toggle_filter_dialog_row(scope, market):
                logger.info(
                    "Toggled market %r row in filter dialog",
                    market,
                )
                return True
        return False

    def _select_member_in_filter_dialog(self, member: str) -> None:
        """Uncheck All, then check only the target member in Filter Condition Settings."""
        member = member.strip()
        frame = self._designer_frame()

        def probe() -> str:
            for scope in self._filter_dialog_scopes():
                reason = scope.evaluate(
                    """
                    () => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        let dialog = null;
                        let bestArea = Infinity;
                        for (const el of document.querySelectorAll('div, table')) {
                            const text = el.textContent || '';
                            if (!text.includes('Filter Condition Settings')) continue;
                            if (!text.includes('Filter Method')) continue;
                            if (!text.includes('OK')) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            const area = r.width * r.height;
                            if (area < bestArea) {
                                bestArea = area;
                                dialog = el;
                            }
                        }
                        if (!dialog) return 'no-dialog';
                        if (/Loading/i.test(dialog.textContent || '')) return 'loading';
                        const text = dialog.textContent || '';
                        if (text.includes('Filter Method') && text.includes('OK')) {
                            return 'ready';
                        }
                        for (const row of dialog.querySelectorAll('tr, li, .rtLI')) {
                            for (const cell of row.querySelectorAll(
                                'td, span, label, nobr'
                            )) {
                                if (trim(cell.textContent) === 'All') return 'ready';
                            }
                        }
                        return 'waiting';
                    }
                    """
                )
                if reason != "no-dialog":
                    return reason
            return "no-dialog"

        if not self._poll_until(
            frame,
            lambda: probe() not in ("no-dialog", "loading", "waiting"),
            timeout_ms=15_000,
            poll_ms=100,
        ):
            raise PlaywrightTimeoutError(
                f"{self.filter_condition_dialog_title!r} dialog did not open"
            )

        for scope in self._filter_dialog_scopes():
            result = scope.evaluate(
                """
                ([member]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const norm = (s) => trim(s).toUpperCase();
                    const target = norm(member);
                    let dialog = null;
                    let bestArea = Infinity;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        if (!text.includes('Filter Method')) continue;
                        if (!text.includes('OK')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const area = r.width * r.height;
                        if (area < bestArea) {
                            bestArea = area;
                            dialog = el;
                        }
                    }
                    if (!dialog) return { ok: false, reason: 'no-dialog' };

                    const rowCells = (row) =>
                        Array.from(row.querySelectorAll('td, span, label, nobr'));
                    const rowExactLabel = (row, labelText) => {
                        for (const cell of rowCells(row)) {
                            if (trim(cell.textContent) === labelText) return cell;
                        }
                        return null;
                    };
                    const rowMatchesMember = (row) => {
                        for (const cell of rowCells(row)) {
                            const t = norm(cell.textContent);
                            if (t === target || t.startsWith(target)) return cell;
                        }
                        return null;
                    };
                    const clickRow = (row, cell) => {
                        const cb = row.querySelector('input[type="checkbox"]');
                        const img = row.querySelector('img');
                        const hit = cb || img || cell;
                        hit.scrollIntoView({ block: 'center', inline: 'nearest' });
                        hit.click();
                        return true;
                    };

                    const rows = dialog.querySelectorAll('tr, li, .rtLI');
                    for (const row of rows) {
                        if (!rowExactLabel(row, 'All')) continue;
                        const cb = row.querySelector('input[type="checkbox"]');
                        if (cb?.checked) clickRow(row, rowExactLabel(row, 'All'));
                        break;
                    }
                    for (const row of rows) {
                        const cell = rowExactLabel(row, member)
                            || rowMatchesMember(row);
                        if (!cell) continue;
                        const cb = row.querySelector('input[type="checkbox"]');
                        if (!cb || !cb.checked) clickRow(row, cell);
                        return { ok: true, label: trim(cell.textContent) };
                    }
                    return { ok: false, reason: 'no-member-row' };
                }
                """,
                [member],
            )
            if result and result.get("ok"):
                logger.info(
                    "Selected member %r in filter dialog",
                    result.get("label", member),
                )
                return

        if self._select_member_in_filter_dialog_fallback(member):
            return

        raise PlaywrightTimeoutError(
            f"Could not check member {member!r} in "
            f"{self.filter_condition_dialog_title!r} dialog"
        )

    def _select_member_in_filter_dialog_fallback(self, member: str) -> bool:
        """Playwright locators when JS tree scan misses the member row."""
        member = member.strip()
        for scope in self._filter_dialog_scopes():
            if self._toggle_filter_dialog_row(scope, "All"):
                scope.wait_for_timeout(POLL_MS)
            if self._toggle_filter_dialog_row(scope, member):
                logger.info("Selected member %r via filter dialog row toggle", member)
                return True
            partial = scope.get_by_text(member, exact=False)
            for index in range(min(partial.count(), 12)):
                item = partial.nth(index)
                try:
                    if not item.is_visible(timeout=300):
                        continue
                    label = item.inner_text(timeout=500).strip()
                except PlaywrightTimeoutError:
                    continue
                if label and self._toggle_filter_dialog_row(scope, label):
                    logger.info(
                        "Selected member %r via partial label match",
                        member,
                    )
                    return True
        return False

    def _select_market_in_filter_dialog(self, market: str) -> None:
        """Uncheck All, then check only the market from TSV."""
        self._select_member_in_filter_dialog(market)

    def _click_filter_dialog_ok(self) -> None:
        try:
            designer = self._designer_frame()
            for locator in (
                designer.get_by_role("button", name="OK"),
                designer.locator('input[type="submit"][value="OK"]'),
                designer.locator('input[type="button"][value="OK"]'),
            ):
                for index in range(locator.count()):
                    button = locator.nth(index)
                    try:
                        if button.is_visible(timeout=500):
                            button.click(timeout=5_000)
                            logger.info(
                                "Clicked OK on %r dialog (designer frame)",
                                self.filter_condition_dialog_title,
                            )
                            return
                    except PlaywrightTimeoutError:
                        continue
        except PlaywrightTimeoutError:
            pass

        dialog = self._filter_condition_dialog_page_locator()
        ok_in_dialog = dialog.locator(
            'input[type="submit"][value="OK"], '
            'input[type="button"][value="OK"], '
            'button:has-text("OK")'
        )
        for index in range(ok_in_dialog.count()):
            button = ok_in_dialog.nth(index)
            try:
                if button.is_visible(timeout=1_000):
                    button.click(timeout=5_000)
                    logger.info(
                        "Clicked OK on %r dialog",
                        self.filter_condition_dialog_title,
                    )
                    return
            except PlaywrightTimeoutError:
                continue

        for locator in (
            self.page.get_by_role("button", name="OK"),
            self.page.locator('input[type="submit"][value="OK"]'),
            self.page.locator('input[type="button"][value="OK"]'),
        ):
            for index in range(locator.count()):
                button = locator.nth(index)
                try:
                    if not button.is_visible(timeout=300):
                        continue
                    button.click(timeout=5_000)
                    logger.info(
                        "Clicked OK on %r dialog (page-wide)",
                        self.filter_condition_dialog_title,
                    )
                    return
                except PlaywrightTimeoutError:
                    continue

        for scope in self._filter_dialog_scopes():
            for locator in (
                scope.get_by_role("button", name="OK"),
                scope.locator('input[type="button"][value="OK"]'),
                scope.locator('input[type="submit"][value="OK"]'),
            ):
                for index in range(locator.count()):
                    button = locator.nth(index)
                    try:
                        if button.is_visible(timeout=500):
                            button.click(timeout=5_000)
                            logger.info(
                                "Clicked OK on %r dialog",
                                self.filter_condition_dialog_title,
                            )
                            return
                    except PlaywrightTimeoutError:
                        continue

        for scan_frame in self._walk_page_frames():
            try:
                clicked = scan_frame.evaluate(
                    """
                    () => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const hits = [];
                        for (const btn of document.querySelectorAll('input, button')) {
                            const label = trim(btn.value || btn.textContent);
                            if (label !== 'OK') continue;
                            const r = btn.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            hits.push({ btn, top: r.top, left: r.left });
                        }
                        if (!hits.length) return false;
                        hits.sort((a, b) => b.top - a.top || a.left - b.left);
                        hits[0].btn.click();
                        return true;
                    }
                    """
                )
                if clicked:
                    logger.info(
                        "Clicked OK on %r dialog (JS, frame %s)",
                        self.filter_condition_dialog_title,
                        (scan_frame.url or "")[-40:],
                    )
                    return
            except Exception:
                continue

        raise PlaywrightTimeoutError(
            f"Could not find OK button on {self.filter_condition_dialog_title!r} dialog"
        )

    def _normalize_market_key(self, name: str) -> str:
        """Loose compare key — strips DRV/MKT/MARKET noise for label comparisons."""
        key = re.sub(r"\bMKT\b", "", name, flags=re.I)
        key = re.sub(r"\bDRV\b", "", key, flags=re.I)
        key = re.sub(r"\bMARKET\b", "", key, flags=re.I)
        return re.sub(r"\s+", " ", key).strip().upper()

    def _markets_match(self, left: str, right: str) -> bool:
        left_u = left.strip().upper()
        right_u = right.strip().upper()
        if left_u == right_u or left_u in right_u or right_u in left_u:
            return True
        ln = self._normalize_market_key(left)
        rn = self._normalize_market_key(right)
        if ln == rn:
            return True
        # Handle IQVIA compound names like "OVIDREL/GONAL-F 75 IU DRV MARKET"
        # where the TSV has just "OVIDREL DRV MKT". After normalization both
        # reduce to their core product name. Check each slash-segment of the
        # compound against the other key.
        for compound, other in ((ln, rn), (rn, ln)):
            if "/" in compound:
                for part in compound.split("/"):
                    part = part.strip()
                    if part and part == other.split("/")[0].strip():
                        return True
        return False

    def _market_filter_applied(self, frame, market: str) -> bool:
        """True when pivot Market row shows the chosen market (not None)."""
        if self._resolve_pivot_field_locator(frame, self.pivot_market_field) is not None:
            return False
        current = self._current_pivot_market_value(frame)
        if current and self._markets_match(current, market):
            return True
        coords = self._find_market_filter_field_coords(frame)
        if coords:
            field_text = coords.get("text") or ""
            if "(None)" not in field_text and self._markets_match(field_text, market):
                return True
        return False

    def _current_pivot_market_value(self, frame) -> str | None:
        """Read the market name currently shown on the pivot filter row."""
        value = frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'nobr, span, td, div, a, label'
                )) {
                    const text = trim(el.textContent);
                    if (!text || text.length > 120) continue;
                    if (inTree(el)) continue;
                    if (!text.includes('Market')) continue;
                    if (text === 'Market') continue;
                    if (text.includes('(None)')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 320) continue;
                    const area = r.width * r.height;
                    if (!best || area < best.area) {
                        let name = text;
                        const prefix = text.match(/^Market\\s+(.+)$/i);
                        if (prefix) name = trim(prefix[1]);
                        best = { name: trim(name), area };
                    }
                }
                return best ? best.name : null;
            }
            """
        )
        return (value or "").strip() or None

    def _clear_pivot_market_filter(self, frame, current_market: str) -> None:
        """Remove an existing Market filter before applying a new TSV row."""
        current_market = current_market.strip()
        if not current_market:
            return
        logger.info("Clearing previous Market filter %r…", current_market)
        self._open_market_inline_filter_popup(frame)
        for scope in self._filter_dialog_scopes():
            labels = scope.get_by_text(current_market, exact=True)
            for index in range(labels.count()):
                label = labels.nth(index)
                try:
                    if not label.is_visible(timeout=400):
                        continue
                except PlaywrightTimeoutError:
                    continue
                try:
                    label.click(timeout=3_000)
                    logger.info("Deselected previous market %r", current_market)
                    break
                except PlaywrightTimeoutError:
                    continue
        try:
            frame.page.keyboard.press("Escape")
        except Exception:
            pass
        frame.wait_for_timeout(400)

    def _market_primary_token(self, market: str) -> str:
        """First word of a MARKET value — used to search IQVIA compound labels."""
        words = market.strip().split()
        return words[0] if words else ""

    def _market_inline_filter_open(self) -> bool:
        """True when the pivot Market picker popup (Filter input + list) is open."""
        for scope in self._inline_filter_scopes():
            try:
                loc = scope.locator(
                    'input[placeholder="Filter"], input[placeholder="filter"]'
                ).first
                if loc.is_visible(timeout=200):
                    return True
            except PlaywrightTimeoutError:
                continue
        return False

    def _click_market_filter_label(self, frame) -> str:
        """Click Market value/label on the right pivot — not the chevron menu."""
        clicked = frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'nobr, span, a, td, div'
                )) {
                    if (el.tagName === 'IMG') continue;
                    const text = trim(el.textContent);
                    if (!text.includes('Market')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 320) continue;
                    if (text.length > 120) continue;
                    const area = r.width * r.height;
                    if (!best || area < best.area) {
                        best = { el, text: text.slice(0, 60), area };
                    }
                }
                if (!best) return null;
                best.el.click();
                return best.text;
            }
            """
        )
        if clicked:
            self._settle(frame, 100)
            return f"js-label:{clicked}"

        coords = self._find_market_filter_field_coords(frame)
        if coords is not None:
            fbox = frame.frame_element().bounding_box()
            if fbox:
                frame.page.mouse.click(
                    fbox["x"] + coords["noneX"],
                    fbox["y"] + coords["noneY"],
                )
                self._settle(frame, 100)
                return "coords-nobr"

        raise PlaywrightTimeoutError(
            "Could not click Market filter label on right-side pivot"
        )

    def _click_pivot_grid_market_header(self, frame) -> bool:
        """Click the Market row header in the pivot grid (PivotTable1 rows)."""
        tree_right = self._schema_tree_right_edge(frame)
        clicked = frame.evaluate(
            """
            ([treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 24;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Market') continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.left < minLeft || r.top < 180 || r.width <= 0) continue;
                    el.click();
                    return true;
                }
                return false;
            }
            """,
            [tree_right],
        )
        if clicked:
            self._settle(frame, 100)
        return bool(clicked)

    def _open_market_inline_filter_popup(self, frame) -> None:
        """Open the Filter-input market picker (not Set Filter dialog)."""
        if self._market_inline_filter_open():
            logger.info("Market inline filter popup already open")
            return

        for attempt in range(1, 4):
            method = self._click_market_filter_label(frame)
            logger.info(
                "Clicked Market on pivot (%s, attempt %d)",
                method,
                attempt,
            )
            if self._poll_until(
                frame, self._market_inline_filter_open, timeout_ms=4_000
            ):
                logger.info("Market inline filter popup open")
                return

            if self._click_pivot_grid_market_header(frame):
                logger.info(
                    "Clicked pivot grid Market header (attempt %d)", attempt
                )
                if self._poll_until(
                    frame, self._market_inline_filter_open, timeout_ms=4_000
                ):
                    logger.info("Market inline filter popup open")
                    return

        raise PlaywrightTimeoutError(
            "Market inline filter popup (Filter input) did not open — "
            "click Market on the right pivot, not Set Filter..."
        )

    def _pick_market_row_js(self) -> str:
        """Shared browser-side helpers for inline Market filter row picking."""
        return """
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const normKey = (s) => trim(s)
                    .replace(/\\bMKT\\b/gi, '')
                    .replace(/\\bDRV\\b/gi, '')
                    .replace(/\\bMARKET\\b/gi, '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toUpperCase();
                const isNoiseLabel = (text) => {
                    const tu = trim(text).toUpperCase();
                    if (!tu) return true;
                    if (tu === 'ALL') return true;
                    if (tu === 'MARKET' || tu === 'MARKET (NONE)') return true;
                    if (/^MARKET\\b/i.test(tu) && tu.length <= 20) return true;
                    return false;
                };
                const keysMatch = (left, right) => {
                    const lu = trim(left).toUpperCase();
                    const ru = trim(right).toUpperCase();
                    if (!lu || !ru || isNoiseLabel(left) || isNoiseLabel(right)) return false;
                    if (lu === ru) return true;
                    const ln = normKey(left);
                    const rn = normKey(right);
                    if (!ln || !rn) return false;
                    if (ln === rn) return true;
                    if (ln.length >= 6 && rn.length >= 6 && (ln.includes(rn) || rn.includes(ln))) {
                        return true;
                    }
                    for (const compound of [ln, rn]) {
                        if (!compound.includes('/')) continue;
                        for (const part of compound.split('/')) {
                            const p = trim(part);
                            const other = compound === ln ? rn : ln;
                            if (p && p === trim(other.split('/')[0])) return true;
                        }
                    }
                    return false;
                };
        """

    def _click_market_inline_option(
        self, scope, *, match_text: str, mode: str
    ) -> str | None:
        """Click a Market row: exact, prefix, or fuzzy (compound / slash labels)."""
        primary = self._market_primary_token(match_text)
        helpers = self._pick_market_row_js()
        return scope.evaluate(
            f"""
            ([matchText, mode, primary]) => {{
                {helpers}
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const target = matchText.toUpperCase();
                const primaryU = (primary || '').toUpperCase();
                let best = null;
                let bestScore = -1;
                for (const el of document.querySelectorAll(
                    'span, td, div, li, a, label, nobr, tr'
                )) {{
                    const text = trim(el.textContent);
                    if (!text || text.length > 140 || isNoiseLabel(text)) continue;
                    if (primaryU && text.length < primaryU.length + 2) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const tu = text.toUpperCase();
                    let score = -1;
                    if (mode === 'exact') {{
                        if (tu === target) score = 100;
                    }} else if (mode === 'prefix') {{
                        if (tu === target) score = 100;
                        else if (tu.startsWith(target + ' ')) score = 90 + tu.length;
                        else if (tu.startsWith(target + '/')) score = 98 + tu.length;
                        else if (primaryU && tu.startsWith(primaryU + '/')) score = 97 + tu.length;
                        else if (primaryU && tu.includes('/' + primaryU)) score = 96 + tu.length;
                    }} else if (mode === 'fuzzy') {{
                        if (keysMatch(text, matchText)) score = 100;
                        else if (primaryU && tu.startsWith(primaryU + '/')) score = 98 + tu.length;
                        else if (primaryU && tu.includes('/' + primaryU)) score = 97 + tu.length;
                        else if (primaryU && tu.includes(primaryU) && (tu.includes('DRV') || tu.includes('MARKET'))) {{
                            score = 95 + tu.length;
                        }}
                        else if (primaryU && tu.startsWith(primaryU + ' ')) score = 95 + tu.length;
                        else if (primaryU && tu === primaryU && tu.length > 6) score = 90;
                    }}
                    if (score > bestScore) {{
                        bestScore = score;
                        best = {{ el, text, score }};
                    }}
                }}
                if (!best || bestScore < 90) return null;
                if (isNoiseLabel(best.text)) return null;
                best.el.click();
                return best.text;
            }}
            """,
            [match_text, mode, primary],
        )

    def _click_best_market_row_for_tsv(self, scope, market: str) -> str | None:
        """Pick the best visible Market row for a TSV MARKET value (handles / compounds)."""
        candidates = self._market_name_candidates(market)
        primary = self._market_primary_token(market)
        helpers = self._pick_market_row_js()
        picked = scope.evaluate(
            f"""
            ([candidates, primary]) => {{
                {helpers}
                const primaryU = (primary || '').toUpperCase();
                let best = null;
                let bestScore = -1;
                for (const el of document.querySelectorAll(
                    'span, td, div, li, a, label, nobr, tr'
                )) {{
                    const text = trim(el.textContent);
                    if (!text || text.length > 140 || isNoiseLabel(text)) continue;
                    if (primaryU && text.length < primaryU.length + 2) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const tu = text.toUpperCase();
                    const area = r.width * r.height;
                    let score = -1;
                    for (const cand of candidates) {{
                        if (keysMatch(text, cand)) score = Math.max(score, 100);
                    }}
                    if (primaryU) {{
                        if (tu.startsWith(primaryU + '/')) score = Math.max(score, 98 + tu.length);
                        else if (tu.includes('/' + primaryU)) score = Math.max(score, 97 + tu.length);
                        else if (tu.includes(primaryU) && (tu.includes('DRV') || tu.includes('MARKET'))) {{
                            score = Math.max(score, 95 + tu.length);
                        }}
                        else if (tu.startsWith(primaryU + ' ')) score = Math.max(score, 95 + tu.length);
                        else {{
                            const first = tu.split('/')[0].trim();
                            if (first === primaryU) score = Math.max(score, 96 + tu.length);
                        }}
                    }}
                    if (score > bestScore || (score === bestScore && best && area < best.area)) {{
                        bestScore = score;
                        best = {{ el, text, score, area }};
                    }}
                }}
                if (!best || bestScore < 90) return null;
                if (isNoiseLabel(best.text)) return null;
                best.el.click();
                return best.text;
            }}
            """,
            [candidates, primary],
        )
        if picked and picked.strip().upper() in {"MARKET", "ALL"}:
            return None
        return picked or None

    def _try_select_market_inline_tier(
        self,
        scope,
        filter_input,
        frame,
        *,
        market: str,
        mode: str,
    ) -> str | None:
        """Try one match tier in the inline Market picker."""
        market = market.strip()
        if mode == "exact":
            match_text = market
        elif mode == "prefix":
            match_text = self._market_two_word_prefix(market)
            if not match_text:
                return None
        elif mode == "first_word_then_exact":
            words = market.split()
            if not words:
                return None
            # Narrow the list first (often virtualized), then try exact click again.
            match_text = words[0]
        else:
            return None

        try:
            filter_input.click(timeout=3_000)
            filter_input.fill("")
            if mode in {"prefix", "first_word_then_exact"}:
                filter_input.fill(match_text)
            frame.wait_for_timeout(400)
        except PlaywrightTimeoutError:
            pass

        if mode in {"exact", "first_word_then_exact"}:
            labels = scope.get_by_text(market, exact=True)
            for index in range(labels.count()):
                label = labels.nth(index)
                try:
                    if not label.is_visible(timeout=300):
                        continue
                except PlaywrightTimeoutError:
                    continue
                try:
                    label.click(timeout=5_000)
                    return market
                except PlaywrightTimeoutError:
                    continue

        if mode == "first_word_then_exact":
            picked = self._click_best_market_row_for_tsv(scope, market)
            if picked:
                return picked
            picked = self._click_market_inline_option(
                scope, match_text=match_text, mode="fuzzy"
            )
            if picked:
                return picked

        return self._click_market_inline_option(
            scope,
            match_text=(market if mode == "first_word_then_exact" else match_text),
            mode=("fuzzy" if mode == "first_word_then_exact" else mode),
        )

    def _market_name_candidates(self, market: str) -> list[str]:
        """IQVIA label variants for one MARKET value (MKT/MARKET/DRV).

        Also covers compound IQVIA names like "OVIDREL/GONAL-F 75 IU DRV MARKET"
        where the TSV has only the first product name ("OVIDREL DRV MKT").
        """
        market = market.strip()
        if not market:
            return []
        candidates = [market]
        upper = market.upper()
        # "ASPIRIN MKT" → "ASPIRIN DRV MKT"
        if " DRV " not in upper and upper.endswith(" MKT"):
            candidates.append(f"{market[:-4]} DRV MKT")
        if upper.endswith(" MKT"):
            # "ASPIRIN DRV MKT" → "ASPIRIN DRV MARKET"
            candidates.append(market[:-4] + " MARKET")
            # Avoid double-DRV: only replace MKT with MARKET, not DRV MARKET
            candidates.append(re.sub(r"\bMKT\b", "MARKET", market, flags=re.I))
        elif "MKT" in upper:
            candidates.append(re.sub(r"\bMKT\b", "MARKET", market, flags=re.I))
        seen: set[str] = set()
        return [
            c for c in candidates if c and not (c.lower() in seen or seen.add(c.lower()))
        ]

    def _market_search_terms(self, market: str) -> list[str]:
        """Search needles for one MARKET value (from TSV / --market), no hardcoded names."""
        market = market.strip()
        if not market:
            return []
        terms: list[str] = []
        for cand in self._market_name_candidates(market):
            terms.append(cand)
            prefix = self._market_two_word_prefix(cand)
            if prefix:
                terms.append(prefix)
            words = cand.split()
            if words:
                terms.append(words[0])
            digit = re.search(r"\b(\d+)\b", cand)
            if digit and words:
                terms.append(f"{words[0]} {digit.group(1)}")
        seen: set[str] = set()
        return [
            t for t in terms if t and not (t.lower() in seen or seen.add(t.lower()))
        ]

    def _open_market_set_filter_dialog(self) -> None:
        """Market filter chevron → Set Filter… → Filter Condition Settings."""
        frame = self._designer_frame()
        market = self.market_dimension

        for attempt in range(1, 6):
            self._clear_open_popups(frame)
            if not self._ensure_market_filter_menu_open(frame, attempts=2):
                coords = self._find_market_filter_field_coords(frame)
                if coords:
                    frame.page.mouse.click(coords["chevronX"], coords["chevronY"])
                    frame.wait_for_timeout(500)

            if not self._poll_until(
                frame,
                lambda: self._set_filter_menu_open(frame)
                or self._pivot_row_filter_menu_open(),
                timeout_ms=2_500,
            ):
                logger.info(
                    "%r filter menu not open after chevron (attempt %d)",
                    market,
                    attempt,
                )
                continue

            if not self._set_filter_menu_open(frame):
                try:
                    self._open_filter_submenu_if_needed()
                except PlaywrightTimeoutError:
                    logger.info(
                        "Filter submenu missing for %r (attempt %d)", market, attempt
                    )
                    continue

            self._click_set_filter_menu()
            frame.wait_for_timeout(1_200)
            try:
                self._wait_for_filter_condition_dialog(timeout_ms=20_000)
            except PlaywrightTimeoutError as exc:
                logger.warning(
                    "Set Filter dialog not ready (attempt %d): %s", attempt, exc
                )
                self._dismiss_filter_condition_dialog()
                frame.wait_for_timeout(400)
                continue

            actual = self._filter_condition_dialog_dimension()
            if actual and actual.lower() != market.lower():
                logger.warning(
                    "Expected %r filter dialog, got %r — retrying (attempt %d)",
                    market,
                    actual,
                    attempt,
                )
                self._dismiss_filter_condition_dialog()
                frame.wait_for_timeout(400)
                continue
            return

        raise PlaywrightTimeoutError(
            f"Could not open Set Filter for {market!r} "
            f"(last dimension: {self._filter_condition_dialog_dimension()!r})"
        )

    def _check_member_row_in_filter_dialog(self, member: str) -> bool:
        """Check one member row in an open Filter Condition Settings dialog."""
        member = member.strip()
        if not member:
            return False
        for scope in self._filter_dialog_scopes():
            result = scope.evaluate(
                """
                ([member]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const norm = (s) => trim(s).toUpperCase();
                    const target = norm(member);
                    let dialog = null;
                    let bestArea = Infinity;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        if (!text.includes('Filter Method')) continue;
                        if (!text.includes('OK')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const area = r.width * r.height;
                        if (area < bestArea) {
                            bestArea = area;
                            dialog = el;
                        }
                    }
                    if (!dialog) return { ok: false };

                    const rowCells = (row) =>
                        Array.from(row.querySelectorAll('td, span, label, nobr'));
                    const rowMatchesMember = (row) => {
                        for (const cell of rowCells(row)) {
                            const t = norm(cell.textContent);
                            if (t === target || t.startsWith(target)) return cell;
                            if (target.startsWith(t) && t.length > 4) return cell;
                        }
                        return null;
                    };
                    const clickRow = (row, cell) => {
                        const cb = row.querySelector('input[type="checkbox"]');
                        const img = row.querySelector('img');
                        const hit = cb || img || cell;
                        hit.scrollIntoView({ block: 'center', inline: 'nearest' });
                        hit.click();
                        return true;
                    };

                    const rows = dialog.querySelectorAll('tr, li, .rtLI');
                    for (const row of rows) {
                        const cell = rowMatchesMember(row);
                        if (!cell) continue;
                        const cb = row.querySelector('input[type="checkbox"]');
                        if (!cb || !cb.checked) clickRow(row, cell);
                        return { ok: true, label: trim(cell.textContent) };
                    }
                    return { ok: false };
                }
                """,
                [member],
            )
            if result and result.get("ok"):
                logger.info(
                    "Checked member %r in filter dialog (matched %r)",
                    member,
                    result.get("label", member),
                )
                return True

        for scope in self._filter_dialog_scopes():
            for cand in self._market_name_candidates(member):
                if self._toggle_filter_dialog_row(scope, cand):
                    logger.info("Checked member %r via filter dialog row toggle", cand)
                    return True
        return False

    def _uncheck_root_all_in_filter_dialog(self) -> None:
        """Uncheck the root All / All * row in an open filter dialog."""
        for scope in self._filter_dialog_scopes():
            toggled = scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    let dialog = null;
                    let bestArea = Infinity;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        if (!text.includes('Filter Method')) continue;
                        if (!text.includes('OK')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const area = r.width * r.height;
                        if (area < bestArea) {
                            bestArea = area;
                            dialog = el;
                        }
                    }
                    if (!dialog) return false;
                    for (const row of dialog.querySelectorAll('tr, li, .rtLI')) {
                        let label = '';
                        for (const cell of row.querySelectorAll('td, span, label, nobr')) {
                            const t = trim(cell.textContent);
                            if (t === 'All' || t.startsWith('All ')) {
                                label = t;
                                break;
                            }
                        }
                        if (!label) continue;
                        const cb = row.querySelector('input[type="checkbox"]');
                        if (cb?.checked) {
                            (cb || row).click();
                            return true;
                        }
                        return false;
                    }
                    return false;
                }
                """
            )
            if toggled:
                logger.info("Unchecked root All row in filter dialog")
                return

    def _check_member_row_in_filter_dialog_fuzzy(self, needle: str) -> bool:
        """Check a member row whose label contains needle (case-insensitive)."""
        needle = needle.strip().upper()
        if not needle:
            return False
        for scope in self._filter_dialog_scopes():
            result = scope.evaluate(
                """
                ([needle]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const norm = (s) => trim(s).toUpperCase();
                    let dialog = null;
                    let bestArea = Infinity;
                    for (const el of document.querySelectorAll('div, table')) {
                        const text = el.textContent || '';
                        if (!text.includes('Filter Condition Settings')) continue;
                        if (!text.includes('Filter Method')) continue;
                        if (!text.includes('OK')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const area = r.width * r.height;
                        if (area < bestArea) {
                            bestArea = area;
                            dialog = el;
                        }
                    }
                    if (!dialog) return { ok: false };

                    let best = null;
                    for (const row of dialog.querySelectorAll('tr, li, .rtLI')) {
                        for (const cell of row.querySelectorAll('td, span, label, nobr')) {
                            const t = norm(cell.textContent);
                            if (!t || t === 'ALL' || t.startsWith('ALL ')) continue;
                            if (!t.includes(needle)) continue;
                            const cb = row.querySelector('input[type="checkbox"]');
                            const score = t === needle ? 100 : (t.startsWith(needle) ? 90 : 80);
                            if (!best || score > best.score) {
                                best = { row, cell, label: trim(cell.textContent), score };
                            }
                        }
                    }
                    if (!best) return { ok: false };
                    const cb = best.row.querySelector('input[type="checkbox"]');
                    const hit = cb || best.cell;
                    hit.scrollIntoView({ block: 'center', inline: 'nearest' });
                    if (!cb || !cb.checked) hit.click();
                    return { ok: true, label: best.label };
                }
                """,
                [needle],
            )
            if result and result.get("ok"):
                logger.info(
                    "Checked fuzzy member %r (matched %r)",
                    needle,
                    result.get("label"),
                )
                return True
        return False

    def _select_markets_in_filter_dialog(self, markets: list[str]) -> None:
        """Uncheck All once, then check each market in Set Filter dialog."""
        frame = self._designer_frame()
        self._wait_for_filter_dialog_ready(timeout_ms=45_000)
        self._expand_filter_dialog_tree()
        self._uncheck_root_all_in_filter_dialog()
        frame.wait_for_timeout(500)

        selected: list[str] = []
        for market in markets:
            found = False
            for cand in self._market_name_candidates(market):
                if self._check_member_row_in_filter_dialog(cand):
                    selected.append(market)
                    found = True
                    break
            if not found:
                for needle in self._market_search_terms(market)[1:]:
                    if self._check_member_row_in_filter_dialog_fuzzy(needle):
                        selected.append(market)
                        found = True
                        break
            if not found:
                logger.warning("Could not find market %r in filter dialog", market)

        if not selected:
            raise PlaywrightTimeoutError(
                f"Could not select any markets in filter dialog: {markets!r}"
            )
        logger.info("Selected %d market(s) in filter dialog: %s", len(selected), selected)

    def _try_pick_single_market_in_inline_filter(self, market: str) -> bool:
        """Pick one MARKET: type first word, click best matching row."""
        market = market.strip()
        if not market:
            return False
        return self._search_and_pick_market_inline(market)

    def _select_market_from_inline_filter(self, market: str) -> None:
        """Pick MARKET: full name → first word (no silent fallback to All)."""
        if not self._try_pick_single_market_in_inline_filter(market):
            logger.warning("No market match for %r in inline filter", market)

    def _type_into_market_filter(
        self, filter_input, frame, term: str, *, char_by_char: bool
    ) -> None:
        """Type into the Market filter box (char-by-char triggers IQVIA list filter)."""
        filter_input.click(timeout=2_000)
        filter_input.fill("")
        frame.wait_for_timeout(200)
        if char_by_char:
            filter_input.press_sequentially(term, delay=80)
            frame.wait_for_timeout(900)
        else:
            filter_input.fill(term)
            frame.wait_for_timeout(600)

    def _type_and_click_market_row(
        self,
        scope,
        filter_input,
        frame,
        term: str,
        *,
        exact: bool,
    ) -> str | None:
        """Type *term* in filter box and click the best matching list row."""
        term = term.strip()
        if not term:
            return None
        try:
            self._type_into_market_filter(
                filter_input, frame, term, char_by_char=not exact
            )
        except PlaywrightTimeoutError:
            return None

        row_sel = "span, td, div, li, a, label, nobr, tr"
        scopes = [scope]
        try:
            designer = self._designer_frame()
            if designer not in scopes:
                scopes.append(designer)
        except PlaywrightTimeoutError:
            pass
        if self.page not in scopes:
            scopes.append(self.page)

        for click_scope in scopes:
            if exact:
                locator = click_scope.get_by_text(term, exact=True)
            else:
                primary = term.strip()
                locator = click_scope.locator(row_sel).filter(
                    has_text=re.compile(
                        rf".*{re.escape(primary)}.*DRV", re.I
                    )
                )
                if locator.count() == 0:
                    locator = click_scope.locator(row_sel).filter(
                        has_text=re.compile(
                            rf"^{re.escape(primary)}\s*/", re.I
                        )
                    )
                if locator.count() == 0:
                    locator = click_scope.locator(row_sel).filter(
                        has_text=re.compile(re.escape(primary), re.I)
                    )

            for index in range(min(locator.count(), 25)):
                item = locator.nth(index)
                try:
                    if not item.is_visible(timeout=400):
                        continue
                    text = " ".join(item.inner_text(timeout=1_000).split())
                    upper = text.upper()
                    if upper in {"MARKET", "ALL", "MARKET (NONE)"}:
                        continue
                    if upper == "MARKET" or (
                        upper.startswith("MARKET ") and len(text) <= 20
                    ):
                        continue
                    if upper.endswith("(NONE)") or upper == "PIVOTTABLE1":
                        continue
                    if "DRV" not in upper and "MKT" not in upper:
                        continue
                    item.scroll_into_view_if_needed(timeout=2_000)
                    item.click(timeout=5_000)
                    return text
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue

        # Keyboard fallback — only if list likely narrowed but click failed.
        try:
            filter_input.press("ArrowDown")
            frame.wait_for_timeout(300)
            filter_input.press("Enter")
            frame.wait_for_timeout(500)
        except PlaywrightTimeoutError:
            pass
        return None

    def _click_market_dropdown_row(
        self,
        filter_input,
        frame,
        market: str,
        primary: str,
    ) -> str | None:
        """Click the visible filtered row in the Market dropdown."""
        if not primary:
            return None

        # 1) Keyboard — works when filter leaves one row (see screenshot).
        try:
            filter_input.click(timeout=2_000)
            frame.wait_for_timeout(300)
            filter_input.press("ArrowDown")
            frame.wait_for_timeout(250)
            filter_input.press("Enter")
            frame.wait_for_timeout(600)
        except PlaywrightTimeoutError:
            pass

        row_pattern = re.compile(
            rf".*{re.escape(primary)}.*(?:DRV|MARKET|MKT)",
            re.I,
        )
        row_selector = "span, td, div, li, a, label, nobr, tr"

        for click_scope in self._market_filter_click_scopes():
            # 2) Playwright get_by_text — click the dropdown label directly.
            try:
                labels = click_scope.get_by_text(row_pattern)
                for index in range(min(labels.count(), 8)):
                    item = labels.nth(index)
                    try:
                        if not item.is_visible(timeout=400):
                            continue
                        text = " ".join(item.inner_text(timeout=1_000).split())
                        if not self._valid_market_pick(text, market):
                            continue
                        item.click(force=True, timeout=4_000)
                        return text
                    except PlaywrightTimeoutError:
                        continue
            except Exception:
                pass

            # 3) JS — smallest visible row under the Filter box.
            try:
                picked = click_scope.evaluate(
                    """
                    ([primary]) => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const pu = (primary || '').toUpperCase();
                        const filter = document.querySelector(
                            'input[placeholder="Filter"], input[placeholder="filter"]'
                        );
                        const fy = filter ? filter.getBoundingClientRect().bottom : 0;
                        let best = null;
                        let bestArea = Infinity;
                        for (const el of document.querySelectorAll(
                            'span, td, div, li, a, label, nobr'
                        )) {
                            const text = trim(el.textContent);
                            if (!text || text.length > 120 || text.length < pu.length + 4) continue;
                            const tu = text.toUpperCase();
                            if (tu === 'ALL' || tu === 'MARKET' || tu === 'MARKET (NONE)') continue;
                            if (!tu.includes(pu)) continue;
                            if (!tu.includes('DRV') && !tu.includes('MARKET') && !tu.includes('MKT')) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            if (filter && r.top < fy - 2) continue;
                            const area = r.width * r.height;
                            if (area < bestArea) {
                                bestArea = area;
                                best = { el, text };
                            }
                        }
                        if (!best) return null;
                        best.el.click();
                        return best.text;
                    }
                    """,
                    [primary],
                )
                if picked and self._valid_market_pick(picked, market):
                    return picked
            except Exception:
                pass

            # 4) Locator filter fallback.
            try:
                locator = click_scope.locator(row_selector).filter(
                    has_text=row_pattern
                )
                for index in range(min(locator.count(), 10)):
                    item = locator.nth(index)
                    try:
                        if not item.is_visible(timeout=400):
                            continue
                        text = " ".join(item.inner_text(timeout=1_000).split())
                        if not self._valid_market_pick(text, market):
                            continue
                        item.click(force=True, timeout=4_000)
                        return text
                    except PlaywrightTimeoutError:
                        continue
            except Exception:
                pass

        # 5) Mouse click directly under the Filter input.
        try:
            box = filter_input.bounding_box()
            if box:
                page = frame.page
                for offset in (24, 38):
                    page.mouse.click(
                        box["x"] + box["width"] * 0.5,
                        box["y"] + box["height"] + offset,
                    )
                    frame.wait_for_timeout(400)
        except Exception:
            pass

        return None

    def _search_and_pick_market_inline(self, market: str) -> bool:
        """Type first word → click dropdown row → close popup."""
        market = market.strip()
        if not market:
            return False
        frame = self._designer_frame()
        primary = self._market_primary_token(market)

        for scope in self._inline_filter_scopes():
            filter_input = scope.locator(
                'input[placeholder="Filter"], input[placeholder="filter"]'
            ).first
            try:
                filter_input.wait_for(state="visible", timeout=2_000)
            except PlaywrightTimeoutError:
                continue

            if not primary:
                continue

            try:
                self._type_into_market_filter(
                    filter_input, frame, primary, char_by_char=True
                )
            except PlaywrightTimeoutError:
                continue

            logger.info(
                "Market filter: typed %r, clicking dropdown row for %r",
                primary,
                market,
            )

            picked = self._click_market_dropdown_row(
                filter_input, frame, market, primary
            )
            self._dismiss_market_inline_filter(frame)
            frame.wait_for_timeout(500)

            current = self._current_pivot_market_value(frame)
            if current and self._markets_match(current, market):
                logger.info(
                    "Selected market %r → %r",
                    market,
                    current,
                )
                return True
            if picked and self._markets_match(picked, market):
                logger.info(
                    "Selected market %r → %r",
                    market,
                    picked,
                )
                return True

            # Retry once: reopen, retype, click again.
            self._open_market_inline_filter_popup(frame)
            try:
                self._type_into_market_filter(
                    filter_input, frame, primary, char_by_char=True
                )
                self._click_market_dropdown_row(
                    filter_input, frame, market, primary
                )
                self._dismiss_market_inline_filter(frame)
                frame.wait_for_timeout(500)
                current = self._current_pivot_market_value(frame)
                if current and self._markets_match(current, market):
                    logger.info(
                        "Selected market %r on retry → %r",
                        market,
                        current,
                    )
                    return True
            except PlaywrightTimeoutError:
                pass

        return False

    def _inline_filter_scopes(self):
        """Frames that currently show the inline Market filter input."""
        scopes = []
        for frame in self._walk_page_frames():
            try:
                loc = frame.locator(
                    'input[placeholder="Filter"], input[placeholder="filter"]'
                ).first
                if loc.count() > 0 and loc.is_visible(timeout=200):
                    scopes.append(frame)
            except Exception:
                continue
        if not scopes:
            try:
                scopes.append(self._designer_frame())
            except PlaywrightTimeoutError:
                pass
            scopes.append(self.page)
        return scopes

    def _market_filter_click_scopes(self):
        """Frames/page to search when clicking a row in the Market filter list."""
        scopes: list = []
        seen: set[int] = set()

        def add(scope) -> None:
            key = id(scope)
            if key not in seen:
                seen.add(key)
                scopes.append(scope)

        for scope in self._inline_filter_scopes():
            add(scope)
        add(self.page)
        try:
            add(self._designer_frame())
        except PlaywrightTimeoutError:
            pass
        return scopes

    def _dismiss_market_inline_filter(self, frame) -> None:
        """Close the Market filter popup so the pivot can apply the selection."""
        if not self._market_inline_filter_open():
            return
        try:
            frame.page.keyboard.press("Escape")
            frame.wait_for_timeout(300)
        except Exception:
            pass
        if self._market_inline_filter_open():
            fbox = frame.frame_element().bounding_box()
            if fbox:
                frame.page.mouse.click(
                    fbox["x"] + fbox["width"] * 0.55,
                    fbox["y"] + 320,
                )
                frame.wait_for_timeout(400)

    def _click_market_row_by_text_locator(self, scope, market: str) -> str | None:
        """Click a filtered market row — handles OVIDREL/GONAL-F compound labels."""
        primary = self._market_primary_token(market)
        if not primary:
            return None
        patterns = [
            re.compile(rf".*\b{re.escape(primary)}\b.*\bDRV\b", re.I),
            re.compile(rf".*\b{re.escape(primary)}\b.*\bMARKET\b", re.I),
            re.compile(rf"/{re.escape(primary)}\b", re.I),
            re.compile(rf"^{re.escape(primary)}/", re.I),
        ]
        row_selector = "span, td, div, li, a, label, nobr, tr"
        for pattern in patterns:
            locator = scope.locator(row_selector).filter(has_text=pattern)
            count = min(locator.count(), 20)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not item.is_visible(timeout=500):
                        continue
                    text = " ".join(item.inner_text(timeout=1_000).split())
                    if not self._valid_market_pick(text, market):
                        continue
                    if not self._markets_match(text, market):
                        continue
                    item.scroll_into_view_if_needed(timeout=3_000)
                    item.click(force=True, timeout=5_000)
                    return text
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue
        return None

    def _click_market_row_below_filter(
        self, filter_input, frame
    ) -> bool:
        """Click the first filtered row directly under the Filter input."""
        try:
            box = filter_input.bounding_box()
            if not box:
                return False
            page = frame.page
            for offset in (22, 34, 48):
                page.mouse.click(
                    box["x"] + box["width"] * 0.45,
                    box["y"] + box["height"] + offset,
                )
                frame.wait_for_timeout(350)
            return True
        except Exception:
            return False

    def _click_market_row_playwright(self, scope, market: str) -> str | None:
        """Click a visible market row using Playwright locators (more reliable than JS)."""
        primary = self._market_primary_token(market)
        if not primary:
            return None
        patterns = [
            re.compile(rf"^{re.escape(primary)}/", re.I),
            re.compile(rf"/{re.escape(primary)}\b", re.I),
            re.compile(rf".*\b{re.escape(primary)}\b.*\bDRV\b", re.I),
            re.compile(rf"^{re.escape(primary)}\s+DRV\b", re.I),
            re.compile(rf"^{re.escape(primary)}\s", re.I),
        ]
        row_selector = "span, td, div, li, a, label, nobr, tr"
        for pattern in patterns:
            locator = scope.locator(row_selector).filter(has_text=pattern)
            count = min(locator.count(), 30)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not item.is_visible(timeout=400):
                        continue
                    text = " ".join(item.inner_text(timeout=1_000).split())
                    if not self._valid_market_pick(text, market):
                        continue
                    item.scroll_into_view_if_needed(timeout=3_000)
                    item.click(force=True, timeout=5_000)
                    return text
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue
        return None

    def _click_market_row_keyboard(
        self, filter_input, frame, *, primary: str, market: str
    ) -> str | None:
        """Arrow-down + Enter in the filter box when list rows won't click."""
        try:
            filter_input.click(timeout=2_000)
            filter_input.press("ArrowDown")
            frame.wait_for_timeout(300)
            filter_input.press("Enter")
            frame.wait_for_timeout(600)
        except PlaywrightTimeoutError:
            return None
        current = self._current_pivot_market_value(frame)
        if current and self._markets_match(current, market):
            return current
        return primary if primary else None

    def _valid_market_pick(self, picked: str | None, market: str) -> bool:
        """Reject pivot labels like 'Market' mistaken for a market row."""
        if not picked:
            return False
        text = picked.strip()
        upper = text.upper()
        if upper in {"MARKET", "ALL", "MARKET (NONE)"}:
            return False
        if upper.startswith("MARKET ") and len(text) <= 20:
            return False
        primary = self._market_primary_token(market)
        if primary and len(text) < len(primary) + 2:
            return False
        if self._markets_match(text, market):
            return True
        if primary and primary.upper() in text.upper():
            upper_text = text.upper()
            if "DRV" in upper_text or "MARKET" in upper_text or "MKT" in upper_text:
                return True
        return False

    def _select_markets_from_inline_filter(self, markets: list[str]) -> None:
        """Pick multiple MARKET values in one inline filter session."""
        selected: list[str] = []
        for market in markets:
            if self._search_and_pick_market_inline(market):
                selected.append(market)
            else:
                logger.warning("Could not select market %r", market)
        if not selected:
            logger.warning("No markets matched — selecting 'All'")
            self._select_all_from_inline_filter()
        else:
            logger.info("Selected %d market(s): %s", len(selected), selected)

    def _select_all_from_inline_filter(self) -> None:
        """Pick 'All' in the Market inline filter picker."""
        for scope in self._market_filter_click_scopes():
            labels = scope.get_by_text("All", exact=True)
            for index in range(labels.count()):
                label = labels.nth(index)
                try:
                    if not label.is_visible(timeout=300):
                        continue
                except PlaywrightTimeoutError:
                    continue
                try:
                    label.click(timeout=5_000)
                    logger.info("Selected 'All' in Market inline Filter list")
                    return
                except PlaywrightTimeoutError:
                    continue

            picked = scope.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    for (const el of document.querySelectorAll(
                        'span, td, div, li, a, label, nobr'
                    )) {
                        if (trim(el.textContent) !== 'All') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        el.click();
                        return true;
                    }
                    return false;
                }
                """
            )
            if picked:
                logger.info("Selected 'All' in Market inline Filter list (JS)")
                return

        logger.warning("Could not click 'All' in Market inline filter — continuing")

    def _market_filter_satisfied(self, frame, market: str) -> bool:
        if self._market_filter_applied(frame, market):
            return True
        current = self._current_pivot_market_value(frame)
        if current and self._markets_match(current, market):
            return True
        coords = self._find_market_filter_field_coords(frame)
        if coords:
            field_text = coords.get("text") or ""
            if "(None)" not in field_text and self._markets_match(field_text, market):
                return True
        return False

    def set_market_filter(self, market: str) -> None:
        """Click Market on pivot → Filter input → pick market from TSV list."""
        frame = self._designer_frame()
        markets = [m.strip() for m in market.split(",") if m.strip()]
        if not markets:
            logger.warning("MARKET value is empty — selecting 'All'")
            self._wait_for_market_filter_field(frame)
            self._open_market_inline_filter_popup(frame)
            self._select_all_from_inline_filter()
            frame.wait_for_timeout(400)
            if self._market_inline_filter_open():
                fbox = frame.frame_element().bounding_box()
                if fbox:
                    frame.page.mouse.click(
                        fbox["x"] + fbox["width"] * 0.55,
                        fbox["y"] + 280,
                    )
                    frame.wait_for_timeout(400)
            self._wait_for_query_idle(frame)
            return

        if len(markets) > 1:
            logger.info(
                "Setting Market filter to %d markets via Set Filter dialog…",
                len(markets),
            )
            self._wait_for_market_filter_field(frame)
            try:
                self._open_market_set_filter_dialog()
                self._select_markets_in_filter_dialog(markets)
                self._click_filter_dialog_ok()
            except PlaywrightTimeoutError as exc:
                logger.warning(
                    "Set Filter multi-market failed (%s) — trying inline picker",
                    exc,
                )
                self._dismiss_filter_condition_dialog()
                frame.wait_for_timeout(400)
                self._open_market_inline_filter_popup(frame)
                self._select_markets_from_inline_filter(markets)
                frame.wait_for_timeout(400)
                if self._market_inline_filter_open():
                    fbox = frame.frame_element().bounding_box()
                    if fbox:
                        frame.page.mouse.click(
                            fbox["x"] + fbox["width"] * 0.55,
                            fbox["y"] + 280,
                        )
                        frame.wait_for_timeout(400)
            self._wait_for_query_idle(frame)
            logger.info("Market filter applied for markets: %s", markets)
            return

        market = markets[0]
        if self._market_filter_satisfied(frame, market):
            logger.info("Market filter already set to %r", market)
            return

        current = self._current_pivot_market_value(frame)
        if (
            current
            and current.upper() != market.upper()
            and current.upper() != "MARKET"
        ):
            self._clear_pivot_market_filter(frame, current)

        logger.info(
            "Setting Market filter to %r via inline Filter popup…", market
        )

        self._wait_for_market_filter_field(frame)
        self._open_market_inline_filter_popup(frame)
        self._select_market_from_inline_filter(market)
        self._dismiss_market_inline_filter(frame)
        frame.wait_for_timeout(500)
        self._wait_for_query_idle(frame)

        if self._market_filter_satisfied(frame, market):
            logger.info("Market filter applied: %r", market)
            return

        if not self._poll_until(
            frame,
            lambda: self._market_filter_satisfied(frame, market),
            timeout_ms=8_000,
        ):
            current = self._current_pivot_market_value(frame)
            if current and self._markets_match(current, market):
                logger.info(
                    "Market filter applied as %r (matches TSV %r)",
                    current,
                    market,
                )
                return
            logger.warning(
                "Market filter verification failed for %r (pivot shows %r) — "
                "retrying selection once",
                market,
                current,
            )
            self._open_market_inline_filter_popup(frame)
            if self._try_pick_single_market_in_inline_filter(market):
                self._dismiss_market_inline_filter(frame)
                frame.wait_for_timeout(500)
                self._wait_for_query_idle(frame)
                if self._market_filter_satisfied(frame, market):
                    logger.info("Market filter applied on retry: %r", market)
                    return
            logger.warning(
                "Market filter still not verified for %r (pivot shows %r)",
                market,
                self._current_pivot_market_value(frame),
            )
            raise PlaywrightTimeoutError(
                f"Market filter could not be applied for {market!r}"
            )
        logger.info("Market filter applied: %r", market)

    def open_market_attributes_and_drag_market_to_rows(self) -> None:
        """Backward-compatible alias — Market goes to the filter zone."""
        self.open_market_attributes_and_drag_market_to_filter()

    def _find_label_drag_box(
        self,
        frame,
        label: str,
        *,
        deepest: bool = True,
        below_label: str | None = None,
    ) -> dict | None:
        """Exact tree label box for drag — avoids matching huge parent containers."""
        target = self._resolve_tree_label_locator(
            frame, label, deepest=deepest, below_label=below_label
        )
        if target is None:
            return None
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        if not box:
            return None
        return {
            "text": label,
            "x": box["x"] + box["width"] / 2,
            "y": box["y"] + box["height"] / 2,
        }

    def _sales_data_children_visible(self, frame) -> bool:
        for item in (self.units_item, self.values_item):
            try:
                frame.get_by_text(item, exact=True).first.wait_for(
                    state="visible", timeout=500
                )
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    def _sales_data_item_locator(self, frame, item: str):
        """Units / Values leaf rows under Measures → Sales Data."""
        loc = self._resolve_tree_label_locator(
            frame,
            item,
            deepest=False,
            below_label=self.sales_data_folder,
            grandparent_label=self.measures_folder,
        )
        if loc is not None:
            return loc
        return self._resolve_tree_label_locator(
            frame,
            item,
            deepest=False,
            below_label=self.sales_data_folder,
        )

    def _find_pivot_measure_units_coords(self, frame) -> dict | None:
        """Click/drop point on the Units row in the RIGHT pivot measure zone."""
        tree_right = self._schema_tree_right_edge(frame)
        simple = frame.evaluate(
            """
            ([treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Units') continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 520) continue;
                    if (r.left < minLeft) continue;
                    return {
                        x: r.x + r.width * 0.82,
                        y: r.y + r.height / 2,
                        text,
                    };
                }
                return null;
            }
            """,
            [tree_right],
        )
        if simple:
            fbox = frame.frame_element().bounding_box()
            if fbox:
                return {
                    "page_x": fbox["x"] + simple["x"],
                    "page_y": fbox["y"] + simple["y"],
                    "text": simple["text"],
                }

        hit = frame.evaluate(
            """
            ([treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let columnTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (!text.includes('Relative MAT')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 450) continue;
                    if (r.left < minLeft) continue;
                    columnTop = r.top;
                    break;
                }

                let measureRowTop = null;
                let dropZoneVisible = false;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Drop a Measure Here'
                        && !text.includes('Drop a Measure Here')) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 450) continue;
                    if (r.left < minLeft) continue;
                    measureRowTop = r.top;
                    dropZoneVisible = true;
                    break;
                }

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Units' && !text.startsWith('Units')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 500) continue;
                    if (r.left < minLeft) continue;
                    if (columnTop !== null && r.top <= columnTop + 8) continue;
                    if (
                        dropZoneVisible
                        && measureRowTop !== null
                        && Math.abs(r.top - measureRowTop) > 80
                    ) {
                        continue;
                    }
                    const score = r.left * 2 + r.top;
                    if (!best || score > best.score) {
                        best = {
                            x: r.x + r.width * 0.82,
                            y: r.y + r.height / 2,
                            text,
                            left: r.left,
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [tree_right],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _pivot_page_coords_valid(self, frame, coords: dict) -> bool:
        """True when coords land on the RIGHT pivot panel, not the left schema tree."""
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return False
        tree_right = self._schema_tree_right_edge(frame)
        min_page_x = fbox["x"] + (tree_right if tree_right > 0 else 280) + 8
        return coords.get("page_x", 0) >= min_page_x

    def _pivot_row_header_coords_valid(self, frame, coords: dict) -> bool:
        """Row-dimension headers sit in the pivot field band, not column headers."""
        if not self._pivot_page_coords_valid(frame, coords):
            return False
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return False
        frame_y = coords.get("page_y", 0) - fbox["y"]
        return 90 <= frame_y <= 420

    def _pivot_drop_coords_sane(self, frame, coords: dict | None) -> bool:
        """Reject drop targets above the pivot data band (toolbar/tab strip noise)."""
        if not coords:
            return False
        if not self._pivot_page_coords_valid(frame, coords):
            return False
        return self._pivot_drop_y_sane(frame, coords)

    def _rightmost_row_field_drop_point(self, frame) -> dict | None:
        """Drop point just RIGHT of the rightmost row-dimension header.

        The pivot lays row fields out left→right as outer→inner, so dropping a
        new field immediately to the right of the current innermost header nests
        it one level deeper (deterministic Brick → Pack → Product ordering).
        """
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));

                let measureLeft = null;
                for (const el of document.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const t = trim(el.textContent);
                    if (t !== 'Units' && !t.startsWith('Units')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 320) continue;
                    if (measureLeft === null || r.left < measureLeft) {
                        measureLeft = r.left;
                    }
                }

                let best = null;
                for (const td of document.querySelectorAll('td[area="rows"]')) {
                    if (inTree(td)) continue;
                    const r = td.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 320) continue;
                    if (r.left < treeRight) continue;
                    if (!best || r.right > best.right) {
                        best = {
                            left: r.left,
                            right: r.right,
                            top: r.top,
                            height: r.height,
                        };
                    }
                }
                if (!best) return null;

                const width = best.right - best.left;
                let x = best.right + 8;
                if (measureLeft !== null && x > measureLeft - 6) {
                    x = measureLeft - 6;
                }
                if (x < best.left + width * 0.55) {
                    x = best.left + width * 0.55;
                }
                return { x, y: best.top + best.height / 2 };
            }
            """,
            [tree_right],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": "row-insert-right",
        }

    def _pivot_drop_y_sane(self, frame, coords: dict | None) -> bool:
        """Vertical-only sanity (proven cached coords can sit near the tree edge)."""
        if not coords:
            return False
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return False
        frame_y = coords.get("page_y", 0) - fbox["y"]
        return 100 <= frame_y <= 460

    def _find_pivot_row_product_header_coords(self, frame) -> dict | None:
        """Drop point on the Product row header in the RIGHT pivot row zone."""
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight, productLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let measureLeft = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Units' && !text.startsWith('Units -')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 450) continue;
                    if (r.left < minLeft) continue;
                    measureLeft = r.left;
                    break;
                }

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (
                        text !== productLabel
                        && !text.startsWith(productLabel + ' (')
                        && !text.startsWith(productLabel + '(')
                    ) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 420) continue;
                    if (r.top < 90) continue;
                    if (r.left < minLeft) continue;
                    if (measureLeft !== null && r.left >= measureLeft - 24) continue;
                    const score = r.top * 1000 + r.left;
                    if (!best || score > best.score) {
                        best = {
                            x: r.x + r.width * 0.82,
                            y: r.y + r.height / 2,
                            text,
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [tree_right, self.product_dimension],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _find_pivot_row_brick_header_coords(self, frame) -> dict | None:
        """Drop point on the Brick row header in the RIGHT pivot row zone."""
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight, brickLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let measureLeft = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Units' && !text.startsWith('Units -')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 450) continue;
                    if (r.left < minLeft) continue;
                    measureLeft = r.left;
                    break;
                }

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (
                        text !== brickLabel
                        && !text.startsWith(brickLabel + ' (')
                        && !text.startsWith(brickLabel + '(')
                    ) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 380) continue;
                    if (r.width > 220) continue;
                    if (r.left < minLeft) continue;
                    if (measureLeft !== null && r.left >= measureLeft - 24) continue;
                    const score = r.left * 2 + r.top;
                    if (!best || score > best.score) {
                        best = {
                            x: Math.max(r.x + r.width * 0.82, minLeft + 80),
                            y: r.y + r.height / 2,
                            text,
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [tree_right, self.brick_attribute],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _find_pivot_row_brick_coords(self, frame) -> dict | None:
        """Drop point on the Brick row header (cached after Brick lands)."""
        for candidate in (
            self._find_pivot_row_brick_header_coords(frame),
            self._find_row_field_header_loose(frame, self.brick_attribute),
            self._last_brick_row_coords,
        ):
            if candidate and self._pivot_page_coords_valid(frame, candidate):
                return candidate
        return None

    def _find_pivot_row_pack_on_brick_header_coords(self, frame) -> dict | None:
        """Drop point on Pack nested on Brick in the RIGHT pivot row zone."""
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight, packLabel, brickLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let brickLeft = null;
                let brickTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (
                        text !== brickLabel
                        && !text.startsWith(brickLabel + ' (')
                        && !text.startsWith(brickLabel + '(')
                    ) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 380) continue;
                    if (r.left < minLeft) continue;
                    brickLeft = r.left;
                    brickTop = r.top;
                    break;
                }

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (
                        text !== packLabel
                        && !text.startsWith(packLabel + ' (')
                        && !text.startsWith(packLabel + '(')
                    ) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 380) continue;
                    if (r.left < minLeft) continue;
                    if (
                        brickTop !== null
                        && Math.abs(r.top - brickTop) > 48
                    ) {
                        continue;
                    }
                    const score = r.left * 2 + r.top;
                    if (!best || score > best.score) {
                        best = {
                            x: Math.max(r.x + r.width * 0.82, minLeft + 80),
                            y: r.y + r.height / 2,
                            text,
                            score,
                        };
                    }
                }
                if (best) return best;
                if (brickLeft !== null && brickTop !== null) {
                    return {
                        x: Math.max(brickLeft + 96, minLeft + 80),
                        y: brickTop + 10,
                        text: brickLabel,
                        score: 0,
                    };
                }
                return null;
            }
            """,
            [tree_right, self.pack_attribute, self.brick_attribute],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _find_pivot_row_pack_header_coords(self, frame) -> dict | None:
        """Page coords for the Pack row-dimension field header."""
        header = self._find_pivot_row_dimension_field_header(
            frame, self.pack_attribute
        )
        if header:
            return header

        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight, packLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                const matches = (span, label) => {
                    if (!span) return false;
                    const text = trim(span.textContent);
                    const title = span.getAttribute('title') || '';
                    return (
                        text === label
                        || text.startsWith(label + ' (')
                        || title.includes('[' + label + '.')
                        || title.includes('[' + label + ']')
                    );
                };

                let best = null;
                for (const td of document.querySelectorAll('td[area="rows"]')) {
                    if (inTree(td)) continue;
                    const span = td.querySelector('span[axis="r"], nobr span');
                    if (!matches(span, packLabel)) continue;
                    const sr = span.getBoundingClientRect();
                    const r = td.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top < 95 || r.top > 720) {
                        continue;
                    }
                    if (r.left < minLeft) continue;
                    const score = r.left;
                    if (!best || score > best.score) {
                        best = {
                            x: sr.x + Math.min(sr.width * 0.32, sr.width - 14),
                            y: sr.y + sr.height / 2,
                            text: trim(span.textContent),
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [tree_right, self.pack_attribute],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _values_nested_in_schema_tree(self, frame) -> bool:
        """True when Values was wrongly nested under Units in the left schema tree."""
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                if (!tree) return false;

                let unitsRow = null;
                for (const el of tree.querySelectorAll('span, a, td, div, label, li')) {
                    const text = trim(el.textContent);
                    if (text !== 'Units' && !text.startsWith('Units -')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    unitsRow = el.closest('tr, li, div') || el.parentElement;
                    break;
                }
                if (!unitsRow) return false;

                for (const el of unitsRow.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    if (trim(el.textContent) !== 'Values') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    return true;
                }
                return false;
            }
            """
        )

    def _values_nested_on_units_in_pivot(self, frame) -> bool:
        """True when Values was dropped onto the Units measure row in the pivot."""
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 24;

                let unitsHost = null;
                let unitsTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Units' && !text.startsWith('Units -')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 450) continue;
                    if (r.left < minLeft) continue;
                    unitsHost = el.closest('tr, td, table, div, li') || el.parentElement;
                    unitsTop = r.top;
                    break;
                }
                if (!unitsHost) return false;

                for (const el of unitsHost.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    if (inTree(el)) continue;
                    const text = trim(el.textContent);
                    if (text !== 'Values') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    return true;
                }

                const hostText = trim(unitsHost.textContent);
                return hostText.includes('Units') && hostText.includes('Values');
            }
            """,
            [tree_right],
        )

    def _values_on_units_verified(self, frame) -> bool:
        """True when Values was dropped onto / under the Units measure row."""
        if self._values_nested_in_schema_tree(frame):
            return False
        if self._values_nested_on_units_in_pivot(frame):
            return True
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 24;

                let unitsTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Units' && !text.startsWith('Units -')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 450) continue;
                    if (r.left < minLeft) continue;
                    unitsTop = r.top;
                    break;
                }
                if (unitsTop === null) return false;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Values') continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    if (r.top >= unitsTop - 6 && r.top <= unitsTop + 96) return true;
                }
                return false;
            }
            """,
            [tree_right],
        )

    def _verify_values_on_units(self, frame) -> None:
        if not self._poll_until(
            frame,
            lambda: self._values_on_units_verified(frame),
            timeout_ms=12_000,
        ):
            raise PlaywrightTimeoutError(
                f"Could not drop {self.values_item!r} onto pivot "
                f"{self.units_item!r} row — nested Values not visible"
            )
        logger.info(
            "Verified %r nested on pivot %r row",
            self.values_item,
            self.units_item,
        )

    def _drag_sales_data_item_to_pivot_coords(
        self, frame, item: str, coords: dict, *, verify
    ) -> None:
        """Drag a Sales Data leaf onto explicit RIGHT-side pivot coordinates."""
        source_loc = self._sales_data_item_locator(frame, item)
        if source_loc is None:
            hints = self._tree_node_hints(frame, item)
            raise PlaywrightTimeoutError(
                f"Could not find {item!r} under {self.sales_data_folder!r} "
                f"(similar: {hints})"
            ) from None

        self._scroll_schema_label_into_view(frame, item)
        self._settle(frame)

        page = frame.page
        end = (coords["page_x"], coords["page_y"])
        for attempt in range(1, 6):
            logger.info(
                "Dragging %r → right-side pivot at (%.0f, %.0f) (attempt %d)…",
                item,
                end[0],
                end[1],
                attempt,
            )
            start = self._locator_page_point(
                source_loc, x_ratio=0.32, y_ratio=0.5
            )
            if not start:
                raise PlaywrightTimeoutError(
                    f"Could not read drag start for {item!r}"
                ) from None
            self._human_mouse_drag(page, start, end)

            if self._poll_until_ui(frame, verify, idle_timeout_ms=3_000):
                logger.info("Dropped %r on right-side pivot %r row", item, coords.get("text"))
                return

            if self._values_nested_in_schema_tree(frame):
                logger.info(
                    "%r landed in left schema tree — retrying right-side drop",
                    item,
                )

            if attempt >= 5:
                raise PlaywrightTimeoutError(
                    f"Drop verification failed for {item!r} after {attempt} attempts"
                )
            logger.info("Retrying %r drop onto right-side Units…", item)
            self._scroll_schema_label_into_view(frame, item)
            refreshed = self._find_pivot_measure_units_coords(frame)
            if refreshed:
                end = (refreshed["page_x"], refreshed["page_y"])
            self._settle(frame, 200)

    def _is_inside_schema_tree(self, element) -> bool:
        return element.evaluate(
            """
            (el) => {
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                return !!(tree && tree.contains(el));
            }
            """
        )

    def _schema_tree_right_edge(self, frame) -> float:
        """Right edge of the left schema tree (frame coordinates)."""
        return frame.evaluate(
            """
            () => {
                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                if (!root) return 0;
                return root.getBoundingClientRect().right;
            }
            """
        )

    def _resolve_pivot_field_locator(self, frame, label: str):
        """Return a visible pivot field on the RIGHT panel — never the left schema tree."""
        tree_right = self._schema_tree_right_edge(frame)
        fbox = frame.frame_element().bounding_box()
        min_page_x = (fbox["x"] + tree_right + 24) if fbox else tree_right + 24

        candidates: list[tuple[float, float, object]] = []
        loc = frame.get_by_text(label, exact=True)
        for idx in range(loc.count()):
            node = loc.nth(idx)
            try:
                if not node.is_visible(timeout=300):
                    continue
            except PlaywrightTimeoutError:
                continue
            if self._is_inside_schema_tree(node):
                continue
            box = node.bounding_box()
            if not box or box["x"] < min_page_x:
                continue
            candidates.append((box["x"], box["y"], node))

        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def _wait_for_pivot_measure_row(
        self, frame, label: str, timeout_ms: int = 15_000
    ):
        loc_holder: list = []

        def found() -> bool:
            loc = self._resolve_pivot_field_locator(frame, label)
            if loc is not None:
                loc_holder.clear()
                loc_holder.append(loc)
                return True
            return False

        if self._poll_until(frame, found, timeout_ms=timeout_ms):
            return loc_holder[0]
        return None

    def _find_measure_drop_target(self, frame, *, anchor_measure: str | None = None):
        """
        First measure → 'Drop a Measure Here'.
        After that the placeholder is replaced — drop onto the existing measure row.
        """
        placeholder = frame.get_by_text(self.measure_drop_zone_text, exact=False)
        try:
            if placeholder.count() > 0 and placeholder.first.is_visible(timeout=500):
                return placeholder.first, self.measure_drop_zone_text
        except PlaywrightTimeoutError:
            pass

        anchor = anchor_measure or self.units_item
        anchor_loc = self._wait_for_pivot_measure_row(frame, anchor, timeout_ms=5_000)
        if anchor_loc is None:
            anchor_loc = self._resolve_pivot_field_locator(frame, anchor)
        if anchor_loc is not None:
            logger.info(
                "Measure placeholder gone — dropping onto existing %r in pivot",
                anchor,
            )
            return anchor_loc, anchor

        raise PlaywrightTimeoutError(
            f"Neither {self.measure_drop_zone_text!r} nor pivot anchor "
            f"{anchor!r} found for measure drop"
        )

    def _pivot_field_dropped(
        self, frame, item_label: str, drop_zone_text: str
    ) -> bool:
        """Return True when item_label appears in a pivot drop zone (not tree)."""
        return frame.evaluate(
            """
            ([itemLabel, dropZoneText]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => tree && tree.contains(el);

                for (const el of document.body.querySelectorAll(
                    'span, a, td, div, li, label'
                )) {
                    const text = trim(el.textContent);
                    if (!text || text.length > 80) continue;
                    if (text !== itemLabel) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (inTree(el)) continue;
                    if (el.children.length > 4) continue;
                    return true;
                }

                for (const el of document.body.querySelectorAll('*')) {
                    const text = trim(el.textContent);
                    if (!text.includes(dropZoneText)) continue;
                    const host = el.closest('tr, td, table, div') || el.parentElement;
                    if (!host) continue;
                    const hostText = trim(host.textContent);
                    if (hostText.includes(itemLabel)) return true;
                }
                return false;
            }
            """,
            [item_label, drop_zone_text],
        )

    def _verify_column_dimension_dropped(self, frame, item_label: str) -> None:
        """Confirm a dimension landed in the column zone (outside the schema tree)."""
        if not self._poll_until(
            frame,
            lambda: self._pivot_field_dropped(
                frame, item_label, self.column_drop_zone_text
            ),
            timeout_ms=12_000,
        ):
            raise PlaywrightTimeoutError(
                f"Column drop verification failed — {item_label!r} not found "
                f"in column zone (check pivot designer, not schema tree)"
            )
        logger.info("Verified %r in column zone", item_label)

    def _verify_measure_dropped(self, frame, item_label: str) -> None:
        """Confirm a measure landed in the measure zone (outside the schema tree)."""
        if not self._poll_until(
            frame,
            lambda: self._pivot_field_dropped(
                frame, item_label, self.measure_drop_zone_text
            ),
            timeout_ms=12_000,
        ):
            raise PlaywrightTimeoutError(
                f"Measure drop verification failed — {item_label!r} not found "
                f"in measure zone"
            )
        logger.info("Verified %r in measure zone", item_label)

    def _scroll_schema_label_into_view(self, frame, label: str) -> None:
        """Scroll the #trvSchema container so that label is centred in the panel.

        Playwright's scroll_into_view_if_needed scrolls the outer browser frame
        rather than the tree's own inner scrollbar.  We drive it from JS so the
        scroll happens inside the trvSchema container even when the target node
        is currently outside the visible clip area (getBoundingClientRect may
        return zero size for clipped-overflow elements, so we do NOT guard on
        visibility here).
        """
        frame.evaluate(
            """
            (label) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]')
                    || document.body;
                // Period expansion can push the tree horizontally — reset so
                // chevrons to the left of labels are reachable again.
                let host = root;
                for (let i = 0; i < 6 && host; i++) {
                    if (host.scrollWidth > host.clientWidth + 2) {
                        host.scrollLeft = 0;
                    }
                    host = host.parentElement;
                }
                for (const el of root.querySelectorAll(
                    'span, a, td, div, label, li, nobr'
                )) {
                    if (trim(el.textContent) !== label) continue;
                    el.scrollIntoView({ block: 'center', inline: 'start' });
                    return;
                }
            }
            """,
            label,
        )

    def _click_schema_tree_chevron(
        self,
        frame,
        label: str,
        *,
        below_label: str | None = None,
        require_below: str | None = None,
        deepest: bool = True,
    ) -> bool:
        """Click expand/collapse chevron for a row inside #trvSchema only."""
        return frame.evaluate(
            """
            ([label, belowLabel, requireBelow, deepest]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const root = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                if (!root) return false;

                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const fire = (el) => {
                    if (!el) return false;
                    el.scrollIntoView({ block: 'center', inline: 'start' });
                    for (const type of ['mousedown', 'mouseup', 'click']) {
                        el.dispatchEvent(new MouseEvent(type, {
                            bubbles: true, cancelable: true, view: window,
                        }));
                    }
                    if (typeof el.click === 'function') el.click();
                    return true;
                };

                let anchorRect = null;
                if (requireBelow) {
                    let maxLeft = -1;
                    for (const el of root.querySelectorAll(
                        'span, a, td, div, label, li, nobr'
                    )) {
                        const text = trim(el.textContent);
                        if (text !== requireBelow) continue;
                        const r = el.getBoundingClientRect();
                        if (!isVisible(el)) continue;
                        if (r.left > maxLeft) {
                            maxLeft = r.left;
                            anchorRect = r;
                        }
                    }
                }

                let belowRect = null;
                if (belowLabel) {
                    let maxLeft = -1;
                    for (const el of root.querySelectorAll(
                        'span, a, td, div, label, li, nobr'
                    )) {
                        const text = trim(el.textContent);
                        if (text !== belowLabel) continue;
                        const r = el.getBoundingClientRect();
                        if (!isVisible(el)) continue;
                        if (r.left > maxLeft) {
                            maxLeft = r.left;
                            belowRect = r;
                        }
                    }
                }

                const hits = [];
                for (const el of root.querySelectorAll(
                    'span, a, td, div, label, li, nobr'
                )) {
                    const text = trim(el.textContent);
                    if (text !== label) continue;
                    const r = el.getBoundingClientRect();
                    if (anchorRect && r.top <= anchorRect.top + 4) continue;
                    if (belowRect) {
                        if (r.top <= belowRect.top + 4) continue;
                        if (r.left < belowRect.left - 8) continue;
                        if (r.left - belowRect.left > 96) continue;
                    }
                    hits.push({ el, left: r.left, r });
                }
                if (!hits.length) return false;
                hits.sort((a, b) =>
                    deepest ? b.left - a.left : a.left - b.left
                );
                const labelEl = hits[0].el;
                const labelRect = hits[0].r;
                labelEl.scrollIntoView({ block: 'center', inline: 'start' });
                const row = labelEl.closest(
                    'tr, li, [class*="rtLI"], [class*="Node"], '
                    + '[class*="node"], [class*="Tree"]'
                ) || labelEl.parentElement;

                const tryContainer = (container) => {
                    if (!container) return false;
                    for (const el of container.querySelectorAll(
                        '.rtPlus, .rtMinus, [class*="rtPlus"], [class*="rtMinus"]'
                    )) {
                        if (fire(el)) return true;
                    }
                    for (const img of container.querySelectorAll('img')) {
                        const r = img.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.left >= labelRect.left) continue;
                        if (fire(img)) return true;
                    }
                    for (const ch of container.querySelectorAll('span, i')) {
                        const t = trim(ch.textContent);
                        if (t === '>' || t === '▶' || t === '›' || t === '▼') {
                            if (fire(ch)) return true;
                        }
                    }
                    return false;
                };

                if (tryContainer(row)) return true;
                let parent = labelEl.parentElement;
                for (let i = 0; i < 4 && parent; i++) {
                    if (tryContainer(parent)) return true;
                    parent = parent.parentElement;
                }
                return false;
            }
            """,
            [label, below_label, require_below, deepest],
        )

    def expand_sales_data_folder(self) -> None:
        """Expand Measures (if needed) then Sales Data via row chevrons."""
        frame = self._designer_frame()

        if self._sales_data_children_visible(frame):
            logger.info("%r already expanded", self.sales_data_folder)
            return

        if not self._is_tree_node_visible(frame, self.sales_data_folder):
            logger.info("Clicking %r chevron…", self.measures_folder)
            # Scroll Measures into view before clicking its chevron so the tree
            # panel is positioned correctly even when Period is still expanded.
            self._scroll_schema_label_into_view(frame, self.measures_folder)
            measures_visible = lambda: self._is_tree_node_visible(
                frame, self.sales_data_folder
            )
            if not self._expand_tree_chevron(
                frame,
                self.measures_folder,
                measures_visible,
                deepest=True,
            ):
                raise PlaywrightTimeoutError(
                    f"Could not expand {self.measures_folder!r} — "
                    f"{self.sales_data_folder!r} not visible"
                )

        if self._sales_data_children_visible(frame):
            logger.info("%r already expanded", self.sales_data_folder)
            return

        # Scroll Sales Data into the visible area of the schema tree panel
        # before attempting the chevron click. When Period is still expanded its
        # many children push Sales Data below the visible scroll area, making all
        # coordinate-based chevron clicks land on the wrong element.
        self._scroll_schema_label_into_view(frame, self.sales_data_folder)

        logger.info(
            "Clicking %r chevron under %r…",
            self.sales_data_folder,
            self.measures_folder,
        )
        sales_visible = lambda: self._sales_data_children_visible(frame)
        if self._expand_tree_chevron(
            frame,
            self.sales_data_folder,
            sales_visible,
            deepest=True,
            below_label=self.measures_folder,
        ):
            return

        hints = self._tree_node_hints(frame, self.units_item)
        raise PlaywrightTimeoutError(
            f"Could not expand {self.sales_data_folder!r} — "
            f"{self.units_item!r}/{self.values_item!r} not visible "
            f"(similar: {hints})"
        )

    def _tree_node_label_box(self, frame, node: dict) -> dict:
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            raise PlaywrightTimeoutError("Could not read designer frame bounds")
        width = max(20, (node["x"] - node["left"]) * 2)
        return {
            "x": fbox["x"] + node["left"],
            "y": fbox["y"] + node["top"],
            "width": width,
            "height": 18,
        }

    def collapse_period_if_expanded(self, frame) -> None:
        """Close Period so its Attributes row is not confused with Market's."""
        try:
            self._collapse_period_if_expanded_inner(frame)
        except PlaywrightError as exc:
            # The designer iframe can be reloaded by a server-side report rebuild
            # triggered by a drag.  When that happens any pending Playwright calls
            # on the old frame throw TargetClosedError.  The collapse is cosmetic
            # cleanup — the next step opens a fresh designer anyway, so log and
            # continue rather than propagating.
            logger.warning(
                "collapse_period_if_expanded skipped — frame closed: %s", exc
            )

    def _collapse_period_if_expanded_inner(self, frame) -> None:
        if not self._period_children_visible(frame):
            return

        logger.info(
            "Closing %r before %r work…",
            self.period_item,
            self.market_dimension,
        )
        # Tree is locked while a query is running — wait for idle first.
        self._wait_for_query_idle(frame, timeout_ms=60_000)
        self._scroll_schema_label_into_view(frame, self.period_item)

        collapsed = lambda: not self._period_children_visible(frame)
        if self._click_schema_tree_chevron(
            frame,
            self.period_item,
            require_below=self.dimensions_folder,
            deepest=False,
        ) and self._poll_until_ui(
            frame, collapsed, idle_timeout_ms=600, busy_timeout_ms=3_000
        ):
            logger.info("%r collapsed", self.period_item)
            return

        period_target = self._dimension_anchor_locator(frame, self.period_item)
        if period_target is None:
            return
        period_target.scroll_into_view_if_needed()
        period_box = period_target.bounding_box()
        if not period_box:
            return

        if self._click_chevron_for_label_box(
            frame,
            period_box,
            self.period_item,
            verify=collapsed,
            idle_timeout_ms=600,
            busy_timeout_ms=3_000,
        ):
            logger.info("%r collapsed", self.period_item)
        else:
            logger.info(
                "%r collapse skipped — continuing anyway",
                self.period_item,
            )

    def collapse_market_if_expanded(self, frame) -> None:
        """Close Market so its Attributes row is not confused with Product's."""
        if not self._dimension_children_visible(frame, self.market_dimension):
            return

        logger.info(
            "Closing %r before %r work…",
            self.market_dimension,
            self.product_dimension,
        )
        market_target = self._dimension_anchor_locator(
            frame, self.market_dimension
        )
        if market_target is None:
            return
        market_target.scroll_into_view_if_needed()
        market_box = market_target.bounding_box()
        if not market_box:
            return

        if self._click_chevron_for_label_box(
            frame,
            market_box,
            self.market_dimension,
            verify=lambda: not self._dimension_children_visible(
                frame, self.market_dimension
            ),
            idle_timeout_ms=600,
            busy_timeout_ms=3_000,
        ):
            logger.info("%r collapsed", self.market_dimension)
        else:
            logger.info(
                "%r collapse skipped — continuing with %r",
                self.market_dimension,
                self.product_dimension,
            )

    def collapse_product_if_expanded(self, frame) -> None:
        """Close Product so its Attributes row is not confused with Geography's."""
        if not self._dimension_children_visible(frame, self.product_dimension):
            return

        logger.info(
            "Closing %r before %r work…",
            self.product_dimension,
            self.geography_dimension,
        )
        product_target = self._dimension_anchor_locator(
            frame, self.product_dimension
        )
        if product_target is None:
            return
        product_target.scroll_into_view_if_needed()
        product_box = product_target.bounding_box()
        if not product_box:
            return

        if self._click_chevron_for_label_box(
            frame,
            product_box,
            self.product_dimension,
            verify=lambda: not self._dimension_children_visible(
                frame, self.product_dimension
            ),
            idle_timeout_ms=600,
            busy_timeout_ms=3_000,
        ):
            logger.info("%r collapsed", self.product_dimension)
        else:
            logger.info(
                "%r collapse skipped — continuing with %r",
                self.product_dimension,
                self.geography_dimension,
            )

    def _hierarchies_children_visible(self, frame) -> bool:
        """Return True when the Hierarchies folder is open (its items are shown)."""
        try:
            return frame.get_by_text(
                self.relative_mat_item, exact=False
            ).first.is_visible(timeout=300)
        except PlaywrightTimeoutError:
            return False

    def collapse_hierarchies_if_expanded(self, frame) -> None:
        """Close the Hierarchies sub-folder under Period to clean up the tree."""
        if not self._hierarchies_children_visible(frame):
            return

        logger.info(
            "Closing %r before next step…",
            self.hierarchies_folder,
        )
        hier_target = self._resolve_tree_label_locator(
            frame,
            self.hierarchies_folder,
            deepest=True,
            below_label=self.period_item,
        )
        if hier_target is None:
            return
        hier_target.scroll_into_view_if_needed()
        hier_box = hier_target.bounding_box()
        if not hier_box:
            return

        if self._click_chevron_for_label_box(
            frame,
            hier_box,
            self.hierarchies_folder,
            verify=lambda: not self._hierarchies_children_visible(frame),
            idle_timeout_ms=600,
            busy_timeout_ms=3_000,
        ):
            logger.info("%r collapsed", self.hierarchies_folder)
        else:
            logger.info(
                "%r collapse skipped — continuing anyway",
                self.hierarchies_folder,
            )

    def _is_dimension_expanded(self, frame, dimension: str) -> bool:
        return self._dimension_children_visible(frame, dimension)

    def expand_market_folder(self) -> None:
        """Expand Dimensions (if needed) then Market via row chevrons."""
        frame = self._designer_frame()
        self.collapse_period_if_expanded(frame)

        if self._dimension_children_visible(frame, self.market_dimension):
            logger.info("%r already expanded", self.market_dimension)
            return

        if not self._is_tree_node_visible(frame, self.market_dimension):
            logger.info("Clicking %r chevron…", self.dimensions_folder)
            dims_target = self._resolve_tree_label_locator(
                frame, self.dimensions_folder, deepest=True
            )
            if dims_target is None:
                raise PlaywrightTimeoutError(
                    f"Could not locate {self.dimensions_folder!r} in schema tree"
                ) from None
            dims_target.scroll_into_view_if_needed()
            dims_box = dims_target.bounding_box()
            if not dims_box:
                raise PlaywrightTimeoutError(
                    f"Could not read bounds for {self.dimensions_folder!r}"
                ) from None

            def _market_row_visible() -> bool:
                return self._is_tree_node_visible(frame, self.market_dimension)

            if not self._click_chevron_for_label_box(
                frame,
                dims_box,
                self.dimensions_folder,
                verify=_market_row_visible,
            ):
                raise PlaywrightTimeoutError(
                    f"Could not expand {self.dimensions_folder!r} — "
                    f"{self.market_dimension!r} not visible"
                )

        logger.info(
            "Clicking %r chevron under %r…",
            self.market_dimension,
            self.dimensions_folder,
        )
        verify = lambda: self._dimension_children_visible(
            frame, self.market_dimension
        )

        if self._expand_tree_row_by_label(
            frame,
            self.market_dimension,
            deepest=False,
            require_below=self.dimensions_folder,
            verify=verify,
        ):
            logger.info("%r expanded", self.market_dimension)
            return

        market_target = self._dimension_anchor_locator(frame, self.market_dimension)
        if market_target is None:
            raise PlaywrightTimeoutError(
                f"Could not locate {self.market_dimension!r} under "
                f"{self.dimensions_folder!r}"
            ) from None
        market_target.scroll_into_view_if_needed()
        market_box = market_target.bounding_box()
        if not market_box:
            raise PlaywrightTimeoutError(
                f"Could not read bounds for {self.market_dimension!r}"
            ) from None

        if not self._click_chevron_for_label_box(
            frame,
            market_box,
            self.market_dimension,
            verify=verify,
        ):
            raise PlaywrightTimeoutError(
                f"Could not expand {self.market_dimension!r} — "
                f"{self.attributes_folder!r}/{self.hierarchies_folder!r} "
                f"not visible"
            )

    def _market_dimension_locator(self, frame):
        return self._resolve_tree_label_locator(
            frame,
            self.market_dimension,
            deepest=False,
            below_label=self.dimensions_folder,
        )

    def _market_attributes_locator(self, frame):
        return self._resolve_tree_label_locator(
            frame,
            self.attributes_folder,
            deepest=False,
            below_label=self.market_dimension,
            grandparent_label=self.dimensions_folder,
        )

    def _market_attribute_item_locator(self, frame):
        return self._resolve_tree_label_locator(
            frame,
            self.market_dimension,
            deepest=False,
            below_label=self.attributes_folder,
            grandparent_label=self.market_dimension,
        )

    def _locate_child_folder_under_dimension(
        self, frame, dimension: str, folder_name: str
    ):
        target = self._resolve_tree_label_locator(
            frame,
            dimension,
            deepest=False,
            below_label=self.dimensions_folder,
        )
        if target is None:
            target = self._resolve_tree_label_locator(
                frame, dimension, deepest=False
            )
        if target is None:
            return None
        p_box = target.bounding_box()
        if not p_box:
            return None

        best = None
        best_y = float("inf")
        loc = frame.get_by_text(folder_name, exact=True)
        for idx in range(loc.count()):
            node = loc.nth(idx)
            try:
                if not node.is_visible(timeout=200):
                    continue
            except PlaywrightTimeoutError:
                continue
            box = node.bounding_box()
            if not box:
                continue
            if box["y"] <= p_box["y"] + 4:
                continue
            if abs(box["x"] - p_box["x"]) > 64:
                continue
            if box["y"] < best_y:
                best_y = box["y"]
                best = node
        return best

    def _locate_market_attribute_item(self, frame):
        attrs = self._locate_child_folder_under_dimension(
            frame, self.market_dimension, self.attributes_folder
        )
        if attrs is None:
            return None
        a_box = attrs.bounding_box()
        if not a_box:
            return None

        best = None
        best_y = float("inf")
        loc = frame.get_by_text(self.market_dimension, exact=True)
        for idx in range(loc.count()):
            node = loc.nth(idx)
            try:
                if not node.is_visible(timeout=200):
                    continue
            except PlaywrightTimeoutError:
                continue
            box = node.bounding_box()
            if not box:
                continue
            if box["y"] <= a_box["y"] + 4:
                continue
            if abs(box["x"] - a_box["x"]) > 64:
                continue
            if box["y"] < best_y:
                best_y = box["y"]
                best = node
        return best

    def _market_attributes_children_visible(self, frame) -> bool:
        """True when Market attribute leaf is visible under Dimensions → Market → Attributes."""
        if self._market_attribute_item_locator(frame) is not None:
            return True
        if self._locate_market_attribute_item(frame) is not None:
            return True
        return False

    def _dimension_attributes_folder_visible(
        self, frame, dimension: str
    ) -> bool:
        """True when the Attributes folder row is visible under a dimension."""
        if self._find_tree_child_under_parent(
            frame,
            dimension,
            self.attributes_folder,
            grandparent_label=self.dimensions_folder,
        ):
            return True
        loc = self._resolve_tree_label_locator(
            frame,
            self.attributes_folder,
            deepest=False,
            below_label=dimension,
            grandparent_label=self.dimensions_folder,
        )
        return loc is not None

    def _expand_dimension_attributes_folder(
        self, frame, dimension: str
    ) -> None:
        """Expand Dimensions → dimension → Attributes (scoped, not Period/Market)."""
        verify_leaf = (
            self._market_attributes_children_visible
            if dimension == self.market_dimension
            else (
                self._product_attributes_children_visible
                if dimension == self.product_dimension
                else (
                    self._geography_attributes_children_visible
                    if dimension == self.geography_dimension
                    else lambda f: self._dimension_attributes_folder_visible(
                        f, dimension
                    )
                )
            )
        )

        if verify_leaf(frame):
            logger.info(
                "%r under %r already expanded",
                self.attributes_folder,
                dimension,
            )
            return

        if dimension == self.market_dimension:
            self.collapse_period_if_expanded(frame)

        if dimension == self.geography_dimension:
            self.collapse_market_if_expanded(frame)

        if dimension == self.market_dimension:
            self.expand_market_folder()
        elif dimension == self.product_dimension:
            self.expand_product_folder()
        elif dimension == self.geography_dimension:
            self.expand_geography_folder()
        else:
            if not self._is_tree_node_visible(frame, self.dimensions_folder):
                self._expand_tree_chevron(
                    frame,
                    self.dimensions_folder,
                    lambda: self._is_tree_node_visible(frame, dimension),
                    deepest=True,
                )
            if not self._dimension_children_visible(frame, dimension):
                target = self._dimension_anchor_locator(frame, dimension)
                if target is None:
                    raise PlaywrightTimeoutError(
                        f"Could not locate {dimension!r} under "
                        f"{self.dimensions_folder!r}"
                    ) from None
                target.scroll_into_view_if_needed()
                box = target.bounding_box()
                if not box:
                    raise PlaywrightTimeoutError(
                        f"Could not read bounds for {dimension!r}"
                    ) from None
                self._click_chevron_for_label_box(
                    frame,
                    box,
                    dimension,
                    verify=lambda: self._dimension_children_visible(
                        frame, dimension
                    ),
                )

        dim_loc = self._dimension_anchor_locator(frame, dimension)
        if dim_loc is not None:
            dim_loc.scroll_into_view_if_needed()

        if verify_leaf(frame):
            logger.info(
                "%r under %r already expanded",
                self.attributes_folder,
                dimension,
            )
            return

        if not self._dimension_attributes_folder_visible(frame, dimension):
            raise PlaywrightTimeoutError(
                f"Could not find {self.attributes_folder!r} row under "
                f"{dimension!r} (expand {dimension!r} first)"
            ) from None

        logger.info(
            "Clicking %r chevron under %r (under %r)…",
            self.attributes_folder,
            dimension,
            self.dimensions_folder,
        )
        verify = lambda: verify_leaf(frame)

        attrs_loc = self._resolve_tree_label_locator(
            frame,
            self.attributes_folder,
            deepest=False,
            below_label=dimension,
            grandparent_label=self.dimensions_folder,
        )
        if attrs_loc is None:
            attrs_loc = self._locate_child_folder_under_dimension(
                frame, dimension, self.attributes_folder
            )
        if attrs_loc is not None:
            attrs_loc.scroll_into_view_if_needed()
            attrs_box = attrs_loc.bounding_box()
            if attrs_box and self._click_chevron_for_label_box(
                frame,
                attrs_box,
                self.attributes_folder,
                verify=verify,
            ):
                logger.info(
                    "%r expanded under %r",
                    self.attributes_folder,
                    dimension,
                )
                return

        if self._expand_tree_chevron(
            frame,
            self.attributes_folder,
            verify,
            deepest=False,
            below_label=dimension,
            grandparent_label=self.dimensions_folder,
        ):
            logger.info(
                "%r expanded under %r",
                self.attributes_folder,
                dimension,
            )
            return

        attrs_node = self._wait_for_tree_child_under_parent(
            frame,
            dimension,
            self.attributes_folder,
            timeout_ms=8_000,
            grandparent_label=self.dimensions_folder,
        )
        if attrs_node is None:
            attrs_node = self._find_tree_child_under_parent(
                frame,
                dimension,
                self.attributes_folder,
                grandparent_label=self.dimensions_folder,
            )
        if attrs_node is None:
            raise PlaywrightTimeoutError(
                f"Could not locate {self.attributes_folder!r} under "
                f"{dimension!r} in schema tree"
            ) from None

        label_box = self._tree_node_label_box(frame, attrs_node)
        if not self._click_chevron_for_label_box(
            frame,
            label_box,
            self.attributes_folder,
            verify=verify,
        ):
            raise PlaywrightTimeoutError(
                f"Could not expand {self.attributes_folder!r} under "
                f"{dimension!r} — attribute rows not visible"
            )
        logger.info(
            "%r expanded under %r",
            self.attributes_folder,
            dimension,
        )

    def expand_market_attributes_folder(self) -> None:
        """Close Period → expand Market under Dimensions → expand its Attributes folder."""
        frame = self._designer_frame()
        self._expand_dimension_attributes_folder(frame, self.market_dimension)

    def _product_dimension_locator(self, frame):
        return self._resolve_tree_label_locator(
            frame,
            self.product_dimension,
            deepest=False,
            below_label=self.dimensions_folder,
        )

    def _product_attributes_locator(self, frame):
        return self._resolve_tree_label_locator(
            frame,
            self.attributes_folder,
            deepest=False,
            below_label=self.product_dimension,
            grandparent_label=self.dimensions_folder,
        )

    def _product_attribute_item_locator(self, frame):
        """Product attribute leaf under Dimensions → Product → Attributes."""
        return self._resolve_tree_label_locator(
            frame,
            self.product_dimension,
            deepest=False,
            below_label=self.attributes_folder,
            grandparent_label=self.product_dimension,
        )

    def _resolve_attribute_leaf_locator(
        self, frame, label: str, dimension: str
    ):
        """Exact attribute leaf under dimension → Attributes (e.g. Pack not Pack Form)."""
        anchor = self._parent_anchor_box(
            frame,
            self.attributes_folder,
            grandparent_label=dimension,
        )
        if anchor is None:
            return None

        pattern = re.compile(f"^{re.escape(label)}$")
        loc = frame.get_by_text(pattern)
        best = None
        best_y: float | None = None
        for idx in range(loc.count()):
            node = loc.nth(idx)
            try:
                if not node.is_visible(timeout=200):
                    continue
                own_text = node.evaluate(
                    """
                    (el) => (el.innerText || el.textContent || '')
                        .replace(/\\s+/g, ' ').trim()
                    """
                )
                if own_text != label:
                    continue
                box = node.bounding_box()
                if not box:
                    continue
                if box["y"] <= anchor["y"] + 4:
                    continue
                if abs(box["x"] - anchor["x"]) > 64:
                    continue
                if best_y is None or box["y"] < best_y:
                    best_y = box["y"]
                    best = node
            except PlaywrightTimeoutError:
                continue
        return best

    def _pack_attribute_item_locator(self, frame):
        """Pack attribute leaf under Dimensions → Product → Attributes."""
        loc = self._resolve_attribute_leaf_locator(
            frame, self.pack_attribute, self.product_dimension
        )
        if loc is not None:
            return loc
        return self._resolve_tree_label_locator(
            frame,
            self.pack_attribute,
            deepest=False,
            below_label=self.attributes_folder,
            grandparent_label=self.product_dimension,
        )

    def _brick_attribute_item_locator(self, frame):
        """Brick attribute leaf under Dimensions → Geography → Attributes."""
        return self._resolve_tree_label_locator(
            frame,
            self.brick_attribute,
            deepest=False,
            below_label=self.attributes_folder,
            grandparent_label=self.geography_dimension,
        )

    def _product_attributes_children_visible(self, frame) -> bool:
        return self._product_attribute_item_locator(frame) is not None

    def _geography_attributes_children_visible(self, frame) -> bool:
        return self._brick_attribute_item_locator(frame) is not None

    def _dimensions_branch_visible(self, frame) -> bool:
        return (
            self._is_tree_node_in_schema(frame, self.dimensions_folder)
            or self._is_tree_node_in_schema(frame, self.geography_dimension)
        )

    def _expand_cubes_branch_after_filter(self, frame) -> None:
        """
        After a pivot filter the Cubes row stays visible but collapsed.
        Expand Cubes → cube node (e.g. Pakistan DDD) until Dimensions show.
        """
        if self._dimensions_branch_visible(frame):
            return

        if not self._is_tree_node_in_schema(frame, self.cubes_folder):
            logger.info(
                "%r not in schema tree — cannot expand branch yet",
                self.cubes_folder,
            )
            return

        def _dims_or_geo_visible() -> bool:
            return self._dimensions_branch_visible(frame)

        logger.info(
            "After filter — expanding %r chevron to reveal Dimensions / Geography…",
            self.cubes_folder,
        )

        tree = frame.locator("#trvSchema, [id*='trvSchema']").first
        try:
            tree.scroll_into_view_if_needed(timeout=5_000)
        except PlaywrightTimeoutError:
            pass

        cubes_loc = tree.get_by_text(self.cubes_folder, exact=True).first
        try:
            cubes_loc.wait_for(state="visible", timeout=3_000)
        except PlaywrightTimeoutError:
            return

        if not _dims_or_geo_visible():
            cubes_box = cubes_loc.bounding_box()
            if cubes_box and self._click_chevron_for_label_box(
                frame,
                cubes_box,
                self.cubes_folder,
                verify=_dims_or_geo_visible,
            ):
                logger.info("%r expanded in schema tree", self.cubes_folder)
            elif self._expand_tree_chevron(
                frame,
                self.cubes_folder,
                verify=_dims_or_geo_visible,
                deepest=False,
            ):
                logger.info("%r expanded via tree chevron", self.cubes_folder)

        if _dims_or_geo_visible():
            return

        nodes = self._collect_visible_tree_nodes(frame)
        cubes = self._pick_tree_node(
            nodes, self.cubes_folder, shallowest=True
        )
        if not cubes:
            return

        skip_labels = {
            self.cubes_folder,
            self.dimensions_folder,
            self.measures_folder,
        }
        children = [
            node
            for node in nodes
            if node["top"] > cubes["top"] + 2
            and node["left"] > cubes["left"]
            and node["text"] not in skip_labels
        ]
        if not children:
            return

        cube_node = min(children, key=lambda node: (node["top"], node["left"]))
        cube_label = cube_node["text"]
        logger.info("Expanding cube node %r under %r…", cube_label, self.cubes_folder)

        if self._expand_tree_chevron(
            frame,
            cube_label,
            verify=_dims_or_geo_visible,
            deepest=False,
            require_below=self.cubes_folder,
        ):
            logger.info("Expanded cube node %r", cube_label)
            return

        child_loc = tree.get_by_text(cube_label, exact=True).first
        try:
            child_loc.wait_for(state="visible", timeout=2_000)
            child_box = child_loc.bounding_box()
        except PlaywrightTimeoutError:
            child_box = None

        if child_box and self._click_chevron_for_label_box(
            frame,
            child_box,
            cube_label,
            verify=_dims_or_geo_visible,
        ):
            logger.info("Expanded cube node %r via chevron", cube_label)

    def ensure_schema_tree_after_filter(self) -> None:
        """
        After Step 13 the left cube tree collapses — expand Cubes first;
        only re-click Add when the Cubes row is missing entirely.
        """
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)

        if self._geography_attributes_children_visible(frame):
            logger.info(
                "%r already visible in schema tree with %r expanded",
                self.geography_dimension,
                self.attributes_folder,
            )
            return

        if self._is_tree_node_in_schema(frame, self.geography_dimension):
            logger.info("%r already visible in schema tree", self.geography_dimension)
            if self._dimension_attributes_folder_visible(
                frame, self.geography_dimension
            ):
                return

        if (
            self._is_tree_node_in_schema(frame, self.dimensions_folder)
            and self._dimension_attributes_folder_visible(
                frame, self.geography_dimension
            )
        ):
            logger.info("%r already visible in schema tree", self.dimensions_folder)
            return

        self._expand_cubes_branch_after_filter(frame)

        if self._dimensions_branch_visible(frame):
            logger.info("Schema tree ready — Dimensions branch visible")
            return

        if not self._is_tree_node_in_schema(frame, self.cubes_folder):
            logger.info(
                "Cubes row missing after filter — re-clicking Add to reload schema…"
            )
            add_button = frame.locator(
                "#btnOk, input[type='button'][value='Add']"
            ).first
            add_button.wait_for(state="visible", timeout=15_000)
            if self._is_add_enabled(frame):
                if self._target_catalog:
                    self._force_correct_catalog(frame, self._target_catalog)
                self._wait_for_overlay_cleared(frame)
                self._dismiss_stale_db_popup_aggressively(frame)
                self._click_add_button(frame)
                self._dismiss_stale_db_popup_aggressively(frame)
                self._wait_after_add_click(frame, timeout_ms=120_000)
                self._dismiss_stale_db_popup_aggressively(frame)
            self._expand_cubes_branch_after_filter(frame)

        if not self._dimensions_branch_visible(frame):
            logger.info(
                "Expanding Cubes branch via period navigation fallback…"
            )
            self._expand_period_branch(frame, timeout_ms=45_000)

        if not self._poll_until(
            frame,
            lambda: self._dimensions_branch_visible(frame),
            timeout_ms=8_000,
        ):
            hints = self._tree_node_hints(frame, self.geography_dimension)
            raise PlaywrightTimeoutError(
                f"Schema tree still missing {self.dimensions_folder!r} / "
                f"{self.geography_dimension!r} after expanding Cubes "
                f"(similar: {hints})"
            )
        logger.info("Schema tree ready — Dimensions branch visible")

    def reclick_add_to_restore_schema_tree(self) -> None:
        """Backward-compatible alias — prefer expanding Cubes after filter."""
        self.ensure_schema_tree_after_filter()

    def expand_geography_folder(self) -> None:
        """Expand Dimensions (if needed) then Geography via row chevrons."""
        frame = self._designer_frame()
        self.ensure_schema_tree_after_filter()

        if self._geography_attributes_children_visible(frame):
            logger.info(
                "%r %r already expanded",
                self.geography_dimension,
                self.attributes_folder,
            )
            return

        if not self._is_tree_node_in_schema(frame, self.geography_dimension):
            if not self._is_tree_node_in_schema(frame, self.dimensions_folder):
                logger.info("Clicking %r chevron…", self.dimensions_folder)

                def _geography_row_visible() -> bool:
                    return self._is_tree_node_in_schema(
                        frame, self.geography_dimension
                    )

                if self._expand_tree_row_by_label(
                    frame,
                    self.dimensions_folder,
                    deepest=True,
                    verify=_geography_row_visible,
                ):
                    logger.info("%r expanded", self.dimensions_folder)
                elif not self._expand_tree_chevron(
                    frame,
                    self.dimensions_folder,
                    _geography_row_visible,
                    deepest=True,
                ):
                    raise PlaywrightTimeoutError(
                        f"Could not expand {self.dimensions_folder!r} — "
                        f"{self.geography_dimension!r} not visible"
                    )
            else:
                logger.info(
                    "%r already visible — expanding %r directly",
                    self.dimensions_folder,
                    self.geography_dimension,
                )

        logger.info(
            "Clicking %r chevron under %r…",
            self.geography_dimension,
            self.dimensions_folder,
        )
        verify = lambda: self._dimension_attributes_folder_visible(
            frame, self.geography_dimension
        )

        if self._expand_tree_row_by_label(
            frame,
            self.geography_dimension,
            deepest=False,
            require_below=self.dimensions_folder,
            verify=verify,
        ):
            logger.info("%r expanded", self.geography_dimension)
            return

        geography_target = self._dimension_anchor_locator(
            frame, self.geography_dimension
        )
        if geography_target is None:
            raise PlaywrightTimeoutError(
                f"Could not locate {self.geography_dimension!r} under "
                f"{self.dimensions_folder!r}"
            ) from None
        self._try_scroll_locator(geography_target)
        geography_box = geography_target.bounding_box()
        if not geography_box:
            raise PlaywrightTimeoutError(
                f"Could not read bounds for {self.geography_dimension!r}"
            ) from None

        if self._click_chevron_for_label_box(
            frame,
            geography_box,
            self.geography_dimension,
            verify=verify,
        ):
            logger.info("%r expanded via chevron", self.geography_dimension)
            return

        if self._expand_tree_chevron(
            frame,
            self.geography_dimension,
            verify,
            deepest=False,
            below_label=self.dimensions_folder,
        ):
            logger.info("%r expanded via chevron fallback", self.geography_dimension)
            return

        if self._poll_until(frame, verify, timeout_ms=6_000):
            logger.info("%r expanded after poll", self.geography_dimension)
            return

        raise PlaywrightTimeoutError(
            f"Could not expand {self.geography_dimension!r} — "
            f"{self.attributes_folder!r} not visible"
        )

    def expand_product_folder(self) -> None:
        """Expand Dimensions (if needed) then Product via row chevrons."""
        frame = self._designer_frame()

        if self._dimension_children_visible(frame, self.product_dimension):
            logger.info("%r already expanded", self.product_dimension)
            return

        self.collapse_market_if_expanded(frame)

        if not self._is_tree_node_in_schema(frame, self.product_dimension):
            logger.info("Clicking %r chevron…", self.dimensions_folder)
            dims_target = self._resolve_tree_label_locator(
                frame, self.dimensions_folder, deepest=True
            )
            if dims_target is None:
                raise PlaywrightTimeoutError(
                    f"Could not locate {self.dimensions_folder!r} in schema tree"
                ) from None
            dims_target.scroll_into_view_if_needed()
            dims_box = dims_target.bounding_box()
            if not dims_box:
                raise PlaywrightTimeoutError(
                    f"Could not read bounds for {self.dimensions_folder!r}"
                ) from None

            def _product_row_visible() -> bool:
                return self._is_tree_node_in_schema(
                    frame, self.product_dimension
                )

            if not self._click_chevron_for_label_box(
                frame,
                dims_box,
                self.dimensions_folder,
                verify=_product_row_visible,
            ):
                raise PlaywrightTimeoutError(
                    f"Could not expand {self.dimensions_folder!r} — "
                    f"{self.product_dimension!r} not visible"
                )

        logger.info(
            "Clicking %r chevron under %r…",
            self.product_dimension,
            self.dimensions_folder,
        )
        verify = lambda: self._dimension_children_visible(
            frame, self.product_dimension
        )

        if self._expand_tree_row_by_label(
            frame,
            self.product_dimension,
            deepest=False,
            require_below=self.dimensions_folder,
            verify=verify,
        ):
            logger.info("%r expanded", self.product_dimension)
            return

        product_target = self._dimension_anchor_locator(
            frame, self.product_dimension
        )
        if product_target is None:
            raise PlaywrightTimeoutError(
                f"Could not locate {self.product_dimension!r} under "
                f"{self.dimensions_folder!r}"
            ) from None
        product_target.scroll_into_view_if_needed()
        product_box = product_target.bounding_box()
        if not product_box:
            raise PlaywrightTimeoutError(
                f"Could not read bounds for {self.product_dimension!r}"
            ) from None

        if self._click_chevron_for_label_box(
            frame,
            product_box,
            self.product_dimension,
            verify=verify,
        ):
            logger.info("%r expanded", self.product_dimension)
            return

        if self._expand_tree_chevron(
            frame,
            self.product_dimension,
            verify,
            deepest=False,
            below_label=self.dimensions_folder,
        ):
            logger.info("%r expanded via chevron fallback", self.product_dimension)
            return

        if self._poll_until(frame, verify, timeout_ms=6_000):
            logger.info("%r expanded after poll", self.product_dimension)
            return

        raise PlaywrightTimeoutError(
            f"Could not expand {self.product_dimension!r} — "
            f"{self.attributes_folder!r}/{self.hierarchies_folder!r} "
            f"not visible"
        )

    def expand_product_attributes_folder(self) -> None:
        """Expand Product under Dimensions → expand its Attributes folder."""
        frame = self._designer_frame()
        self._expand_dimension_attributes_folder(frame, self.product_dimension)

    def expand_geography_attributes_folder(self) -> None:
        """Expand Geography under Dimensions → expand its Attributes folder."""
        frame = self._designer_frame()
        self._expand_dimension_attributes_folder(frame, self.geography_dimension)

    def _filter_placeholder_visible(self, frame) -> bool:
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                for (const el of document.querySelectorAll(
                    'span, td, div, nobr, label'
                )) {
                    if (trim(el.textContent) !== 'Drop a Filter Condition Here') {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
                return false;
            }
            """
        )

    def _find_market_filter_field_coords(self, frame) -> dict | None:
        """Return clickable coords for Market in the filter/page zone (IMG / NOBR)."""
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const isMarketFilter = (text) => {
                    if (!text || text.length > 80) return false;
                    if (text.includes('Drop a Filter')) return false;
                    const t = trim(text);
                    return (
                        t === 'Market (None)'
                        || t.startsWith('Market (')
                        || /^\\*?\\s*Market\\b/.test(t)
                    );
                };

                let row = null;
                for (const el of document.body.querySelectorAll('nobr, span, td, div')) {
                    const text = trim(el.textContent);
                    if (!isMarketFilter(text)) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 400) continue;
                    row = el.closest('tr, td, div, li') || el.parentElement;
                    break;
                }
                if (!row) return null;

                let chevronX = null;
                let chevronY = null;

                for (const img of row.querySelectorAll(
                    'img.member-icon, img[onclick*="Context"], img'
                )) {
                    const r = img.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 400) continue;
                    chevronX = r.x + r.width / 2;
                    chevronY = r.y + r.height / 2;
                    break;
                }

                if (chevronX === null) return null;

                return {
                    text: trim(row.textContent).slice(0, 60),
                    chevronX,
                    chevronY,
                    // Backward-compatible aliases — always the chevron, never the name.
                    noneX: chevronX,
                    noneY: chevronY,
                    arrowX: chevronX,
                    arrowY: chevronY,
                };
            }
            """
        )

    def _wait_for_market_filter_field(
        self, frame, timeout_ms: int = 30_000
    ) -> dict:
        coords = None

        def ready() -> bool:
            nonlocal coords
            coords = self._find_market_filter_field_coords(frame)
            return coords is not None

        if not self._poll_until(frame, ready, timeout_ms=timeout_ms):
            raise PlaywrightTimeoutError(
                f"{self.pivot_market_field!r} did not appear in the pivot "
                f"filter row after drop"
            )
        logger.info("Market filter field visible: %r", coords.get("text"))
        return coords

    def _filter_field_dropped(self, frame, item_label: str) -> bool:
        if self._filter_placeholder_visible(frame):
            return False
        if self._find_market_filter_field_coords(frame) is not None:
            return True
        return frame.evaluate(
            """
            ([itemLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label'
                )) {
                    const text = trim(el.textContent);
                    if (!text || text.length > 60) continue;
                    if (inTree(el)) continue;
                    if (text.includes('Drop a Filter Condition Here')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.top > 400 || r.width <= 0) continue;
                    if (
                        text === itemLabel
                        || text.startsWith(itemLabel + ' (')
                        || text.startsWith(itemLabel + '(')
                    ) {
                        return true;
                    }
                }
                return false;
            }
            """,
            [item_label],
        )

    def _verify_filter_field_dropped(self, frame, item_label: str) -> None:
        if not self._poll_until(
            frame,
            lambda: self._filter_field_dropped(frame, item_label),
            timeout_ms=12_000,
        ):
            raise PlaywrightTimeoutError(
                f"Filter drop verification failed — {item_label!r} not found "
                f"in filter zone"
            )
        logger.info("Verified %r in filter zone", item_label)

    def _row_placeholder_visible(self, frame) -> bool:
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                for (const el of document.querySelectorAll(
                    'span, td, div, nobr, label'
                )) {
                    if (trim(el.textContent) !== 'Drop a Row Dimension Here') {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
                return false;
            }
            """
        )

    def _row_field_dropped(self, frame, item_label: str) -> bool:
        if self._row_placeholder_visible(frame):
            return False
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([itemLabel, treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label'
                )) {
                    const text = trim(el.textContent);
                    if (!text || text.length > 60) continue;
                    if (inTree(el)) continue;
                    if (text.includes('Drop a Row Dimension Here')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.top > 450 || r.width <= 0) continue;
                    if (r.left < minLeft) continue;
                    if (
                        text === itemLabel
                        || text.startsWith(itemLabel + ' (')
                        || text.startsWith(itemLabel + '(')
                    ) {
                        return true;
                    }
                }
                return false;
            }
            """,
            [item_label, tree_right],
        )

    def _verify_row_field_dropped(self, frame, item_label: str) -> None:
        if not self._poll_until(
            frame,
            lambda: self._row_field_dropped(frame, item_label),
            timeout_ms=12_000,
        ):
            raise PlaywrightTimeoutError(
                f"Row drop verification failed — {item_label!r} not found "
                f"in row zone"
            )
        logger.info("Verified %r in row zone", item_label)

    def _try_add_tree_item_to_filter_via_menu(
        self, frame, source_loc, *, verify
    ) -> bool:
        """Right-click a schema-tree item and choose an Add-to-Filter menu action."""
        fbox = frame.frame_element().bounding_box()
        start = self._locator_page_point(source_loc, x_ratio=0.42, y_ratio=0.5)
        if not fbox or not start:
            return False

        for attempt in range(1, 4):
            self._clear_open_popups(frame)
            if not self._dispatch_contextmenu_at(frame, start[0], start[1]):
                frame.page.mouse.click(start[0], start[1], button="right")
            frame.wait_for_timeout(350)
            for label in (
                "Add to Report Filter",
                "Add to Filters",
                "Add to Filter",
                "Filters",
            ):
                if self._try_click_menu_item(label):
                    logger.info(
                        "Added tree item to filter via context menu %r", label
                    )
                    if self._poll_until_ui(frame, verify, idle_timeout_ms=5_000):
                        return True
            frame.wait_for_timeout(200)
        return False

    def open_market_attributes_and_drag_market_to_filter(
        self, market: str | None = None
    ) -> None:
        """Market → Attributes → drag Market attribute to filter zone."""
        frame = self._designer_frame()
        guard = getattr(self.page, "_rpaGuard", None)
        if guard:
            guard.disable()
            guard.refresh()
        try:
            self._wait_for_query_idle(frame)
            self.expand_market_attributes_folder()

            market_attr = self._market_attribute_item_locator(frame)
            if market_attr is None:
                market_attr = self._locate_market_attribute_item(frame)
            if market_attr is None:
                hints = self._tree_node_hints(frame, self.market_dimension)
                raise PlaywrightTimeoutError(
                    f"Could not find {self.market_dimension!r} under "
                    f"{self.attributes_folder!r} (similar: {hints})"
                ) from None

            verify = lambda: self._filter_field_dropped(
                frame, self.market_dimension
            )
            market_attr.scroll_into_view_if_needed()
            self._settle(frame, 300)

            if self._try_add_tree_item_to_filter_via_menu(
                frame, market_attr, verify=verify
            ):
                logger.info(
                    "Placed %r in filter zone via context menu",
                    self.market_dimension,
                )
            else:
                drop_loc = frame.get_by_text(
                    self.filter_drop_zone_text, exact=False
                ).last
                drop_loc.wait_for(state="visible", timeout=15_000)
                drop_loc.scroll_into_view_if_needed()

                self._drag_tree_label_to_drop_zone(
                    frame,
                    self.market_dimension,
                    self.filter_drop_zone_text,
                    deepest=False,
                    below_label=self.attributes_folder,
                    grandparent_label=self.market_dimension,
                    drop_loc=drop_loc,
                    source_loc=market_attr,
                    verify=verify,
                )

                if not verify():
                    drop_coords = self._find_right_pivot_filter_drop_coords(frame)
                    market_box = self._find_schema_tree_market_attribute_box(frame)
                    if drop_coords is not None:
                        page = frame.page
                        fbox = frame.frame_element().bounding_box()
                        if market_box and fbox:
                            start = (
                                fbox["x"] + market_box["x"],
                                fbox["y"] + market_box["y"],
                            )
                        else:
                            start = self._locator_page_point(
                                market_attr, x_ratio=0.18, y_ratio=0.5
                            )
                        if start:
                            base_x, base_y = (
                                drop_coords["page_x"],
                                drop_coords["page_y"],
                            )
                            offsets = (
                                (0, 0),
                                (16, 0),
                                (-16, 0),
                                (0, -12),
                                (0, 12),
                                (24, -18),
                            )
                            for idx, (dx, dy) in enumerate(offsets, start=1):
                                end = (base_x + dx, base_y + dy)
                                logger.info(
                                    "Retry %r filter drop at (%.0f, %.0f)…",
                                    self.market_dimension,
                                    end[0],
                                    end[1],
                                )
                                self._human_mouse_drag(page, start, end)
                                if self._poll_until_ui(
                                    frame, verify, idle_timeout_ms=4_000
                                ):
                                    break
                                if idx == len(offsets):
                                    raise PlaywrightTimeoutError(
                                        f"Drop verification failed for "
                                        f"{self.market_dimension!r} in filter zone"
                                    )

            self._verify_filter_field_dropped(frame, self.market_dimension)
            self._wait_for_market_filter_field(frame)

            if market:
                self.set_market_filter(market)
        finally:
            if guard:
                guard.enable()
                guard.refresh()

    def open_hierarchies_and_drag_relative_mat_to_columns(self) -> None:
        """Period → Hierarchies chevron → drag Relative MAT to columns."""
        frame = self._designer_frame()
        if not self._period_children_visible(frame):
            self._ensure_period_children_visible(frame)

        logger.info(
            "Clicking %r chevron under %r…",
            self.hierarchies_folder,
            self.period_item,
        )
        try:
            frame.get_by_text(
                self.relative_mat_item, exact=False
            ).first.wait_for(state="visible", timeout=2_000)
        except PlaywrightTimeoutError:
            hier_target = self._resolve_tree_label_locator(
                frame,
                self.hierarchies_folder,
                deepest=True,
                below_label=self.period_item,
            )
            if hier_target is None:
                raise PlaywrightTimeoutError(
                    f"Could not locate {self.hierarchies_folder!r} under "
                    f"{self.period_item!r}"
                ) from None
            hier_target.scroll_into_view_if_needed()
            hier_box = hier_target.bounding_box()
            if not hier_box:
                raise PlaywrightTimeoutError(
                    f"Could not read bounds for {self.hierarchies_folder!r}"
                ) from None

            def _relative_mat_visible() -> bool:
                try:
                    frame.get_by_text(
                        self.relative_mat_item, exact=False
                    ).first.wait_for(state="visible", timeout=500)
                    return True
                except PlaywrightTimeoutError:
                    return False

            if not self._click_chevron_for_label_box(
                frame,
                hier_box,
                self.hierarchies_folder,
                verify=_relative_mat_visible,
            ):
                hints = self._tree_node_hints(frame, self.relative_mat_item)
                raise PlaywrightTimeoutError(
                    f"{self.relative_mat_item!r} did not load under "
                    f"{self.hierarchies_folder!r} (similar: {hints})"
                )

        logger.info("Dragging %r to column area…", self.relative_mat_item)
        self._drag_tree_label_to_drop_zone(
            frame,
            self.relative_mat_item,
            self.column_drop_zone_text,
            deepest=True,
            below_label=self.hierarchies_folder,
        )

        # Collapse Period (and its Hierarchies child with it) so the tree is
        # clean for the next step.
        self.collapse_period_if_expanded(frame)

    def open_sales_data_and_drag_units_values_to_measures(self) -> None:
        """Sales Data → drag Units to measures, then Values onto the Units row."""
        frame = self._designer_frame()
        self.expand_sales_data_folder()

        drop_loc, drop_label = self._find_measure_drop_target(frame)
        units_source = self._sales_data_item_locator(frame, self.units_item)
        self._drag_tree_label_to_drop_zone(
            frame,
            self.units_item,
            drop_label,
            drop_loc=drop_loc,
            deepest=False,
            below_label=self.sales_data_folder,
            grandparent_label=self.measures_folder,
            source_loc=units_source,
            verify=lambda: self._pivot_field_dropped(
                frame, self.units_item, self.measure_drop_zone_text
            ),
        )
        self._verify_measure_dropped(frame, self.units_item)
        self._wait_for_query_idle(frame)

        self._scroll_schema_label_into_view(frame, self.values_item)
        values_source = self._sales_data_item_locator(frame, self.values_item)
        values_drop_loc, values_drop_label = self._find_measure_drop_target(
            frame, anchor_measure=self.units_item
        )
        self._drag_tree_label_to_drop_zone(
            frame,
            self.values_item,
            values_drop_label,
            drop_loc=values_drop_loc,
            deepest=False,
            below_label=self.sales_data_folder,
            grandparent_label=self.measures_folder,
            source_loc=values_source,
            verify=lambda: (
                self._values_on_units_verified(frame)
                or self._pivot_field_dropped(
                    frame, self.values_item, self.measure_drop_zone_text
                )
            ),
        )
        if self._values_on_units_verified(frame):
            logger.info(
                "Verified %r nested on pivot %r row",
                self.values_item,
                self.units_item,
            )
        else:
            self._verify_measure_dropped(frame, self.values_item)
            logger.info(
                "Verified %r in measure zone (Values dropped beside Units)",
                self.values_item,
            )

    def _find_right_pivot_row_drop_coords(self, frame) -> dict | None:
        """Click/drop point on 'Drop a Row Dimension Here' in the RIGHT pivot panel."""
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight, dropText]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== dropText && !text.includes(dropText)) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    const score = r.left * 2 + r.top;
                    if (!best || score > best.score) {
                        best = {
                            x: r.x + r.width * 0.55,
                            y: r.y + r.height / 2,
                            text: dropText,
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [tree_right, self.row_drop_zone_text],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _find_schema_tree_market_attribute_box(self, frame) -> dict | None:
        """Drag start point for Market leaf under Dimensions → Market → Attributes."""
        return frame.evaluate(
            """
            () => {
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                if (!tree) return null;
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();

                const visible = [];
                for (const el of tree.querySelectorAll(
                    'span, td, div, a, nobr, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (!text || text.length > 40) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    visible.push({
                        text,
                        x: r.x,
                        y: r.y,
                        w: r.width,
                        h: r.height,
                        left: r.left,
                    });
                }

                const dims = visible.filter((v) => v.text === 'Dimensions');
                if (!dims.length) return null;
                const dimY = dims.sort((a, b) => a.y - b.y)[0].y;

                const marketDims = visible.filter(
                    (v) => v.text === 'Market' && v.y > dimY + 4
                );
                if (!marketDims.length) return null;
                const marketDim = marketDims.sort((a, b) => a.y - b.y)[0];

                const attrs = visible.filter(
                    (v) =>
                        v.text === 'Attributes'
                        && v.y > marketDim.y + 4
                        && Math.abs(v.left - marketDim.left) < 80
                );
                if (!attrs.length) return null;
                const attrRow = attrs.sort((a, b) => a.y - b.y)[0];

                const leaves = visible.filter(
                    (v) =>
                        v.text === 'Market'
                        && v.y > attrRow.y + 4
                        && Math.abs(v.left - attrRow.left) < 80
                );
                if (!leaves.length) return null;
                const leaf = leaves.sort((a, b) => a.y - b.y)[0];

                return {
                    text: 'Market',
                    x: leaf.x + Math.min(22, leaf.w * 0.35),
                    y: leaf.y + leaf.h / 2,
                };
            }
            """
        )

    def _find_right_pivot_filter_drop_coords(self, frame) -> dict | None:
        """Click/drop point on 'Drop a Filter Condition Here' in the RIGHT pivot panel."""
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight, dropText]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== dropText && !text.includes(dropText)) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    if (r.top > 320) continue;
                    const score = r.left * 3 - r.top * 2;
                    if (!best || score > best.score) {
                        best = {
                            x: r.x + r.width * 0.55,
                            y: r.y + r.height / 2,
                            text: dropText,
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [tree_right, self.filter_drop_zone_text],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _drag_tree_item_to_pivot_coords(
        self, frame, source_loc, coords: dict, *, verify
    ) -> None:
        """Drag a schema-tree leaf onto explicit RIGHT-side pivot coordinates."""
        if source_loc is None:
            raise PlaywrightTimeoutError("Drag source locator is missing") from None

        source_loc.scroll_into_view_if_needed()
        self._settle(frame)

        page = frame.page
        end = (coords["page_x"], coords["page_y"])
        for attempt in range(1, 6):
            logger.info(
                "Dragging → right-side pivot at (%.0f, %.0f) (attempt %d)…",
                end[0],
                end[1],
                attempt,
            )
            start = self._locator_page_point(
                source_loc, x_ratio=0.32, y_ratio=0.5
            )
            if not start:
                raise PlaywrightTimeoutError(
                    "Could not read drag start coordinates"
                ) from None
            self._human_mouse_drag(page, start, end)

            if self._poll_until_ui(frame, verify, idle_timeout_ms=3_000):
                logger.info(
                    "Dropped on right-side pivot %r", coords.get("text")
                )
                return

            if attempt >= 5:
                raise PlaywrightTimeoutError(
                    f"Drop verification failed after {attempt} attempts"
                )
            self._settle(frame, 200)

    def open_product_attributes_and_drag_product_to_row(self) -> None:
        """Dimensions → Product → Attributes → drag Product to row zone."""
        frame = self._designer_frame()
        self.expand_product_attributes_folder()

        product_attr = self._product_attribute_item_locator(frame)
        if product_attr is None:
            hints = self._tree_node_hints(frame, self.product_dimension)
            raise PlaywrightTimeoutError(
                f"Could not find {self.product_dimension!r} under "
                f"{self.attributes_folder!r} (similar: {hints})"
            ) from None

        drop_coords = self._find_right_pivot_row_drop_coords(frame)
        if drop_coords is None:
            drop_loc = frame.get_by_text(
                self.row_drop_zone_text, exact=False
            ).first
            drop_loc.wait_for(state="visible", timeout=15_000)
            drop_loc.scroll_into_view_if_needed()
            logger.info(
                "Dragging %r → %r (locator fallback)…",
                self.product_dimension,
                self.row_drop_zone_text,
            )
            self._drag_tree_label_to_drop_zone(
                frame,
                self.product_dimension,
                self.row_drop_zone_text,
                deepest=False,
                below_label=self.attributes_folder,
                grandparent_label=self.product_dimension,
                drop_loc=drop_loc,
                source_loc=product_attr,
                verify=lambda: self._row_field_dropped(
                    frame, self.product_dimension
                ),
            )
        else:
            logger.info(
                "Dragging %r from left tree → right-side %r at (%.0f, %.0f)…",
                self.product_dimension,
                self.row_drop_zone_text,
                drop_coords["page_x"],
                drop_coords["page_y"],
            )
            self._drag_tree_item_to_pivot_coords(
                frame,
                product_attr,
                drop_coords,
                verify=lambda: self._row_field_dropped(
                    frame, self.product_dimension
                ),
            )
        self._verify_row_field_dropped(frame, self.product_dimension)
        if drop_coords and self._pivot_page_coords_valid(frame, drop_coords):
            self._last_product_row_coords = {
                "page_x": drop_coords["page_x"],
                "page_y": drop_coords["page_y"],
                "text": self.product_dimension,
            }
        else:
            header = self._find_pivot_row_product_header_coords(frame)
            if header:
                self._last_product_row_coords = header

    def _find_pivot_row_field_coords(
        self, frame, field_label: str
    ) -> dict | None:
        """Drop point on a row-dimension header in the RIGHT pivot (Product/Pack/Brick)."""
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight, fieldLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = (treeRight > 0 ? treeRight : 280) + 80;
                const rowHeaderMinY = 90;
                const rowHeaderMaxY = 420;

                const matchesLabel = (text) => (
                    text === fieldLabel
                    || text.startsWith(fieldLabel + ' (')
                    || text.startsWith(fieldLabel + '(')
                );

                let best = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (!text || text.length > 60) continue;
                    if (!matchesLabel(text)) continue;
                    if (text.includes('Drop a Row Dimension Here')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top < rowHeaderMinY || r.top > rowHeaderMaxY) continue;
                    if (r.left < minLeft) continue;
                    const score = -r.top * 1000 + r.left;
                    if (!best || score > best.score) {
                        best = {
                            x: Math.max(r.x + r.width * 0.82, minLeft + 80),
                            y: r.y + r.height / 2,
                            text,
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [tree_right, field_label],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _find_row_field_header_loose(
        self, frame, field_label: str
    ) -> dict | None:
        """Lenient row-header finder — wide y-band, picks the top-most match.

        Used when a row dimension (e.g. an outermost Brick) sits outside the
        strict 200-300px header band assumed by ``_find_pivot_row_field_coords``.
        """
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([treeRight, fieldLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = (treeRight > 0 ? treeRight : 280) + 60;

                const matchesLabel = (text) => (
                    text === fieldLabel
                    || text.startsWith(fieldLabel + ' (')
                    || text.startsWith(fieldLabel + '(')
                );

                let best = null;
                for (const td of document.querySelectorAll('td[area="rows"]')) {
                    if (inTree(td)) continue;
                    const span = td.querySelector('span[axis="r"], nobr span');
                    if (!span) continue;
                    const text = trim(span.textContent);
                    if (!matchesLabel(text)) continue;
                    const r = td.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top < 95 || r.top > 180) continue;
                    if (r.left < minLeft) continue;
                    const sr = span.getBoundingClientRect();
                    const score = -r.top * 1000 - r.left;
                    if (!best || score > best.score) {
                        best = {
                            x: sr.x + Math.min(sr.width * 0.32, sr.width - 14),
                            y: sr.y + sr.height / 2,
                            text,
                            score,
                        };
                    }
                }
                if (best) return best;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (!text || text.length > 60) continue;
                    if (!matchesLabel(text)) continue;
                    if (text.includes('Drop a Row Dimension Here')) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.width > 240) continue;
                    if (r.top < 95 || r.top > 180) continue;
                    if (r.left < minLeft) continue;
                    // Prefer the top-most, left-most header (the row label cell).
                    const score = -r.top * 1000 - r.left;
                    if (!best || score > best.score) {
                        // Drop firmly INSIDE the right grid on this header row —
                        // push x well past the tree edge so the drop never lands
                        // back on the schema tree.
                        const dropX = Math.max(
                            r.x + r.width * 0.82, minLeft + 80
                        );
                        best = {
                            x: dropX,
                            y: r.y + r.height / 2,
                            text,
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [tree_right, field_label],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _resolve_row_field_coords(
        self, frame, field_label: str, *, cached: dict | None = None
    ) -> dict | None:
        """Best-effort drop point for a row header: strict → lenient → cached."""
        coords = self._find_pivot_row_field_coords(frame, field_label)
        if coords and self._pivot_page_coords_valid(frame, coords):
            return coords
        coords = self._find_row_field_header_loose(frame, field_label)
        if coords and self._pivot_page_coords_valid(frame, coords):
            return coords
        if cached and self._pivot_page_coords_valid(frame, cached):
            return cached
        return None

    def _find_pivot_row_product_coords(self, frame) -> dict | None:
        """Drop point on the Product row in the RIGHT pivot row zone."""
        for candidate in (
            self._last_product_row_coords,
            self._find_pivot_row_product_header_coords(frame),
            self._find_row_field_header_loose(frame, self.product_dimension),
            self._find_pivot_row_field_coords(frame, self.product_dimension),
        ):
            if candidate and self._pivot_page_coords_valid(frame, candidate):
                return candidate
        return None

    def _product_on_pack_verified(self, frame) -> bool:
        """True when a 'Product' row-dimension header sits beside Brick/Pack.

        The pivot row headers are laid out horizontally (e.g. ``Pack ▾ Product ▾
        Brick ▾``), so we anchor on the Brick header (always present once the row
        is built) and confirm a sibling 'Product' header at the same vertical
        band, outside the left schema tree.
        """
        tree_right = self._schema_tree_right_edge(frame)
        return bool(
            frame.evaluate(
                """
            ([treeRight, brickLabel, packLabel, productLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;
                const isHeader = (text, label) =>
                    text === label
                    || text.startsWith(label + ' (')
                    || text.startsWith(label + '(');

                // Anchor row band on a known header (Brick, else Pack).
                let anchorTop = null;
                for (const anchor of [brickLabel, packLabel]) {
                    for (const el of document.body.querySelectorAll(
                        'span, td, div, nobr, a, label, li'
                    )) {
                        const text = trim(el.textContent);
                        if (!isHeader(text, anchor)) continue;
                        if (inTree(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        if (r.top < 90 || r.top > 320) continue;
                        if (r.width > 240) continue;
                        if (r.left < minLeft) continue;
                        if (anchorTop === null || r.top < anchorTop) {
                            anchorTop = r.top;
                        }
                    }
                    if (anchorTop !== null) break;
                }
                if (anchorTop === null) return false;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (!isHeader(text, productLabel)) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.width > 240) continue;
                    if (r.left < minLeft) continue;
                    if (r.top >= anchorTop - 24 && r.top <= anchorTop + 24) {
                        return true;
                    }
                }
                return false;
            }
            """,
                [
                    tree_right,
                    self.brick_attribute,
                    self.pack_attribute,
                    self.product_dimension,
                ],
            )
        )

    def _pack_nested_in_schema_tree(self, frame) -> bool:
        """True when Pack was wrongly nested under Product in the left tree."""
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                if (!tree) return false;

                let productRow = null;
                for (const el of tree.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Product' && !text.startsWith('Product -')) continue;
                    productRow = el.closest('tr, li, div') || el.parentElement;
                    break;
                }
                if (!productRow) return false;

                for (const el of productRow.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    if (trim(el.textContent) !== 'Pack') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    return true;
                }
                return false;
            }
            """
        )

    def _pack_nested_on_product_in_pivot(self, frame) -> bool:
        """True when Pack was dropped onto the Product row header in the pivot."""
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([treeRight, productLabel, packLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let productHost = null;
                let productTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== productLabel && !text.startsWith(productLabel + ' (')) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 360) continue;
                    if (r.width > 220) continue;
                    if (r.left < minLeft) continue;
                    if (productTop === null || r.top < productTop) {
                        productHost = el.closest('tr, td, table, div, li') || el.parentElement;
                        productTop = r.top;
                    }
                }
                if (!productHost) return false;

                for (const el of productHost.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    if (inTree(el)) continue;
                    const text = trim(el.textContent);
                    if (text !== packLabel && !text.startsWith(packLabel + ' (')) {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    return true;
                }

                const hostText = trim(productHost.textContent);
                return hostText.includes(productLabel) && hostText.includes(packLabel);
            }
            """,
            [tree_right, self.product_dimension, self.pack_attribute],
        )

    def _pack_nested_on_brick_in_pivot(self, frame) -> bool:
        """True when Pack was dropped onto the Brick row header in the pivot."""
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([treeRight, brickLabel, packLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let brickHost = null;
                let brickTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== brickLabel && !text.startsWith(brickLabel + ' (')) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 380) continue;
                    if (r.width > 220) continue;
                    if (r.left < minLeft) continue;
                    if (brickTop === null || r.top < brickTop) {
                        brickHost = el.closest('tr, td, table, div, li') || el.parentElement;
                        brickTop = r.top;
                    }
                }
                if (!brickHost) return false;

                for (const el of brickHost.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    if (inTree(el)) continue;
                    const text = trim(el.textContent);
                    if (text !== packLabel && !text.startsWith(packLabel + ' (')) {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    return true;
                }

                const hostText = trim(brickHost.textContent);
                return hostText.includes(brickLabel) && hostText.includes(packLabel);
            }
            """,
            [tree_right, self.brick_attribute, self.pack_attribute],
        )

    def _pack_on_brick_verified(self, frame) -> bool:
        if self._pack_nested_in_schema_tree(frame):
            return False
        if self._pack_nested_on_brick_in_pivot(frame):
            return True
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([treeRight, brickLabel, packLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let brickTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== brickLabel && !text.startsWith(brickLabel + ' (')) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 380) continue;
                    if (r.width > 220) continue;
                    if (r.left < minLeft) continue;
                    if (brickTop === null || r.top < brickTop) {
                        brickTop = r.top;
                    }
                }
                if (brickTop === null) return false;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== packLabel && !text.startsWith(packLabel + ' (')) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    if (r.top >= brickTop - 6 && r.top <= brickTop + 96) return true;
                }
                return false;
            }
            """,
            [tree_right, self.brick_attribute, self.pack_attribute],
        )

    def _pack_on_product_verified(self, frame) -> bool:
        if self._pack_nested_in_schema_tree(frame):
            return False
        if self._pack_nested_on_product_in_pivot(frame):
            return True
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([treeRight, productLabel, packLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let productTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== productLabel && !text.startsWith(productLabel + ' (')) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 360) continue;
                    if (r.width > 220) continue;
                    if (r.left < minLeft) continue;
                    if (productTop === null || r.top < productTop) {
                        productTop = r.top;
                    }
                }
                if (productTop === null) return false;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== packLabel && !text.startsWith(packLabel + ' (')) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    if (r.top >= productTop - 6 && r.top <= productTop + 96) return true;
                }
                return false;
            }
            """,
            [tree_right, self.product_dimension, self.pack_attribute],
        )

    def _find_pivot_row_pack_coords(self, frame) -> dict | None:
        """Drop point on the Pack row in the RIGHT pivot row zone."""
        for candidate in (
            self._last_pack_row_coords,
            self._find_pivot_row_pack_on_brick_header_coords(frame),
            self._find_pivot_row_pack_header_coords(frame),
        ):
            if candidate and self._pivot_page_coords_valid(frame, candidate):
                return candidate
        return None

    def _brick_nested_in_schema_tree(self, frame) -> bool:
        """True when Brick was wrongly nested under Geography in the left tree."""
        return frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                if (!tree) return false;

                let geographyRow = null;
                for (const el of tree.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (text !== 'Geography' && !text.startsWith('Geography -')) {
                        continue;
                    }
                    geographyRow = el.closest('tr, li, div') || el.parentElement;
                    break;
                }
                if (!geographyRow) return false;

                for (const el of geographyRow.querySelectorAll(
                    'span, a, td, div, label, li'
                )) {
                    if (trim(el.textContent) !== 'Brick') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    return true;
                }
                return false;
            }
            """
        )

    def _brick_nested_on_pack_in_pivot(self, frame) -> bool:
        """True when Brick was dropped onto the Pack row header in the pivot."""
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([treeRight, packLabel, brickLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let packHost = null;
                let packTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (
                        text !== packLabel
                        && !text.startsWith(packLabel + ' (')
                        && !text.startsWith(packLabel + '(')
                    ) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 350) continue;
                    if (r.left < minLeft) continue;
                    if (packTop === null || r.top < packTop) {
                        packHost = el.closest('tr, td, table, div, li') || el.parentElement;
                        packTop = r.top;
                    }
                }
                if (!packHost) return false;

                for (const el of packHost.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    if (inTree(el)) continue;
                    const text = trim(el.textContent);
                    if (
                        text !== brickLabel
                        && !text.startsWith(brickLabel + ' (')
                        && !text.startsWith(brickLabel + '(')
                    ) {
                        continue;
                    }
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    return true;
                }

                const hostText = trim(packHost.textContent);
                return hostText.includes(packLabel) && hostText.includes(brickLabel);
            }
            """,
            [tree_right, self.pack_attribute, self.brick_attribute],
        )

    def _brick_on_pack_verified(self, frame) -> bool:
        if self._brick_nested_in_schema_tree(frame):
            return False
        if self._brick_nested_on_pack_in_pivot(frame):
            return True
        tree_right = self._schema_tree_right_edge(frame)
        return frame.evaluate(
            """
            ([treeRight, packLabel, brickLabel]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                let packTop = null;
                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (
                        text !== packLabel
                        && !text.startsWith(packLabel + ' (')
                        && !text.startsWith(packLabel + '(')
                    ) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 350) continue;
                    if (r.left < minLeft) continue;
                    if (packTop === null || r.top < packTop) {
                        packTop = r.top;
                    }
                }
                if (packTop === null) return false;

                for (const el of document.body.querySelectorAll(
                    'span, td, div, nobr, a, label, li'
                )) {
                    const text = trim(el.textContent);
                    if (
                        text !== brickLabel
                        && !text.startsWith(brickLabel + ' (')
                        && !text.startsWith(brickLabel + '(')
                    ) {
                        continue;
                    }
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.left < minLeft) continue;
                    if (r.top >= packTop - 6 && r.top <= packTop + 96) return true;
                }
                return false;
            }
            """,
            [tree_right, self.pack_attribute, self.brick_attribute],
        )

    def _drag_attribute_item_to_pivot_coords(
        self,
        frame,
        item: str,
        coords: dict,
        *,
        source_loc,
        verify,
        refresh_coords=None,
    ) -> None:
        """Drag a dimension attribute leaf onto explicit RIGHT-side pivot coords."""
        if source_loc is None:
            hints = self._tree_node_hints(frame, item)
            raise PlaywrightTimeoutError(
                f"Could not find {item!r} under {self.attributes_folder!r} "
                f"(similar: {hints})"
            ) from None

        refresh = refresh_coords or self._find_pivot_row_product_coords
        source_loc.scroll_into_view_if_needed()
        self._settle(frame)

        page = frame.page
        base_x, base_y = coords["page_x"], coords["page_y"]
        end = (base_x, base_y)
        for attempt in range(1, 9):
            logger.info(
                "Dragging %r → right-side pivot at (%.0f, %.0f) (attempt %d)…",
                item,
                end[0],
                end[1],
                attempt,
            )
            start = self._locator_page_point(
                source_loc, x_ratio=0.32, y_ratio=0.5
            )
            if not start:
                raise PlaywrightTimeoutError(
                    f"Could not read drag start for {item!r}"
                ) from None
            self._human_mouse_drag(page, start, end)

            if self._poll_until_ui(frame, verify, idle_timeout_ms=4_000):
                logger.info(
                    "Dropped %r on right-side pivot %r row",
                    item,
                    coords.get("text"),
                )
                return

            if item == self.pack_attribute and self._pack_nested_in_schema_tree(frame):
                logger.info(
                    "%r landed in left schema tree — retrying right-side pivot row",
                    item,
                )
            elif item == self.brick_attribute and self._brick_nested_in_schema_tree(frame):
                logger.info(
                    "%r landed in left schema tree — retrying right-side Pack row",
                    item,
                )

            if attempt >= 8:
                self._dump_open_menu_state(frame, f"drop_fail_{item}")
                raise PlaywrightTimeoutError(
                    f"Drop verification failed for {item!r} after {attempt} attempts"
                )
            refreshed = refresh(frame)
            if refreshed:
                end = (refreshed["page_x"], refreshed["page_y"])
            else:
                end = (base_x + attempt * 20, base_y)
            self._settle(frame, 200)

    def open_product_attributes_and_drag_pack_to_brick_row(self) -> None:
        """Drag Pack from Product → Attributes onto the right-side Brick header."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self.expand_product_attributes_folder()

        def _pack_on_brick_or_row(f) -> bool:
            if self._pack_on_brick_verified(f):
                return True
            return self._row_field_dropped(f, self.pack_attribute)

        def _resolve_brick_target():
            # Drop Pack just RIGHT of the (only) Brick row header so it nests
            # one level deeper — deterministic Brick(outer) → Pack(inner).
            insert = self._rightmost_row_field_drop_point(frame)
            if self._pivot_drop_y_sane(frame, insert):
                return insert
            # Fresh JS finders next (apply the sane y-band filter to reject
            # toolbar/tab-strip noise), then the proven cached coordinate.
            for candidate in (
                self._find_pivot_row_brick_header_coords(frame),
                self._find_pivot_row_brick_coords(frame),
            ):
                if self._pivot_drop_coords_sane(frame, candidate):
                    return candidate
            if self._pivot_drop_y_sane(frame, self._last_brick_row_coords):
                return self._last_brick_row_coords
            return None

        brick_coords = None
        for _ in range(16):
            self._wait_for_query_idle(frame)
            brick_coords = _resolve_brick_target()
            if brick_coords is not None:
                break
            frame.wait_for_timeout(500)
        if brick_coords is None:
            raise PlaywrightTimeoutError(
                f"Could not find pivot {self.brick_attribute!r} header on the right "
                f"for {self.pack_attribute!r} drop"
            ) from None

        pack_attr = self._pack_attribute_item_locator(frame)
        if pack_attr is None:
            hints = self._tree_node_hints(frame, self.pack_attribute)
            raise PlaywrightTimeoutError(
                f"Could not find {self.pack_attribute!r} under "
                f"{self.attributes_folder!r} (similar: {hints})"
            ) from None

        logger.info(
            "Dragging %r from left tree onto right-side pivot %r at (%.0f, %.0f)…",
            self.pack_attribute,
            self.brick_attribute,
            brick_coords["page_x"],
            brick_coords["page_y"],
        )

        def _refresh_brick_drop_coords(f):
            insert = self._rightmost_row_field_drop_point(f)
            if self._pivot_drop_y_sane(f, insert):
                return insert
            header = self._find_pivot_row_brick_header_coords(f)
            if header:
                return header
            return self._last_brick_row_coords or brick_coords

        self._drag_attribute_item_to_pivot_coords(
            frame,
            self.pack_attribute,
            brick_coords,
            source_loc=pack_attr,
            verify=lambda: _pack_on_brick_or_row(frame),
            refresh_coords=_refresh_brick_drop_coords,
        )
        if not self._poll_until(
            frame,
            lambda: _pack_on_brick_or_row(frame),
            timeout_ms=15_000,
        ):
            raise PlaywrightTimeoutError(
                f"Could not drop {self.pack_attribute!r} onto pivot "
                f"{self.brick_attribute!r} row"
            )
        logger.info(
            "Verified %r nested on pivot %r row",
            self.pack_attribute,
            self.brick_attribute,
        )
        cached_pack = self._find_pivot_row_pack_on_brick_header_coords(frame)
        if self._pivot_drop_coords_sane(frame, cached_pack):
            self._last_pack_row_coords = cached_pack
        else:
            self._last_pack_row_coords = brick_coords

    def open_product_attributes_and_drag_product_to_pack_row(self) -> None:
        """Drag Product from Product → Attributes onto the right-side Pack header."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self.expand_product_attributes_folder()

        def _product_on_pack_or_row(f) -> bool:
            return self._product_on_pack_verified(f)

        # Prefer the innermost existing row header. The Brick header coords that
        # accepted the Pack drop are the most reliable target — dropping there
        # nests Product as the deepest row dimension. The pivot may still be
        # rendering right after the Pack drop, so poll the finders until a sane
        # target appears.
        def _resolve_drop_target():
            # Drop Product just RIGHT of the innermost (Pack) row header so it
            # nests deepest — deterministic Brick → Pack → Product(inner).
            insert = self._rightmost_row_field_drop_point(frame)
            if self._pivot_drop_y_sane(frame, insert):
                return insert
            for candidate in (
                self._find_pivot_row_pack_on_brick_header_coords(frame),
                self._find_pivot_row_pack_coords(frame),
                self._find_pivot_row_brick_coords(frame),
            ):
                if self._pivot_drop_coords_sane(frame, candidate):
                    return candidate
            for cached in (self._last_pack_row_coords, self._last_brick_row_coords):
                if self._pivot_drop_y_sane(frame, cached):
                    return cached
            return None

        pack_coords = None
        for _ in range(16):
            self._wait_for_query_idle(frame)
            pack_coords = _resolve_drop_target()
            if pack_coords is not None:
                break
            frame.wait_for_timeout(500)
        if pack_coords is None:
            raise PlaywrightTimeoutError(
                f"Could not find pivot {self.pack_attribute!r} header on the right "
                f"for {self.product_dimension!r} drop"
            ) from None

        product_attr = self._product_attribute_item_locator(frame)
        if product_attr is None:
            hints = self._tree_node_hints(frame, self.product_dimension)
            raise PlaywrightTimeoutError(
                f"Could not find {self.product_dimension!r} under "
                f"{self.attributes_folder!r} (similar: {hints})"
            ) from None

        logger.info(
            "Dragging %r from left tree onto right-side pivot %r at (%.0f, %.0f)…",
            self.product_dimension,
            self.pack_attribute,
            pack_coords["page_x"],
            pack_coords["page_y"],
        )

        def _refresh_pack_drop_coords(f):
            insert = self._rightmost_row_field_drop_point(f)
            if self._pivot_drop_y_sane(f, insert):
                return insert
            header = self._find_pivot_row_pack_on_brick_header_coords(f)
            if self._pivot_drop_coords_sane(f, header):
                return header
            return pack_coords

        self._drag_attribute_item_to_pivot_coords(
            frame,
            self.product_dimension,
            pack_coords,
            source_loc=product_attr,
            verify=lambda: _product_on_pack_or_row(frame),
            refresh_coords=_refresh_pack_drop_coords,
        )
        if not self._poll_until(
            frame,
            lambda: _product_on_pack_or_row(frame),
            timeout_ms=15_000,
        ):
            raise PlaywrightTimeoutError(
                f"Could not drop {self.product_dimension!r} onto pivot "
                f"{self.pack_attribute!r} row"
            )
        logger.info(
            "Verified %r nested on pivot %r row",
            self.product_dimension,
            self.pack_attribute,
        )
        landed = (
            self._find_pivot_row_product_header_coords(frame)
            or self._find_row_field_header_loose(frame, self.product_dimension)
        )
        self._last_product_row_coords = landed or pack_coords

    def open_product_attributes_and_drag_pack_to_product_row(self) -> None:
        """Drag Pack from Product → Attributes onto the right-side Product row."""
        frame = self._designer_frame()
        self.expand_product_attributes_folder()

        pack_attr = self._pack_attribute_item_locator(frame)
        if pack_attr is None:
            hints = self._tree_node_hints(frame, self.pack_attribute)
            raise PlaywrightTimeoutError(
                f"Could not find {self.pack_attribute!r} under "
                f"{self.attributes_folder!r} (similar: {hints})"
            ) from None

        product_coords = self._find_pivot_row_product_coords(frame)
        if product_coords is None or not self._pivot_page_coords_valid(
            frame, product_coords
        ):
            raise PlaywrightTimeoutError(
                f"Could not find pivot {self.product_dimension!r} row on the right "
                f"for {self.pack_attribute!r} drop"
            ) from None

        logger.info(
            "Dragging %r from left tree onto right-side pivot %r at (%.0f, %.0f)…",
            self.pack_attribute,
            self.product_dimension,
            product_coords["page_x"],
            product_coords["page_y"],
        )
        self._drag_attribute_item_to_pivot_coords(
            frame,
            self.pack_attribute,
            product_coords,
            source_loc=pack_attr,
            verify=lambda: self._pack_on_product_verified(frame),
            refresh_coords=self._find_pivot_row_product_header_coords,
        )
        if not self._poll_until(
            frame,
            lambda: self._pack_on_product_verified(frame),
            timeout_ms=12_000,
        ):
            raise PlaywrightTimeoutError(
                f"Could not drop {self.pack_attribute!r} onto pivot "
                f"{self.product_dimension!r} row"
            )
        logger.info(
            "Verified %r nested on pivot %r row",
            self.pack_attribute,
            self.product_dimension,
        )
        cached_pack = self._find_pivot_row_pack_header_coords(frame)
        if cached_pack and self._pivot_page_coords_valid(frame, cached_pack):
            self._last_pack_row_coords = cached_pack
        else:
            self._last_pack_row_coords = None

    def open_product_attributes_and_drag_product_pack_to_rows(self) -> None:
        """Dimensions → Product → Attributes → drag Product then Pack to rows."""
        self.open_product_attributes_and_drag_product_to_row()
        self.open_product_attributes_and_drag_pack_to_product_row()

    def open_geography_attributes_and_drag_brick_to_pack_row(self) -> None:
        """Geography → Attributes → drag Brick beside Pack (not on Product)."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self.expand_geography_attributes_folder()

        brick_attr = self._brick_attribute_item_locator(frame)
        if brick_attr is None:
            hints = self._tree_node_hints(frame, self.brick_attribute)
            raise PlaywrightTimeoutError(
                f"Could not find {self.brick_attribute!r} under "
                f"{self.attributes_folder!r} (similar: {hints})"
            ) from None

        def _resolve_brick_target():
            insert = self._rightmost_row_field_drop_point(frame)
            if self._pivot_drop_coords_sane(frame, insert):
                return insert
            pack_header = self._find_pivot_row_pack_header_coords(frame)
            if pack_header and self._pivot_drop_coords_sane(frame, pack_header):
                return pack_header
            if (
                self._last_pack_row_coords
                and self._pivot_drop_coords_sane(frame, self._last_pack_row_coords)
            ):
                return self._last_pack_row_coords
            return None

        brick_coords = None
        for _ in range(12):
            self._wait_for_query_idle(frame)
            brick_coords = _resolve_brick_target()
            if brick_coords is not None:
                break
            frame.wait_for_timeout(400)
        if brick_coords is None:
            raise PlaywrightTimeoutError(
                f"Could not find pivot {self.pack_attribute!r} insert point "
                f"for {self.brick_attribute!r} — complete Product/Pack first"
            ) from None

        logger.info(
            "Dragging %r onto pivot beside %r at (%.0f, %.0f)…",
            self.brick_attribute,
            self.pack_attribute,
            brick_coords["page_x"],
            brick_coords["page_y"],
        )

        def _refresh_brick_drop_coords(f):
            insert = self._rightmost_row_field_drop_point(f)
            if self._pivot_drop_coords_sane(f, insert):
                return insert
            header = self._find_pivot_row_pack_header_coords(f)
            if header:
                return header
            return self._last_pack_row_coords or brick_coords

        self._drag_attribute_item_to_pivot_coords(
            frame,
            self.brick_attribute,
            brick_coords,
            source_loc=brick_attr,
            verify=lambda: self._brick_on_pack_verified(frame),
            refresh_coords=_refresh_brick_drop_coords,
        )
        if not self._poll_until(
            frame,
            lambda: self._brick_on_pack_verified(frame),
            timeout_ms=12_000,
        ):
            raise PlaywrightTimeoutError(
                f"Could not drop {self.brick_attribute!r} beside "
                f"{self.pack_attribute!r} row"
            )
        logger.info(
            "Verified %r beside %r (under %r)",
            self.brick_attribute,
            self.pack_attribute,
            self.product_dimension,
        )

    def open_geography_attributes_and_drag_brick_to_rows(self) -> None:
        """Backward-compatible alias — drops Brick onto Pack row."""
        self.open_geography_attributes_and_drag_brick_to_pack_row()

    def open_sales_data_and_drag_units_values_to_rows(self) -> None:
        """Backward-compatible alias — measures go to the measure drop zone."""
        self.open_sales_data_and_drag_units_values_to_measures()

    # ------------------------------------------------------------------ #
    # Brick → Pack → Product row ordering (filter applied last)          #
    # ------------------------------------------------------------------ #
    def open_geography_attributes_and_drag_brick_to_row(self) -> None:
        """Geography → Attributes → drag Brick to the empty row zone (outermost)."""
        frame = self._designer_frame()
        self.expand_geography_attributes_folder()

        brick_attr = self._brick_attribute_item_locator(frame)
        if brick_attr is None:
            hints = self._tree_node_hints(frame, self.brick_attribute)
            raise PlaywrightTimeoutError(
                f"Could not find {self.brick_attribute!r} under "
                f"{self.attributes_folder!r} (similar: {hints})"
            ) from None

        drop_coords = self._find_right_pivot_row_drop_coords(frame)
        if drop_coords is None:
            drop_loc = frame.get_by_text(
                self.row_drop_zone_text, exact=False
            ).first
            drop_loc.wait_for(state="visible", timeout=15_000)
            drop_loc.scroll_into_view_if_needed()
            logger.info(
                "Dragging %r → %r (locator fallback)…",
                self.brick_attribute,
                self.row_drop_zone_text,
            )
            self._drag_tree_label_to_drop_zone(
                frame,
                self.brick_attribute,
                self.row_drop_zone_text,
                deepest=False,
                below_label=self.attributes_folder,
                grandparent_label=self.geography_dimension,
                drop_loc=drop_loc,
                source_loc=brick_attr,
                verify=lambda: self._row_field_dropped(
                    frame, self.brick_attribute
                ),
            )
        else:
            logger.info(
                "Dragging %r from left tree → right-side %r at (%.0f, %.0f)…",
                self.brick_attribute,
                self.row_drop_zone_text,
                drop_coords["page_x"],
                drop_coords["page_y"],
            )
            self._drag_tree_item_to_pivot_coords(
                frame,
                brick_attr,
                drop_coords,
                verify=lambda: self._row_field_dropped(
                    frame, self.brick_attribute
                ),
            )
        self._verify_row_field_dropped(frame, self.brick_attribute)
        brick_header = self._find_pivot_row_brick_header_coords(frame)
        self._last_brick_row_coords = brick_header or self._find_row_field_header_loose(
            frame, self.brick_attribute
        )
        if self._last_brick_row_coords:
            logger.info(
                "Brick pivot header at (%.0f, %.0f)",
                self._last_brick_row_coords["page_x"],
                self._last_brick_row_coords["page_y"],
            )

    def _cached_row_coords(self, field_label: str) -> dict | None:
        if field_label == self.brick_attribute:
            return self._last_brick_row_coords
        if field_label == self.pack_attribute:
            return self._last_pack_row_coords
        if field_label == self.product_dimension:
            return self._last_product_row_coords
        return None

    def _drag_attribute_onto_field_row(
        self, item_label: str, target_field: str, source_loc
    ) -> None:
        """Drag a tree attribute leaf onto an existing right-side row header."""
        frame = self._designer_frame()
        cached = self._cached_row_coords(target_field)
        coords = self._resolve_row_field_coords(frame, target_field, cached=cached)
        if coords is None:
            raise PlaywrightTimeoutError(
                f"Could not find pivot {target_field!r} row on the right "
                f"for {item_label!r} drop"
            ) from None
        logger.info(
            "Dragging %r onto right-side pivot %r row at (%.0f, %.0f)…",
            item_label,
            target_field,
            coords["page_x"],
            coords["page_y"],
        )
        self._drag_attribute_item_to_pivot_coords(
            frame,
            item_label,
            coords,
            source_loc=source_loc,
            verify=lambda: self._row_field_dropped(frame, item_label),
            refresh_coords=lambda f: self._resolve_row_field_coords(
                f, target_field, cached=cached
            ),
        )
        self._verify_row_field_dropped(frame, item_label)
        # Cache the freshly added row header for the next nested drop.
        landed = self._resolve_row_field_coords(frame, item_label, cached=coords)
        if item_label == self.pack_attribute:
            self._last_pack_row_coords = landed
        elif item_label == self.product_dimension:
            self._last_product_row_coords = landed

    def open_product_attributes_and_drag_pack_to_field_row(
        self, target_field: str
    ) -> None:
        """Product → Attributes → drag Pack onto the given right-side row header."""
        frame = self._designer_frame()
        self.expand_product_attributes_folder()
        pack_attr = self._pack_attribute_item_locator(frame)
        if pack_attr is None:
            hints = self._tree_node_hints(frame, self.pack_attribute)
            raise PlaywrightTimeoutError(
                f"Could not find {self.pack_attribute!r} under "
                f"{self.attributes_folder!r} (similar: {hints})"
            ) from None
        self._drag_attribute_onto_field_row(
            self.pack_attribute, target_field, pack_attr
        )

    def open_product_attributes_and_drag_product_to_field_row(
        self, target_field: str
    ) -> None:
        """Product → Attributes → drag Product onto the given right-side row header."""
        frame = self._designer_frame()
        self.expand_product_attributes_folder()
        product_attr = self._product_attribute_item_locator(frame)
        if product_attr is None:
            hints = self._tree_node_hints(frame, self.product_dimension)
            raise PlaywrightTimeoutError(
                f"Could not find {self.product_dimension!r} under "
                f"{self.attributes_folder!r} (similar: {hints})"
            ) from None
        self._drag_attribute_onto_field_row(
            self.product_dimension, target_field, product_attr
        )

    # ------------------------------------------------------------------ #
    # Sheet tab operations (right-click tab → Rename / Copy)             #
    # ------------------------------------------------------------------ #
    def _find_sheet_tab_box(self, frame, name: str | None = None) -> dict | None:
        """Page coords of a sheet tab near the analyzer bottom (active tab if name is None)."""
        result = frame.evaluate(
            """
            ([targetName]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const looksLikeSheetTab = (text) => {
                    if (/^Sheet\\d+$/i.test(text)) return true;
                    if (/^[COM]-/.test(text)) return true;
                    // Brick row labels (e.g. "1003 AGRA TAJ COLONY") sit near the
                    // bottom of the pivot grid and must not be treated as tabs.
                    if (/^\\d{3,}\\s/.test(text)) return false;
                    return false;
                };
                const vh = window.innerHeight
                    || document.documentElement.clientHeight;
                const candidates = [];
                for (const el of document.querySelectorAll(
                    'td, div, span, a, li'
                )) {
                    if (!isVisible(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.top < vh - 130) continue;
                    if (r.height > 44 || r.width > 360 || r.width < 8) continue;
                    const text = trim(el.textContent);
                    if (!text || text.length > 60) continue;
                    if (!looksLikeSheetTab(text)) continue;
                    candidates.push({
                        el,
                        text,
                        left: r.left,
                        cx: r.x + r.width / 2,
                        cy: r.y + r.height / 2,
                        cls: (el.className || '') + ' '
                            + (el.parentElement?.className || ''),
                    });
                }
                let chosen = null;
                if (targetName) {
                    const norm = (s) => trim(s).toLowerCase();
                    const want = norm(targetName);
                    chosen = candidates.find((c) => norm(c.text) === want)
                        || candidates.find((c) => norm(c.text).startsWith(want))
                        || candidates.find((c) => want.startsWith(norm(c.text)))
                        || candidates.find((c) => {
                            const t = norm(c.text);
                            const dash = want.indexOf('-');
                            if (dash < 0) return false;
                            return t.startsWith(want.slice(0, dash + 1));
                        });
                } else {
                    chosen = candidates.find((c) =>
                        /active|selected|current|sel/i.test(c.cls));
                    if (!chosen) {
                        chosen = candidates.find((c) =>
                            /^Sheet\\d+$/i.test(c.text));
                    }
                    if (!chosen && candidates.length) {
                        candidates.sort((a, b) => a.left - b.left);
                        chosen = candidates[0];
                    }
                }
                if (!chosen) return null;
                return { x: chosen.cx, y: chosen.cy, text: chosen.text };
            }
            """,
            [name],
        )
        if not result:
            return None
        px, py = self._frame_page_point(frame, result["x"], result["y"])
        return {"page_x": px, "page_y": py, "text": result["text"]}

    def _list_sheet_tab_names(self, frame) -> list[str]:
        """Names of the sheet tabs along the analyzer bottom strip (left→right)."""
        try:
            names = frame.evaluate(
                """
                () => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const looksLikeSheetTab = (text) => {
                        if (/^Sheet\\d+$/i.test(text)) return true;
                        if (/^[COM]-/.test(text)) return true;
                        if (/^\\d{3,}\\s/.test(text)) return false;
                        return false;
                    };
                    const vh = window.innerHeight
                        || document.documentElement.clientHeight;
                    const rows = [];
                    for (const el of document.querySelectorAll(
                        'td, div, span, a, li'
                    )) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.top < vh - 130) continue;
                        if (r.height > 44 || r.width > 360 || r.width < 8) continue;
                        const text = trim(el.textContent);
                        if (!text || text.length > 60) continue;
                        if (!looksLikeSheetTab(text)) continue;
                        if (el.children.length > 1) continue;
                        rows.push({ left: r.left, text });
                    }
                    rows.sort((a, b) => a.left - b.left);
                    const out = [];
                    for (const row of rows) {
                        if (!out.includes(row.text)) out.push(row.text);
                    }
                    return out;
                }
                """
            )
            return names or []
        except Exception:
            return []

    def _resolve_sheet_tab_name(self, frame, preferred_name: str) -> str:
        """Return the visible tab label that best matches *preferred_name*."""
        preferred = " ".join((preferred_name or "").split()).strip()
        if not preferred:
            raise PlaywrightTimeoutError("Sheet tab name must not be empty")

        for _ in range(20):
            tabs = self._list_sheet_tab_names(frame)
            if tabs:
                norm_pref = preferred.lower()
                for tab in tabs:
                    if tab.lower() == norm_pref:
                        return tab
                for tab in tabs:
                    if tab.lower().startswith(norm_pref):
                        return tab
                for tab in tabs:
                    if norm_pref.startswith(tab.lower()):
                        return tab
                prefix = preferred.split("-", 1)[0] + "-"
                for tab in tabs:
                    if tab.upper().startswith(prefix.upper()):
                        return tab
            frame.wait_for_timeout(250)

        tabs = self._list_sheet_tab_names(frame)
        raise PlaywrightTimeoutError(
            f"Could not resolve sheet tab {preferred_name!r} "
            f"(visible tabs: {tabs!r})"
        )

    def _wait_for_sheet_tab_name(
        self, frame, name: str, *, timeout_ms: int = 15_000
    ) -> str:
        """Poll until a sheet tab matching *name* appears; return the visible label."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        last_tabs: list[str] = []
        while time.monotonic() < deadline:
            try:
                return self._resolve_sheet_tab_name(frame, name)
            except PlaywrightTimeoutError:
                last_tabs = self._list_sheet_tab_names(frame)
                frame.wait_for_timeout(250)
        raise PlaywrightTimeoutError(
            f"Sheet tab {name!r} did not appear within {timeout_ms}ms "
            f"(visible tabs: {last_tabs!r})"
        )

    def _activate_sheet_tab(self, frame, name: str) -> None:
        """Left-click a sheet tab so it becomes the active pivot sheet."""
        resolved = self._wait_for_sheet_tab_name(frame, name)
        box = None
        for _ in range(10):
            box = self._find_sheet_tab_box(frame, resolved)
            if box is not None:
                break
            frame.wait_for_timeout(250)
        if box is None:
            tabs = self._list_sheet_tab_names(frame)
            raise PlaywrightTimeoutError(
                f"Could not locate sheet tab {resolved!r} "
                f"(requested {name!r}, visible tabs: {tabs!r})"
            )
        frame.page.mouse.click(box["page_x"], box["page_y"])
        frame.wait_for_timeout(600)
        self._wait_for_query_idle(frame)
        logger.info("Activated sheet tab %r", resolved)

    def _activate_copied_sheet_tab(self, frame, source_sheet: str) -> None:
        """After copy, switch to the new sheet tab (rightmost, not the source)."""
        tabs = self._list_sheet_tab_names(frame)
        if not tabs:
            raise PlaywrightTimeoutError("No sheet tabs visible after copy")
        candidate = tabs[-1]
        if candidate == source_sheet and len(tabs) >= 2:
            for tab in reversed(tabs[:-1]):
                if tab != source_sheet:
                    candidate = tab
                    break
        self._activate_sheet_tab(frame, candidate)

    def _right_click_sheet_tab(self, frame, name: str | None = None) -> dict:
        resolved = name
        if name:
            try:
                resolved = self._resolve_sheet_tab_name(frame, name)
            except PlaywrightTimeoutError:
                resolved = name
        box = None
        for _ in range(8):
            box = self._find_sheet_tab_box(frame, resolved)
            if box is not None:
                break
            frame.wait_for_timeout(250)
        if box is None:
            raise PlaywrightTimeoutError(
                f"Could not locate sheet tab "
                f"{name if name else '(active)'!r} at the analyzer bottom"
            )
        page = frame.page
        # Select the tab first so the right-click context menu targets it.
        page.mouse.click(box["page_x"], box["page_y"])
        frame.wait_for_timeout(200)
        page.mouse.click(box["page_x"], box["page_y"], button="right")
        frame.wait_for_timeout(300)
        if not self._is_menu_item_visible(self.rename_sheet_menu_text, partial=True):
            self._dispatch_contextmenu_at(frame, box["page_x"], box["page_y"])
            frame.wait_for_timeout(300)
        logger.info("Right-clicked sheet tab %r", box["text"])
        return box

    def _fill_active_sheet_name(self, frame, name: str) -> bool:
        for scope in (frame, self.page):
            done = scope.evaluate(
                """
                ([name]) => {
                    const fire = (el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    };
                    const active = document.activeElement;
                    if (active && (active.tagName === 'INPUT'
                        || active.isContentEditable)) {
                        if (active.isContentEditable) {
                            active.textContent = name;
                        } else {
                            active.value = name;
                        }
                        active.focus();
                        fire(active);
                        return true;
                    }
                    for (const el of document.querySelectorAll(
                        'input[type="text"], input:not([type])'
                    )) {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        el.focus();
                        el.value = name;
                        fire(el);
                        return true;
                    }
                    return false;
                }
                """,
                [name],
            )
            if done:
                frame.page.keyboard.press("Enter")
                frame.wait_for_timeout(400)
                return True
        return False

    def _open_sheet_tab_menu_item(
        self, frame, item_label: str, *, current_name: str | None = None
    ) -> None:
        """Right-click a sheet tab and click a context-menu item, with retries."""
        menu_ready = lambda: self._is_menu_item_visible(item_label, partial=True)
        last_exc: Exception | None = None
        for attempt in range(1, 6):
            self._right_click_sheet_tab(frame, current_name)
            if self._poll_until(frame, menu_ready, timeout_ms=4_000):
                try:
                    self._click_menu_item(item_label)
                    return
                except PlaywrightTimeoutError as exc:
                    last_exc = exc
            logger.info(
                "Sheet-tab menu item %r not ready (attempt %d) — retrying",
                item_label,
                attempt,
            )
            self._clear_open_popups(frame)
            frame.wait_for_timeout(600)
        if last_exc is not None:
            raise last_exc
        raise PlaywrightTimeoutError(
            f"Sheet-tab context menu item {item_label!r} did not appear"
        )

    def rename_sheet_tab(
        self, new_name: str, *, current_name: str | None = None
    ) -> None:
        """Right-click sheet tab → Rename → type the new name."""
        frame = self._designer_frame()
        target = (new_name or "").strip()
        if not target:
            raise ValueError("Sheet name must not be empty")
        self._wait_for_query_idle(frame)
        self._open_sheet_tab_menu_item(
            frame, self.rename_sheet_menu_text, current_name=current_name
        )
        frame.wait_for_timeout(400)
        if not self._fill_active_sheet_name(frame, target):
            raise PlaywrightTimeoutError(
                f"Could not set sheet name to {target!r} after Rename"
            )
        frame.wait_for_timeout(600)
        self._wait_for_query_idle(frame)
        actual = self._wait_for_sheet_tab_name(frame, target)
        logger.info("Sheet renamed to %r (visible tab %r)", target, actual)

    def _select_copy_into_new_sheet(self) -> bool:
        """In Move Part / Copy dialog, set 'into sheet' to New Sheet."""
        label = self.copy_into_new_sheet_label
        for scope in self._walk_page_frames():
            try:
                for index in range(scope.locator("select").count()):
                    sel = scope.locator("select").nth(index)
                    try:
                        options = sel.evaluate(
                            "el => [...el.options].map(o => (o.textContent || '').trim())"
                        )
                    except Exception:
                        continue
                    if not any(
                        opt == label or "New Sheet" in opt for opt in options
                    ):
                        continue
                    for alt in (label, "New Sheet"):
                        try:
                            sel.select_option(label=alt, timeout=2_000)
                            logger.info(
                                "Selected %r in 'into sheet' dropdown", alt
                            )
                            return True
                        except PlaywrightTimeoutError:
                            continue

                into_row = scope.locator("tr, div, td").filter(
                    has_text=re.compile(r"into\s*sheet", re.I)
                )
                if into_row.count() > 0:
                    row_sel = into_row.first.locator("select")
                    if row_sel.count() > 0:
                        row_sel.first.select_option(label=label, timeout=2_000)
                        logger.info("Selected %r via into-sheet row", label)
                        return True

                picked = scope.evaluate(
                    """
                    ([label]) => {
                        const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        for (const sel of document.querySelectorAll('select')) {
                            const opts = [...sel.options].map((o) =>
                                trim(o.textContent)
                            );
                            if (!opts.some((o) => o === label || o.includes('New Sheet'))) {
                                continue;
                            }
                            for (const opt of sel.options) {
                                const t = trim(opt.textContent);
                                if (t !== label && !t.includes('New Sheet')) continue;
                                sel.value = opt.value;
                                sel.dispatchEvent(
                                    new Event('change', { bubbles: true })
                                );
                                return true;
                            }
                        }
                        return false;
                    }
                    """,
                    [label],
                )
                if picked:
                    logger.info("Selected %r in 'into sheet' dropdown (JS)", label)
                    return True
            except Exception:
                continue
        return False

    def _confirm_sheet_copy_dialog(self) -> None:
        for title in (self.move_part_dialog_title, self.copy_sheet_dialog_title):
            try:
                self._click_dialog_ok(title)
                return
            except PlaywrightTimeoutError:
                continue
        raise PlaywrightTimeoutError(
            "Could not confirm sheet copy / Move Part dialog"
        )

    def copy_sheet_tab(
        self, *, current_name: str | None = None, into_new_sheet: bool = False
    ) -> str | None:
        """Right-click sheet tab → Copy… → (optional New Sheet) → OK."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        self._clear_open_popups(frame)
        if current_name:
            self._activate_sheet_tab(frame, current_name)
        before = self._list_sheet_tab_names(frame)
        self._open_sheet_tab_menu_item(
            frame, self.copy_sheet_menu_text, current_name=current_name
        )
        frame.wait_for_timeout(600)
        if into_new_sheet:
            if not self._select_copy_into_new_sheet():
                logger.warning(
                    "%r not found in copy dialog — continuing with default",
                    self.copy_into_new_sheet_label,
                )
            else:
                frame.wait_for_timeout(400)
        try:
            self._confirm_sheet_copy_dialog()
        except PlaywrightTimeoutError:
            logger.info("No Copy/Move Part dialog OK — copy may have applied directly")
        frame.wait_for_timeout(1_200)

        new_name = None
        for _ in range(12):
            after = self._list_sheet_tab_names(frame)
            added = [t for t in after if t not in before]
            if added:
                new_name = added[-1]
                break
            if len(after) > len(before) and after:
                new_name = after[-1]
                break
            frame.wait_for_timeout(400)
        if new_name:
            self._activate_sheet_tab(frame, new_name)
        logger.info(
            "Sheet copied from %r — new tab %r",
            current_name or "(active)",
            new_name or "(unknown)",
        )
        return new_name

    # ------------------------------------------------------------------ #
    # Pivot field Dimension menu (Remove / Move Dimension to Row)        #
    # ------------------------------------------------------------------ #
    def _find_pivot_row_dimension_field_header(
        self, frame, dimension: str
    ) -> dict | None:
        """Page coords for a row-dimension field header ``td[area=rows]`` cell."""
        tree_right = self._schema_tree_right_edge(frame)
        hit = frame.evaluate(
            """
            ([dimension, treeRight]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const minLeft = treeRight + 48;

                const matches = (span, label) => {
                    if (!span) return false;
                    const text = trim(span.textContent);
                    const title = span.getAttribute('title') || '';
                    return (
                        text === label
                        || text.startsWith(label + ' (')
                        || title.includes('[' + label + '.')
                        || title.includes('[' + label + ']')
                    );
                };

                let best = null;
                for (const td of document.querySelectorAll('td[area="rows"]')) {
                    if (inTree(td)) continue;
                    const span = td.querySelector('span, nobr span');
                    if (!matches(span, dimension)) continue;
                    const r = td.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    if (r.top < 100 || r.top > 720) continue;
                    if (r.left < minLeft) continue;
                    const sr = span.getBoundingClientRect();
                    const score = r.left;
                    if (!best || score > best.score) {
                        best = {
                            x: sr.x + Math.min(sr.width * 0.32, sr.width - 14),
                            y: sr.y + sr.height / 2,
                            text: trim(span.textContent),
                            score,
                        };
                    }
                }
                return best;
            }
            """,
            [dimension, tree_right],
        )
        if not hit:
            return None
        fbox = frame.frame_element().bounding_box()
        if not fbox:
            return None
        return {
            "page_x": fbox["x"] + hit["x"],
            "page_y": fbox["y"] + hit["y"],
            "text": hit["text"],
        }

    def _scroll_pivot_row_dimension_into_view(self, frame, dimension: str) -> None:
        """Scroll a pivot row-dimension field header into the visible grid band."""
        frame.evaluate(
            """
            ([dimension]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const matches = (span, label) => {
                    if (!span) return false;
                    const text = trim(span.textContent);
                    const title = span.getAttribute('title') || '';
                    return (
                        text === label
                        || text.startsWith(label + ' (')
                        || title.includes('[' + label + '.')
                        || title.includes('[' + label + ']')
                    );
                };
                for (const td of document.querySelectorAll('td[area="rows"]')) {
                    if (inTree(td)) continue;
                    const span = td.querySelector('span[axis="r"], nobr span');
                    if (!matches(span, dimension)) continue;
                    td.scrollIntoView({ block: 'center', inline: 'nearest' });
                    return;
                }
            }
            """,
            [dimension],
        )

    def _hover_pivot_dimension_menu_tab(self, frame) -> None:
        """Hover the Dimension(s) tab on the pivot field chevron menu."""
        page = frame.page
        hovered = frame.evaluate(
            """
            () => {
                const labels = ['Dimension', 'Dimensions'];
                for (const want of labels) {
                    for (const td of document.querySelectorAll(
                        'td[istab="true"], td[captiontext]'
                    )) {
                        const cap = (td.getAttribute('captiontext') || '').trim();
                        const text = (td.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (cap !== want && text !== want) continue;
                        const r = td.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        td.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                        td.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
                        return { x: r.x + r.width / 2, y: r.y + r.height / 2, label: want };
                    }
                }
                return null;
            }
            """
        )
        if hovered:
            fbox = frame.frame_element().bounding_box()
            if fbox:
                page.mouse.move(
                    fbox["x"] + hovered["x"],
                    fbox["y"] + hovered["y"],
                )
                frame.wait_for_timeout(500)
                logger.info(
                    "Hovered %r tab on pivot field menu (captiontext)",
                    hovered.get("label"),
                )
                return

        for tab_label in ("Dimension", "Dimensions"):
            for scope in self._walk_page_frames():
                try:
                    dim_label = scope.get_by_text(tab_label, exact=True)
                    for index in range(dim_label.count()):
                        tab = dim_label.nth(index)
                        try:
                            if not tab.is_visible(timeout=300):
                                continue
                        except PlaywrightTimeoutError:
                            continue
                        box = tab.bounding_box()
                        if not box:
                            continue
                        page.mouse.move(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        frame.wait_for_timeout(400)
                        logger.info(
                            "Hovered %r tab on pivot field menu", tab_label
                        )
                        return
                except Exception:
                    continue

    def _dispatch_row_dimension_field_contextmenu(
        self, frame, dimension: str
    ) -> bool:
        """Fire contextmenu on the row-dimension field label (not the chevron)."""
        return bool(
            frame.evaluate(
                """
                ([dimension]) => {
                    const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const isVis = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const tree = document.getElementById('trvSchema')
                        || document.querySelector('[id*="trvSchema"]');
                    const inTree = (el) => !!(tree && tree.contains(el));

                    const matches = (span, label) => {
                        if (!span) return false;
                        const text = trim(span.textContent);
                        const title = span.getAttribute('title') || '';
                        return (
                            text === label
                            || text.startsWith(label + ' (')
                            || title.includes('[' + label + '.')
                            || title.includes('[' + label + ']')
                        );
                    };

                    let bestTd = null;
                    let bestLeft = -1;
                    for (const td of document.querySelectorAll('td[area="rows"]')) {
                        if (inTree(td) || !isVis(td)) continue;
                        const span = td.querySelector('span[axis="r"], nobr span');
                        if (!matches(span, dimension)) continue;
                        const r = td.getBoundingClientRect();
                        if (r.top < 90 || r.top > 400) continue;
                        if (r.left > bestLeft) {
                            bestLeft = r.left;
                            bestTd = td;
                        }
                    }
                    if (!bestTd) return false;

                    const span = bestTd.querySelector('span[axis="r"], nobr span');
                    if (!span || !isVis(span)) return false;
                    const sr = span.getBoundingClientRect();
                    const x = sr.x + Math.min(sr.width * 0.32, sr.width - 14);
                    const y = sr.y + sr.height / 2;
                    const opts = {
                        bubbles: true,
                        cancelable: true,
                        clientX: x,
                        clientY: y,
                        button: 2,
                    };
                    span.dispatchEvent(
                        new MouseEvent('mousedown', { ...opts, button: 2 })
                    );
                    span.dispatchEvent(
                        new MouseEvent('mouseup', { ...opts, button: 2 })
                    );
                    span.dispatchEvent(new MouseEvent('contextmenu', opts));
                    return true;
                }
                """,
                [dimension],
            )
        )

    def _click_row_dimension_member_icon(self, frame, dimension: str) -> str | None:
        """Left-click a row-dimension chevron — opens the member Filter menu only."""
        clicked = frame.evaluate(
            """
            ([dimension]) => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const isVis = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));

                const matches = (span, label) => {
                    if (!span) return false;
                    const text = trim(span.textContent);
                    const title = span.getAttribute('title') || '';
                    return (
                        text === label
                        || text.startsWith(label + ' (')
                        || title.includes('[' + label + '.')
                        || title.includes('[' + label + ']')
                    );
                };

                const tryClickIcon = (td) => {
                    if (!td || !isVis(td)) return false;
                    const icon = td.querySelector(
                        'img.member-icon, img[onclick*="LevelContext"], '
                        + 'img[onclick*="ContextClick"]'
                    );
                    if (icon && isVis(icon)) {
                        icon.scrollIntoView({ block: 'center', inline: 'nearest' });
                        icon.click();
                        return true;
                    }
                    return false;
                };

                let bestTd = null;
                let bestLeft = -1;
                for (const td of document.querySelectorAll('td[area="rows"]')) {
                    if (inTree(td) || !isVis(td)) continue;
                    const span = td.querySelector('span[axis="r"], nobr span');
                    if (!matches(span, dimension)) continue;
                    const r = td.getBoundingClientRect();
                    if (r.top < 90 || r.top > 720) continue;
                    if (r.left > bestLeft) {
                        bestLeft = r.left;
                        bestTd = td;
                    }
                }
                if (tryClickIcon(bestTd)) return 'row-dimension-icon';
                return null;
            }
            """,
            [dimension],
        )
        if clicked:
            logger.info(
                "Clicked %r row chevron (%s)", dimension, clicked
            )
        return clicked

    def _click_row_dimension_chevron_locator(
        self, frame, dimension: str
    ) -> bool:
        """Click the chevron (member-icon) beside a row-dimension column header."""
        if self._click_row_dimension_member_icon(frame, dimension):
            return True
        try:
            header_td = frame.locator("td[area='rows']").filter(
                has=frame.locator(
                    f"span[axis='r'][title*='[{dimension}.']"
                )
            )
            if header_td.count() == 0:
                header_td = frame.locator("td[area='rows']").filter(
                    has=frame.locator(f"span[axis='r']:text-is('{dimension}')")
                )
            if header_td.count() == 0:
                return False
            icon = header_td.last.locator(
                "img.member-icon, img[onclick*='LevelContext'], "
                "img[onclick*='ContextClick']"
            ).first
            icon.click(timeout=3_000, force=True)
            logger.info("Clicked %r row chevron via locator (force)", dimension)
            return True
        except Exception as exc:
            logger.info(
                "Locator chevron click for %r failed: %s", dimension, exc
            )
            return False

    def _open_dimension_submenu_via_chevron(
        self, frame, dimension: str
    ) -> None:
        """Click chevron → hover Dimension tab → Remove Dimension submenu ready."""
        menu_ready = lambda: (
            self._is_menu_item_visible("Dimension", partial=False)
            or self._is_menu_item_visible("Dimensions", partial=False)
            or self._pivot_row_filter_menu_open()
            or self._is_menu_item_visible(
                self.remove_dimension_menu_text, partial=True
            )
        )

        for attempt in range(1, 6):
            self._clear_open_popups(frame)
            frame.wait_for_timeout(200)
            self._clear_open_popups(frame)

            clicked = self._click_row_dimension_chevron_locator(frame, dimension)
            if not clicked:
                logger.info(
                    "Chevron for %r not clicked (attempt %d)", dimension, attempt
                )
                frame.wait_for_timeout(400)
                continue

            frame.wait_for_timeout(450)
            if not self._poll_until(frame, menu_ready, timeout_ms=3_000):
                logger.info(
                    "Chevron menu for %r not open (attempt %d)", dimension, attempt
                )
                self._clear_open_popups(frame)
                continue

            self._hover_pivot_dimension_menu_tab(frame)
            frame.wait_for_timeout(600)
            if self._is_menu_item_visible(
                self.remove_dimension_menu_text, partial=True
            ):
                logger.info(
                    "Dimension submenu ready for %r (chevron → hover Dimension)",
                    dimension,
                )
                return

            logger.info(
                "Remove Dimension not visible for %r after hover (attempt %d)",
                dimension,
                attempt,
            )
            self._clear_open_popups(frame)
            frame.wait_for_timeout(300)

        raise PlaywrightTimeoutError(
            f"Dimension submenu for {dimension!r} did not open via chevron"
        )

    def _row_dimension_field_header_locator(self, frame, dimension: str):
        """Playwright locator for the rightmost row-dimension field header span."""
        by_title = frame.locator("td[area='rows']").filter(
            has=frame.locator(
                f"span[axis='r'][title*='[{dimension}.']"
            )
        )
        if by_title.count() > 0:
            return by_title.last.locator("span[axis='r']").first

        by_text = frame.locator("td[area='rows']").filter(
            has=frame.locator(f"span[axis='r']:text-is('{dimension}')")
        )
        if by_text.count() > 0:
            return by_text.last.locator("span[axis='r']").first

        return frame.locator("td[area='rows'] span[axis='r']").filter(
            has_text=dimension
        ).last

    def _right_click_row_dimension_field_label(
        self, frame, dimension: str
    ) -> bool:
        """Native Playwright right-click on the row-dimension field label."""
        try:
            target = self._row_dimension_field_header_locator(frame, dimension)
            if target.count() == 0:
                return False
            header = target.first
            header.wait_for(state="visible", timeout=3_000)
            header.click(button="right", timeout=3_000)
            logger.info("Right-clicked %r row field label via locator", dimension)
            return True
        except Exception as exc:
            logger.info(
                "Locator right-click for %r failed: %s", dimension, exc
            )
            return False

    def _open_dimension_field_menu(self, frame, dimension: str) -> None:
        """Right-click row-dimension field label → field menu (Remove / Move)."""
        markers = (
            self.remove_dimension_menu_text,
            self.move_dimension_to_row_menu_text,
            "Move Dimension to Column",
            "Move Dimension to Filter",
            "Choose Another Dimension",
        )
        is_open = lambda: any(
            self._is_menu_item_visible(marker, partial=True) for marker in markers
        )

        def _header_coords():
            for finder in (
                lambda: self._find_pivot_row_dimension_field_header(
                    frame, dimension
                ),
                lambda: self._find_row_field_header_any(frame, dimension),
                lambda: self._find_row_field_header_loose(frame, dimension),
                lambda: self._find_pivot_row_field_coords(frame, dimension),
            ):
                try:
                    c = finder()
                except Exception:
                    c = None
                if c and self._pivot_row_header_coords_valid(frame, c):
                    return c
            return None

        page = frame.page
        for attempt in range(1, 6):
            self._clear_open_popups(frame)
            if self._pivot_row_filter_menu_open():
                self._clear_open_popups(frame)
                frame.wait_for_timeout(300)

            if self._right_click_row_dimension_field_label(frame, dimension):
                frame.wait_for_timeout(400)
                if self._poll_until(frame, is_open, timeout_ms=3_000):
                    logger.info(
                        "Opened field menu for %r (locator right-click)",
                        dimension,
                    )
                    return
                self._clear_open_popups(frame)

            if self._dispatch_row_dimension_field_contextmenu(frame, dimension):
                frame.wait_for_timeout(400)
                if self._poll_until(frame, is_open, timeout_ms=3_000):
                    logger.info(
                        "Opened field menu for %r (label contextmenu-event)",
                        dimension,
                    )
                    return
                self._clear_open_popups(frame)

            coords = _header_coords()
            if coords:
                px, py = coords["page_x"], coords["page_y"]
                page.mouse.move(px, py)
                frame.wait_for_timeout(150)
                page.mouse.click(px, py, button="right")
                frame.wait_for_timeout(400)
                if self._poll_until(frame, is_open, timeout_ms=3_000):
                    logger.info(
                        "Opened field menu for %r (right-click label)",
                        dimension,
                    )
                    return
                self._clear_open_popups(frame)
                if self._dispatch_contextmenu_at(frame, px, py):
                    frame.wait_for_timeout(400)
                    if self._poll_until(frame, is_open, timeout_ms=3_000):
                        logger.info(
                            "Opened field menu for %r (contextmenu-at-label)",
                            dimension,
                        )
                        return
                    self._clear_open_popups(frame)
            if attempt == 1:
                self._dump_open_menu_state(frame, f"dim_menu_{dimension}")
            logger.info(
                "Field menu for %r not open yet (attempt %d)", dimension, attempt
            )
            self._clear_open_popups(frame)
            frame.wait_for_timeout(400)
        raise PlaywrightTimeoutError(
            f"Dimension field menu for {dimension!r} did not open"
        )

    def remove_pivot_dimension_via_context_menu(self, dimension: str) -> None:
        """Remove a row dimension — chevron → hover Dimension → Remove Dimension."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        logger.info(
            "Removing %r — chevron → hover Dimension → Remove Dimension…",
            dimension,
        )

        for attempt in range(1, 6):
            if not self._row_field_header_present(frame, dimension):
                logger.info("Dimension %r no longer present — removed", dimension)
                self._wait_for_query_idle(frame)
                return

            self._open_dimension_submenu_via_chevron(frame, dimension)
            if self._try_click_menu_item(self.remove_dimension_menu_text):
                self._clear_open_popups(frame)
                self._wait_for_query_idle(frame)
                if self._poll_until(
                    frame,
                    lambda: not self._row_field_header_present(frame, dimension),
                    timeout_ms=15_000,
                ):
                    self._wait_for_query_idle(frame)
                    logger.info("Removed %r via Remove Dimension menu", dimension)
                    return
            logger.info(
                "Remove Dimension for %r did not apply (attempt %d)",
                dimension,
                attempt,
            )
            self._clear_open_popups(frame)
            frame.wait_for_timeout(500)

        raise PlaywrightTimeoutError(
            f"Could not remove dimension {dimension!r} via Remove Dimension menu"
        )

    def _find_row_field_header_any(self, frame, dimension: str) -> dict | None:
        """Page coords of a row-dimension field header (any vertical band)."""
        finders: list = [
            lambda: self._find_pivot_row_dimension_field_header(frame, dimension),
        ]
        if dimension == self.product_dimension:
            finders.append(
                lambda: self._find_pivot_row_product_header_coords(frame)
            )
        elif dimension == self.pack_attribute:
            finders.append(lambda: self._find_pivot_row_pack_header_coords(frame))
        elif dimension == self.brick_attribute:
            finders.append(lambda: self._find_pivot_row_brick_header_coords(frame))
        finders.extend(
            (
                lambda: self._find_row_field_header_loose(frame, dimension),
                lambda: self._find_pivot_row_field_coords(frame, dimension),
            )
        )
        for finder in finders:
            try:
                coords = finder()
            except Exception:
                coords = None
            if coords and self._pivot_drop_y_sane(frame, coords):
                return coords
        return None

    def _row_field_header_present(self, frame, dimension: str) -> bool:
        """True only when the dimension is still a row-zone field header."""
        return (
            self._find_pivot_row_dimension_field_header(frame, dimension)
            is not None
        )

    def remove_pivot_dimension(self, dimension: str) -> None:
        """Remove a row-dimension field via Dimension menu, Delete key, or drag-out."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        logger.info("Removing dimension %r from pivot…", dimension)

        page = frame.page
        fbox = frame.frame_element().bounding_box()
        tree_right = self._schema_tree_right_edge(frame)

        for attempt in range(1, 8):
            if not self._row_field_header_present(frame, dimension):
                logger.info("Dimension %r no longer present — removed", dimension)
                self._wait_for_query_idle(frame)
                return

            if self._pivot_row_filter_menu_open():
                self._clear_open_popups(frame)
                frame.wait_for_timeout(300)

            # Chevron → hover Dimension tab → Remove Dimension.
            try:
                self._open_dimension_submenu_via_chevron(frame, dimension)
                if self._try_click_menu_item(self.remove_dimension_menu_text):
                    self._clear_open_popups(frame)
                    self._wait_for_query_idle(frame)
                    if self._poll_until(
                        frame,
                        lambda: not self._row_field_header_present(frame, dimension),
                        timeout_ms=15_000,
                    ):
                        self._wait_for_query_idle(frame)
                        logger.info(
                            "Removed dimension %r (Remove Dimension menu)",
                            dimension,
                        )
                        return
            except PlaywrightTimeoutError:
                logger.info(
                    "Dimension menu for %r unavailable (attempt %d)",
                    dimension,
                    attempt,
                )
            self._clear_open_popups(frame)

            header = self._find_row_field_header_any(frame, dimension)
            if header is None:
                logger.info("Dimension %r no longer present — removed", dimension)
                self._wait_for_query_idle(frame)
                return

            page.mouse.click(header["page_x"], header["page_y"])
            frame.wait_for_timeout(200)
            page.keyboard.press("Delete")
            if self._poll_until(
                frame,
                lambda: not self._row_field_header_present(frame, dimension),
                timeout_ms=2_000,
            ):
                self._wait_for_query_idle(frame)
                logger.info("Removed dimension %r (Delete key)", dimension)
                return

            start = (header["page_x"], header["page_y"])
            if fbox:
                drop_x = fbox["x"] + max(tree_right * 0.35, 80) + attempt * 6
                drop_y = header["page_y"]
            else:
                drop_x = start[0] - 180 - attempt * 10
                drop_y = start[1]
            end = (drop_x, drop_y)
            logger.info(
                "Dragging %r header left to (%.0f, %.0f) to remove (attempt %d)…",
                dimension,
                end[0],
                end[1],
                attempt,
            )
            self._human_mouse_drag(page, start, end)
            if self._poll_until(
                frame,
                lambda: not self._row_field_header_present(frame, dimension),
                timeout_ms=5_000,
            ):
                self._wait_for_query_idle(frame)
                logger.info("Removed dimension %r (drag-out)", dimension)
                return
            self._clear_open_popups(frame)
            frame.wait_for_timeout(500)

        raise PlaywrightTimeoutError(
            f"Could not remove dimension {dimension!r} from the pivot rows"
        )

    def _click_market_filter_chevron(self, frame) -> bool:
        """Click the chevron beside the Market filter label (not the market name)."""
        clicked = frame.evaluate(
            """
            () => {
                const trim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tree = document.getElementById('trvSchema')
                    || document.querySelector('[id*="trvSchema"]');
                const inTree = (el) => !!(tree && tree.contains(el));
                const isMarketRow = (text) => {
                    if (!text) return false;
                    const t = trim(text);
                    return (
                        t === 'Market (None)'
                        || t.startsWith('Market (')
                        || /^\\*?\\s*Market\\b/.test(t)
                    );
                };

                for (const el of document.body.querySelectorAll('nobr, span, td, div')) {
                    const text = trim(el.textContent);
                    if (!isMarketRow(text)) continue;
                    if (inTree(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.top > 400) continue;
                    const row = el.closest('tr, td, div, nobr') || el.parentElement;
                    if (!row) continue;

                    let bestImg = null;
                    let bestLeft = Infinity;
                    for (const img of row.querySelectorAll(
                        'img.member-icon, img[onclick*="Context"], img'
                    )) {
                        const ir = img.getBoundingClientRect();
                        if (ir.width <= 0 || ir.height <= 0 || ir.top > 220) continue;
                        if (ir.left < bestLeft) {
                            bestLeft = ir.left;
                            bestImg = img;
                        }
                    }
                    if (bestImg) {
                        bestImg.scrollIntoView({ block: 'center', inline: 'nearest' });
                        bestImg.click();
                        return true;
                    }
                }
                return false;
            }
            """
        )
        if clicked:
            logger.info("Clicked Market filter chevron (not market name)")
        return bool(clicked)

    def move_market_dimension_to_row(self) -> None:
        """Move Market to rows — chevron beside Market → hover Dimension → Move to Row."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        logger.info(
            "Moving %r to rows — Market chevron → Dimension → Move Dimension to Row…",
            self.market_dimension,
        )

        menu_ready = lambda: (
            self._is_menu_item_visible("Dimension", partial=False)
            or self._is_menu_item_visible("Dimensions", partial=False)
            or self._pivot_row_filter_menu_open()
            or self._is_menu_item_visible(
                self.move_dimension_to_row_menu_text, partial=True
            )
        )

        for attempt in range(1, 6):
            self._clear_open_popups(frame)
            frame.wait_for_timeout(200)
            self._clear_open_popups(frame)

            if not self._click_market_filter_chevron(frame):
                coords = self._find_market_filter_field_coords(frame)
                fbox = frame.frame_element().bounding_box()
                if coords and fbox:
                    px = fbox["x"] + coords["chevronX"]
                    py = fbox["y"] + coords["chevronY"]
                    frame.page.mouse.click(px, py)
                    logger.info("Clicked Market filter chevron via coords")
                else:
                    logger.info(
                        "Market filter chevron not found (attempt %d)", attempt
                    )
                    frame.wait_for_timeout(400)
                    continue

            frame.wait_for_timeout(450)
            if not self._poll_until(frame, menu_ready, timeout_ms=3_000):
                logger.info("Market chevron menu not open (attempt %d)", attempt)
                continue

            self._hover_pivot_dimension_menu_tab(frame)
            frame.wait_for_timeout(500)
            if self._try_click_menu_item(self.move_dimension_to_row_menu_text):
                self._wait_for_query_idle(frame)
                logger.info("Moved %r to rows (chevron → Dimension)", self.market_dimension)
                return

            logger.info(
                "Move Dimension to Row for %r did not apply (attempt %d)",
                self.market_dimension,
                attempt,
            )
            self._clear_open_popups(frame)
            frame.wait_for_timeout(400)

        raise PlaywrightTimeoutError(
            f"Could not move {self.market_dimension!r} to rows via Market chevron menu"
        )

    def move_pivot_dimension_to_row(self, dimension: str) -> None:
        """Open a pivot field's Dimension menu and click Move Dimension to Row."""
        frame = self._designer_frame()
        self._wait_for_query_idle(frame)
        logger.info("Moving dimension %r into the row zone…", dimension)
        self._open_dimension_field_menu(frame, dimension)
        self._click_menu_item(self.move_dimension_to_row_menu_text)
        self._wait_for_query_idle(frame)
        logger.info("Moved dimension %r to rows", dimension)


class ReportPage:
    concurrent_error_frame_url = "dashboard/ConcurrentError.aspx"
    designer_frame_url = "dashboard/designer.aspx"
    report_viewer_only_button = "#btnViewReport"
    report_viewer_only_text = "Report Viewer Only"

    def __init__(self, page: Page) -> None:
        self.page = page

    def _concurrent_error_frame(self):
        return self.page.frame(
            url=lambda url: url is not None
            and self.concurrent_error_frame_url in url
        )

    def _designer_frame(self):
        return self.page.frame(
            url=lambda url: url is not None and self.designer_frame_url in url
        )

    def needs_report_viewer_only(self) -> bool:
        return self._concurrent_error_frame() is not None

    def wait_for_page_loaded(self, timeout_ms: int = 60_000) -> None:
        for _ in range(timeout_ms // 1_000):
            concurrent = self._concurrent_error_frame()
            if concurrent is not None:
                concurrent.wait_for_load_state("load")
                concurrent.locator(self.report_viewer_only_button).wait_for(
                    state="visible",
                    timeout=timeout_ms,
                )
                return

            if self._designer_frame() is not None:
                return

            self.page.wait_for_timeout(FRAME_POLL_MS)

        raise PlaywrightTimeoutError(
            "Neither concurrent-error dialog nor designer report frame loaded "
            "after opening CRESCOR Test(1)"
        )

    def clickReportViewerOnly(self) -> None:
        frame = self._concurrent_error_frame()
        if frame is None:
            return
        frame.locator(self.report_viewer_only_button).click()


class DesignerPage:
    designer_frame_url = "dashboard/designer.aspx"
    csv_export_button = "#CSV"
    csv_export_icon = 'img[title="Export to CSV..."]'
    xls_export_button = "#EXCEL"
    xls_export_icon = 'img[title="Export to Excel..."]'
    query_running_text = "Query is running"

    def __init__(self, page: Page) -> None:
        self.page = page

    def _designer_frame(self):
        for _ in range(120):
            frame = self.page.frame(
                url=lambda url: url is not None and self.designer_frame_url in url
            )
            if frame:
                return frame
            self.page.wait_for_timeout(FRAME_POLL_MS)

        raise PlaywrightTimeoutError(
            f"Designer frame ({self.designer_frame_url}) did not load"
        )

    def _is_query_running_visible(self) -> bool:
        frame = self.page.frame(
            url=lambda url: url is not None and self.designer_frame_url in url
        )
        if frame is None:
            return False
        try:
            query_running = frame.get_by_text(
                self.query_running_text,
                exact=True,
            )
            return query_running.count() > 0 and query_running.first.is_visible()
        except Exception:
            return False

    def _is_csv_ready(self, frame) -> bool:
        csv_button = frame.locator(self.csv_export_button)
        if csv_button.count() == 0:
            return False
        return (
            csv_button.is_visible()
            and csv_button.get_attribute("enabled") == "True"
            and not self._is_query_running_visible()
        )

    def wait_for_query_complete(
        self, timeout_ms: int = QUERY_EXPORT_MAX_WAIT_MS
    ) -> None:
        frame = self._designer_frame()
        csv_button = frame.locator(self.csv_export_button)
        csv_button.wait_for(state="visible", timeout=timeout_ms)

        poll_ms = QUERY_EXPORT_POLL_MS
        popup_appear_wait_sec = 10.0
        stable_ready_checks = QUERY_EXPORT_STABLE_CHECKS
        deadline = timeout_ms / 1_000
        elapsed = 0.0
        saw_running = False

        # Phase 1 — wait briefly for "Query is running" to appear.
        while elapsed < min(deadline, popup_appear_wait_sec):
            if self._is_query_running_visible():
                saw_running = True
                logger.info("Query is running popup detected")
                break
            frame.page.wait_for_timeout(poll_ms)
            elapsed += poll_ms / 1_000

        # Phase 2 — wait for query popup to disappear.
        if saw_running:
            while elapsed < deadline:
                if not self._is_query_running_visible():
                    logger.info("Query is running popup cleared")
                    break
                frame.page.wait_for_timeout(poll_ms)
                elapsed += poll_ms / 1_000
            else:
                raise PlaywrightTimeoutError(
                    "Query is running popup did not disappear in time"
                )

        # Phase 3 — proceed as soon as export is stably ready.
        ready_streak = 0
        while elapsed < deadline:
            if self._is_csv_ready(frame):
                ready_streak += 1
                if ready_streak >= stable_ready_checks:
                    logger.info(
                        "CSV export ready after %.1fs (query popup seen: %s)",
                        elapsed,
                        saw_running,
                    )
                    return
            else:
                ready_streak = 0
            frame.page.wait_for_timeout(poll_ms)
            elapsed += poll_ms / 1_000

        raise PlaywrightTimeoutError(
            "CSV export button not ready after query completed"
        )

    def clickCsvExport(self) -> None:
        frame = self._designer_frame()
        csv_button = frame.locator(self.csv_export_button)
        if csv_button.count() == 0:
            frame.locator(self.csv_export_icon).click()
            return
        frame.evaluate("document.getElementById('CSV').click()")

    def clickXlsExport(self) -> None:
        frame = self._designer_frame()
        clicked = frame.evaluate(
            """() => {
                const byId = document.getElementById('EXCEL');
                if (byId) {
                    byId.click();
                    return true;
                }
                const img = document.querySelector('img[title="Export to Excel..."]');
                if (img) {
                    img.click();
                    return true;
                }
                return false;
            }"""
        )
        if not clicked:
            raise PlaywrightTimeoutError("XLS export button not found in designer frame")


class ExportCsvPage:
    export_csv_frame_url = "dialogs/analyzer/ExportCSV.aspx"
    export_excel_frame_url = "dialogs/analyzer/ExportExcel.aspx"
    export_dialog_url_part = "dialogs/analyzer/Export"
    export_button = "#btnExport"
    export_button_selectors = (
        "#btnExport",
        'input[value="Export"]',
        'button:has-text("Export")',
    )
    export_options_title = "Export Options"
    export_sheet_label = "All"
    column_header_label = "Include Column Header"
    row_hierarchy_label = "Include Row Hierarchy"
    column_hierarchy_label = "Include Column Hierarchy"
    cell_style_flat_label = "Flat"
    cell_style_hierarchical_label = "Hierarchical"
    excel_format_dashboard_label = "Dashboard"
    excel_data_range_all_label = "All"
    excel_filter_info_label = "Includes Filter Information"
    excel_leading_space_label = "Add Leading Space To Member"
    excel_2007_label = "Excel 2007"
    excel_cell_format_number_label = "Number"
    excel_expand_all_levels_label = "Expand All Levels"
    excel_expand_row_label = "Row"
    excel_expand_column_label = "Column"
    excel_image_quality_print_label = "For Print"
    _checkbox_known_ids = {
        column_header_label: (
            "chkIncludeColumnHeader",
            "chkColumnHeader",
            "chkHeader",
            "chkIncludeHeader",
            "includeColumnHeader",
            "cbColumnHeader",
        ),
        row_hierarchy_label: (
            "chkIncludeRowHierarchy",
            "chkRowHierarchy",
            "chkIncludeRowHier",
            "includeRowHierarchy",
            "cbRowHierarchy",
        ),
        column_hierarchy_label: (
            "chkIncludeColumnHierarchy",
            "chkColumnHierarchy",
            "chkIncludeColHier",
            "includeColumnHierarchy",
            "cbColumnHierarchy",
        ),
        excel_filter_info_label: (
            "chkIncludeFilterInformation",
            "chkIncludesFilterInformation",
            "chkFilterInformation",
            "chkFilterInfo",
            "cbIncludeFilterInformation",
        ),
    }
    _excel_expand_row_checkbox_ids = (
        "chkExpandAllLevelRow",
        "chkExpandAllLevelsRow",
        "chkExpandRow",
        "chkRowExpandAll",
        "cbExpandRow",
    )
    _excel_expand_column_checkbox_ids = (
        "chkExpandAllLevelColumn",
        "chkExpandAllLevelsColumn",
        "chkExpandColumn",
        "chkColumnExpandAll",
        "cbExpandColumn",
    )

    def __init__(self, page: Page) -> None:
        self.page = page
        self._export_frame_ref = None

    def _frame_has_export_button(self, frame) -> bool:
        for selector in self.export_button_selectors:
            try:
                export_btn = frame.locator(selector)
                if export_btn.count() > 0 and export_btn.first.is_visible():
                    return True
            except Exception:
                continue
        try:
            export_btn = frame.get_by_role("button", name="Export", exact=True)
            return export_btn.count() > 0 and export_btn.first.is_visible()
        except Exception:
            return False

    def _frame_has_export_options(self, frame) -> bool:
        try:
            title = frame.get_by_text(self.export_options_title, exact=True)
            return title.count() > 0 and title.first.is_visible()
        except Exception:
            return False

    def _export_frame(self):
        for _ in range(30):
            for frame in self.page.frames:
                if self._frame_has_export_button(frame):
                    return frame

            if self._frame_has_export_button(self.page.main_frame):
                return self.page.main_frame

            for frame in self.page.frames:
                if self._frame_has_export_options(frame):
                    return frame

            frame = self.page.frame(
                url=lambda url: url is not None
                and self.export_dialog_url_part in url
            )
            if frame:
                return frame

            self.page.wait_for_timeout(FRAME_POLL_MS)

        raise PlaywrightTimeoutError(
            "Export Options popup did not load (Export button not found)"
        )

    def _wait_for_export_button(self, frame, timeout_ms: int) -> None:
        last_error: PlaywrightTimeoutError | None = None
        for selector in self.export_button_selectors:
            try:
                frame.locator(selector).first.wait_for(
                    state="visible",
                    timeout=timeout_ms,
                )
                return
            except PlaywrightTimeoutError as exc:
                last_error = exc

        try:
            frame.get_by_role("button", name="Export", exact=True).wait_for(
                state="visible",
                timeout=timeout_ms,
            )
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc

        if last_error is not None:
            raise last_error
        raise PlaywrightTimeoutError("Export button not found in Export Options popup")

    def wait_for_page_loaded(self, timeout_ms: int = 60_000) -> None:
        self._export_frame_ref = self._export_frame()
        self._wait_for_export_button(self._export_frame_ref, timeout_ms)

    def _iter_export_targets(self):
        """Yield unique frames that may host the Export Options dialog."""
        seen: set[int] = set()
        candidates = []
        if self._export_frame_ref is not None:
            candidates.append(self._export_frame_ref)
        for pg in self.page.context.pages:
            candidates.extend(pg.frames)

        for frame in candidates:
            frame_id = id(frame)
            if frame_id not in seen:
                seen.add(frame_id)
                yield frame

    def _dump_export_frame_inputs(self, frame) -> None:
        """Log inputs in a frame when checkbox selection fails."""
        logger.info("Export frame URL: %s", getattr(frame, "url", "unknown"))
        info = frame.evaluate(
            """() => {
                const inputs = Array.from(document.querySelectorAll('input'));
                return inputs.map(el => ({
                    id: el.id,
                    name: el.name,
                    type: el.type,
                    value: el.value,
                    checked: el.checked,
                    outerHTML: el.outerHTML.slice(0, 200),
                    parentText: (el.parentElement
                        ? el.parentElement.textContent.trim().slice(0, 100)
                        : ''),
                }));
            }"""
        )
        for item in info:
            logger.info("INPUT: %s", item)

    def _ensure_checkbox_checked_in_frame(self, frame, label_text: str) -> bool:
        """Try every strategy to tick an export checkbox inside one frame."""
        try:
            checkbox = frame.get_by_label(label_text, exact=False)
            if checkbox.count() > 0:
                target = checkbox.first
                target.wait_for(state="visible", timeout=3_000)
                if not target.is_checked():
                    target.check(force=True)
                logger.info(
                    "%s checked via get_by_label in %s",
                    label_text,
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass

        try:
            row = frame.locator("tr").filter(has_text=label_text)
            if row.count() > 0:
                row_checkbox = row.first.locator('input[type="checkbox"]')
                if row_checkbox.count() > 0:
                    row_checkbox.first.wait_for(state="visible", timeout=3_000)
                    if not row_checkbox.first.is_checked():
                        row_checkbox.first.check(force=True)
                    logger.info(
                        "%s checked via table row in %s",
                        label_text,
                        getattr(frame, "url", "unknown"),
                    )
                    return True
        except Exception:
            pass

        try:
            label = frame.get_by_text(label_text, exact=True)
            if label.count() > 0 and label.first.is_visible():
                label.first.click(timeout=3_000)
                logger.info(
                    "%s clicked via label text in %s",
                    label_text,
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass

        slug = label_text.lower().replace("include ", "").replace(" ", "")
        try:
            result = frame.evaluate(
                """({ labelText, knownIds, slug }) => {
                    const matchText = labelText.toLowerCase();

                    const checkAndSet = (el) => {
                        if (!el || el.type !== 'checkbox') return null;
                        const wasChecked = el.checked;
                        if (!el.checked) {
                            el.checked = true;
                            el.click();
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return { id: el.id, name: el.name, wasChecked };
                    };

                    for (const id of knownIds) {
                        const el = document.getElementById(id);
                        const r = checkAndSet(el);
                        if (r) return { found: true, method: 'id:' + id, ...r };
                    }

                    for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
                        const id = (cb.id || '').toLowerCase();
                        const name = (cb.name || '').toLowerCase();
                        if (id.includes(slug) || name.includes(slug)) {
                            const r = checkAndSet(cb);
                            if (r) return { found: true, method: 'partial-id', ...r };
                        }
                    }

                    for (const label of document.querySelectorAll('label')) {
                        if (label.textContent.trim().toLowerCase().includes(matchText)) {
                            const forId = label.getAttribute('for');
                            const cb = forId
                                ? document.getElementById(forId)
                                : label.querySelector('input[type="checkbox"]');
                            const r = checkAndSet(cb);
                            if (r) return { found: true, method: 'label-for', ...r };
                        }
                    }

                    for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
                        const cell = cb.closest('td, tr, div, span, li, p') || cb.parentElement;
                        if (cell && cell.textContent.toLowerCase().includes(matchText)) {
                            const r = checkAndSet(cb);
                            if (r) return { found: true, method: 'cell-text', ...r };
                        }
                    }

                    return { found: false };
                }""",
                {
                    "labelText": label_text,
                    "knownIds": list(self._checkbox_known_ids.get(label_text, ())),
                    "slug": slug,
                },
            )
            if result.get("found"):
                logger.info(
                    "%s checked via %s (id=%s, was=%s) in %s",
                    label_text,
                    result.get("method"),
                    result.get("id"),
                    result.get("wasChecked"),
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass

        return False

    def _set_expand_all_levels_js(self, frame) -> dict:
        """Tick Expand All Levels Row + Column via DOM (no generic Row/Column match)."""
        try:
            return frame.evaluate(
                """({ rowIds, colIds }) => {
                    const checkAndSet = (cb) => {
                        if (!cb || cb.type !== 'checkbox') return null;
                        const wasChecked = !!cb.checked;
                        if (!cb.checked) {
                            cb.checked = true;
                            cb.click();
                            cb.dispatchEvent(new Event('change', { bubbles: true }));
                            cb.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                        return {
                            id: cb.id || '',
                            wasChecked,
                            nowChecked: !!cb.checked,
                        };
                    };

                    for (let i = 0; i < rowIds.length; i++) {
                        const rowCb = document.getElementById(rowIds[i]);
                        const colCb = document.getElementById(colIds[i]);
                        if (!rowCb || !colCb) continue;
                        const rowResult = checkAndSet(rowCb);
                        const colResult = checkAndSet(colCb);
                        if (rowResult?.nowChecked && colResult?.nowChecked) {
                            return {
                                ok: true,
                                method: 'known-id',
                                row: rowResult,
                                column: colResult,
                            };
                        }
                    }

                    const resolveCheckboxForLabel = (row, label) => {
                        const match = label.toLowerCase();
                        const tds = Array.from(row.querySelectorAll('td'));
                        for (let i = 0; i < tds.length; i++) {
                            const tdText = tds[i].textContent.trim().toLowerCase();
                            if (tdText !== match && !tdText.startsWith(match)) {
                                continue;
                            }
                            const cb = tds[i].querySelector('input[type="checkbox"]')
                                || (i > 0
                                    ? tds[i - 1].querySelector('input[type="checkbox"]')
                                    : null);
                            if (cb) return cb;
                        }
                        return null;
                    };

                    const processExpandRow = (row) => {
                        let rowCb = resolveCheckboxForLabel(row, 'row');
                        let colCb = resolveCheckboxForLabel(row, 'column');
                        const cbs = Array.from(
                            row.querySelectorAll('input[type="checkbox"]')
                        );
                        if (!rowCb && !colCb && cbs.length >= 2) {
                            rowCb = cbs[0];
                            colCb = cbs[1];
                        }
                        if (!rowCb || !colCb) return null;
                        return {
                            row: checkAndSet(rowCb),
                            column: checkAndSet(colCb),
                        };
                    };

                    const rows = Array.from(document.querySelectorAll('tr'));
                    for (let i = 0; i < rows.length; i++) {
                        const row = rows[i];
                        if (!row.textContent.toLowerCase().includes(
                            'expand all levels'
                        )) {
                            continue;
                        }

                        let result = processExpandRow(row);
                        if (!result && rows[i + 1]) {
                            const nextCbs = Array.from(
                                rows[i + 1].querySelectorAll('input[type="checkbox"]')
                            );
                            if (nextCbs.length >= 2) {
                                result = {
                                    row: checkAndSet(nextCbs[0]),
                                    column: checkAndSet(nextCbs[1]),
                                };
                            }
                        }
                        if (result?.row?.nowChecked && result?.column?.nowChecked) {
                            return { ok: true, method: 'expand-row', ...result };
                        }
                    }

                    return { ok: false };
                }""",
                {
                    "rowIds": list(self._excel_expand_row_checkbox_ids),
                    "colIds": list(self._excel_expand_column_checkbox_ids),
                },
            )
        except Exception as exc:
            logger.warning("Expand All Levels JS error: %s", exc)
            return {"ok": False}

    def _set_expand_all_levels_playwright(self, frame) -> bool:
        """Click Expand All Levels Row + Column checkboxes via Playwright."""
        try:
            expand_row = frame.locator("tr").filter(
                has_text=re.compile(r"Expand\s+All\s+Levels", re.I)
            ).first
            if expand_row.count() == 0:
                return False
            expand_row.wait_for(state="visible", timeout=3_000)

            checkboxes = expand_row.locator('input[type="checkbox"]')
            if checkboxes.count() >= 2:
                for idx in (0, 1):
                    box = checkboxes.nth(idx)
                    box.scroll_into_view_if_needed(timeout=2_000)
                    if not box.is_checked():
                        box.check(force=True)
                if (
                    checkboxes.nth(0).is_checked()
                    and checkboxes.nth(1).is_checked()
                ):
                    return True

            for label in (self.excel_expand_row_label, self.excel_expand_column_label):
                cell = expand_row.locator("td").filter(
                    has_text=re.compile(rf"^{re.escape(label)}$", re.I)
                )
                if cell.count() > 0:
                    cell.first.click(force=True, timeout=2_000)

            if checkboxes.count() >= 2:
                return (
                    checkboxes.nth(0).is_checked()
                    and checkboxes.nth(1).is_checked()
                )
        except Exception as exc:
            logger.debug("Playwright Expand All Levels: %s", exc)
        return False

    def _ensure_expand_all_levels_checked_in_frame(self, frame) -> bool:
        """Ensure Expand All Levels Row and Column checkboxes are checked."""
        result = self._set_expand_all_levels_js(frame)
        if result.get("ok"):
            logger.info(
                "Expand All Levels Row+Column checked via %s in %s",
                result.get("method"),
                getattr(frame, "url", "unknown"),
            )
            return True
        if self._set_expand_all_levels_playwright(frame):
            logger.info(
                "Expand All Levels Row+Column checked via Playwright in %s",
                getattr(frame, "url", "unknown"),
            )
            return True
        logger.warning(
            "Could not verify Expand All Levels Row+Column in %s: %s",
            getattr(frame, "url", "unknown"),
            result,
        )
        return False

    def _ensure_expand_all_levels_on_export_dialog(self) -> bool:
        """Retry Expand All Levels on every export frame until both boxes are ticked."""
        export_frames: list = []
        for frame in self._iter_export_targets():
            if self._frame_has_export_options(frame) or self._frame_has_export_button(
                frame
            ):
                export_frames.append(frame)
        if not export_frames and self._export_frame_ref is not None:
            export_frames = [self._export_frame_ref]

        for attempt in range(4):
            for frame in export_frames:
                if self._ensure_expand_all_levels_checked_in_frame(frame):
                    return True
            self.page.wait_for_timeout(300)

        for frame in export_frames:
            self._dump_export_frame_inputs(frame)
        return False

    def _select_hierarchical_cell_style_in_frame(self, frame) -> bool:
        """Select the Hierarchical cell-style radio button in one frame."""
        label_text = self.cell_style_hierarchical_label

        try:
            radio = frame.get_by_role("radio", name=label_text, exact=True)
            if radio.count() > 0:
                target = radio.first
                target.wait_for(state="visible", timeout=3_000)
                if not target.is_checked():
                    target.check(force=True)
                logger.info(
                    "Cell Style set to %s via get_by_role in %s",
                    label_text,
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass

        try:
            label = frame.get_by_label(label_text, exact=True)
            if label.count() > 0:
                target = label.first
                target.wait_for(state="visible", timeout=3_000)
                if not target.is_checked():
                    target.check(force=True)
                logger.info(
                    "Cell Style set to %s via get_by_label in %s",
                    label_text,
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass

        try:
            result = frame.evaluate(
                """(labelText) => {
                    const matchText = labelText.toLowerCase();

                    const selectRadio = (el) => {
                        if (!el || el.type !== 'radio') return null;
                        const wasChecked = el.checked;
                        if (!el.checked) {
                            el.checked = true;
                            el.click();
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return { id: el.id, name: el.name, wasChecked };
                    };

                    const knownIds = [
                        'rbHierarchical',
                        'rdoHierarchical',
                        'cellStyleHierarchical',
                        'chkHierarchical',
                    ];
                    for (const id of knownIds) {
                        const el = document.getElementById(id);
                        const r = selectRadio(el);
                        if (r) return { found: true, method: 'id:' + id, ...r };
                    }

                    for (const label of document.querySelectorAll('label')) {
                        if (label.textContent.trim().toLowerCase() === matchText) {
                            const forId = label.getAttribute('for');
                            const radio = forId
                                ? document.getElementById(forId)
                                : label.querySelector('input[type="radio"]');
                            const r = selectRadio(radio);
                            if (r) return { found: true, method: 'label-for', ...r };
                        }
                    }

                    for (const radio of document.querySelectorAll('input[type="radio"]')) {
                        const cell = radio.closest('td, tr, div, span, li, p') || radio.parentElement;
                        if (!cell) continue;
                        const text = cell.textContent.toLowerCase();
                        if (text.includes('cell style') && text.includes(matchText)) {
                            const r = selectRadio(radio);
                            if (r) return { found: true, method: 'cell-style-cell', ...r };
                        }
                    }

                    for (const radio of document.querySelectorAll('input[type="radio"]')) {
                        const cell = radio.closest('td, tr, div, span, li, p') || radio.parentElement;
                        if (cell && cell.textContent.trim().toLowerCase() === matchText) {
                            const r = selectRadio(radio);
                            if (r) return { found: true, method: 'cell-text', ...r };
                        }
                    }

                    return { found: false };
                }""",
                label_text,
            )
            if result.get("found"):
                logger.info(
                    "Cell Style set to %s via %s (id=%s) in %s",
                    label_text,
                    result.get("method"),
                    result.get("id"),
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass

        return False

    def _select_comma_delimiter_in_frame(self, frame) -> bool:
        """Select comma as the CSV delimiter in the export dialog."""
        label_text = self.comma_delimiter_label

        try:
            radio = frame.get_by_role("radio", name=label_text, exact=True)
            if radio.count() > 0:
                target = radio.first
                target.wait_for(state="visible", timeout=3_000)
                if not target.is_checked():
                    target.check(force=True)
                logger.info(
                    "Delimiter set to %s via get_by_role in %s",
                    label_text,
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass

        try:
            result = frame.evaluate(
                """(labelText) => {
                    const matchText = labelText.toLowerCase();

                    const selectRadio = (el) => {
                        if (!el || el.type !== 'radio') return null;
                        if (!el.checked) {
                            el.checked = true;
                            el.click();
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return { id: el.id, name: el.name };
                    };

                    for (const label of document.querySelectorAll('label')) {
                        if (label.textContent.trim().toLowerCase() === matchText) {
                            const forId = label.getAttribute('for');
                            const radio = forId
                                ? document.getElementById(forId)
                                : label.querySelector('input[type="radio"]');
                            const r = selectRadio(radio);
                            if (r) return { found: true, method: 'label-for', ...r };
                        }
                    }

                    for (const radio of document.querySelectorAll('input[type="radio"]')) {
                        const cell = radio.closest('td, tr, div, span, li, p') || radio.parentElement;
                        if (cell && cell.textContent.trim().toLowerCase() === matchText) {
                            const r = selectRadio(radio);
                            if (r) return { found: true, method: 'cell-text', ...r };
                        }
                    }

                    return { found: false };
                }""",
                label_text,
            )
            if result.get("found"):
                logger.info(
                    "Delimiter set to %s via %s in %s",
                    label_text,
                    result.get("method"),
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass

        return False

    def _ensure_checkbox_unchecked_in_frame(self, frame, label_text: str) -> bool:
        """Uncheck a checkbox by label text, trying multiple strategies."""
        slug = label_text.lower().replace("include ", "").replace(" ", "")
        try:
            result = frame.evaluate(
                """({ labelText, knownIds, slug }) => {
                    const matchText = labelText.toLowerCase();

                    const uncheckEl = (el) => {
                        if (!el || el.type !== 'checkbox') return null;
                        const wasChecked = el.checked;
                        if (el.checked) {
                            el.checked = false;
                            el.click();
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return { id: el.id, name: el.name, wasChecked };
                    };

                    for (const id of knownIds) {
                        const el = document.getElementById(id);
                        const r = uncheckEl(el);
                        if (r) return { found: true, method: 'id:' + id, ...r };
                    }

                    for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
                        const id = (cb.id || '').toLowerCase();
                        const name = (cb.name || '').toLowerCase();
                        if (id.includes(slug) || name.includes(slug)) {
                            const r = uncheckEl(cb);
                            if (r) return { found: true, method: 'partial-id', ...r };
                        }
                    }

                    for (const label of document.querySelectorAll('label')) {
                        if (label.textContent.trim().toLowerCase().includes(matchText)) {
                            const forId = label.getAttribute('for');
                            const cb = forId
                                ? document.getElementById(forId)
                                : label.querySelector('input[type="checkbox"]');
                            const r = uncheckEl(cb);
                            if (r) return { found: true, method: 'label-for', ...r };
                        }
                    }

                    return { found: false };
                }""",
                {
                    "labelText": label_text,
                    "knownIds": list(self._checkbox_known_ids.get(label_text, ())),
                    "slug": slug,
                },
            )
            if result.get("found"):
                logger.info(
                    "%s unchecked via %s (was=%s) in %s",
                    label_text,
                    result.get("method"),
                    result.get("wasChecked"),
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass
        return False

    def _select_flat_cell_style_in_frame(self, frame) -> bool:
        """Select the Flat cell-style radio to restore the default MTH column export."""
        label_text = self.cell_style_flat_label
        try:
            result = frame.evaluate(
                """(labelText) => {
                    const matchText = labelText.toLowerCase();

                    const selectRadio = (el) => {
                        if (!el || el.type !== 'radio') return null;
                        const wasChecked = el.checked;
                        if (!el.checked) {
                            el.checked = true;
                            el.click();
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return { id: el.id, name: el.name, wasChecked };
                    };

                    for (const label of document.querySelectorAll('label')) {
                        if (label.textContent.trim().toLowerCase() === matchText) {
                            const forId = label.getAttribute('for');
                            const radio = forId
                                ? document.getElementById(forId)
                                : label.querySelector('input[type="radio"]');
                            const r = selectRadio(radio);
                            if (r) return { found: true, method: 'label-for', ...r };
                        }
                    }

                    for (const radio of document.querySelectorAll('input[type="radio"]')) {
                        const cell = radio.closest('td, tr, div, span, li, p') || radio.parentElement;
                        if (cell && cell.textContent.trim().toLowerCase() === matchText) {
                            const r = selectRadio(radio);
                            if (r) return { found: true, method: 'cell-text', ...r };
                        }
                    }

                    return { found: false };
                }""",
                label_text,
            )
            if result.get("found"):
                logger.info(
                    "Cell Style set to Flat via %s in %s",
                    result.get("method"),
                    getattr(frame, "url", "unknown"),
                )
                return True
        except Exception:
            pass
        return False

    def _select_export_sheet_in_frame(self, frame, sheet_name: str) -> bool:
        """Select the report sheet to export (e.g. All, C-Prod)."""
        try:
            select = frame.locator("select").first
            if select.count() > 0:
                select.select_option(label=sheet_name)
                logger.info("Export sheet set to %s via select", sheet_name)
                return True
        except Exception:
            pass

        try:
            result = frame.evaluate(
                """(sheetName) => {
                    const select = document.querySelector('select');
                    if (!select) return { found: false };
                    for (const opt of select.options) {
                        if (opt.text.trim() === sheetName) {
                            select.value = opt.value;
                            select.dispatchEvent(new Event('change', { bubbles: true }));
                            return { found: true, value: opt.value };
                        }
                    }
                    return { found: false };
                }""",
                sheet_name,
            )
            if result.get("found"):
                logger.info("Export sheet set to %s via evaluate", sheet_name)
                return True
        except Exception:
            pass

        return False

    def _configure_excel_export_options_fast(self, frame) -> bool:
        """Set Excel export dialog options in one shot (ExportExcel.aspx)."""
        try:
            result = frame.evaluate(
                """(opts) => {
                    const selectRadio = (el) => {
                        if (!el || el.type !== 'radio') return false;
                        if (!el.checked) {
                            el.checked = true;
                            el.click();
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return true;
                    };

                    const selectRadioByText = (text, sectionHint) => {
                        const match = text.toLowerCase();
                        const section = (sectionHint || '').toLowerCase();

                        if (section) {
                            for (const row of document.querySelectorAll('tr')) {
                                const rowText = row.textContent.toLowerCase();
                                if (!rowText.includes(section)) continue;
                                for (const radio of row.querySelectorAll('input[type="radio"]')) {
                                    const cell = radio.closest('label, td, span, div')
                                        || radio.parentElement;
                                    const labelText = (cell
                                        ? cell.textContent
                                        : '').trim().toLowerCase();
                                    if (labelText === match || labelText.includes(match)) {
                                        if (selectRadio(radio)) return true;
                                    }
                                }
                            }
                        }

                        for (const label of document.querySelectorAll('label')) {
                            const labelText = label.textContent.trim().toLowerCase();
                            if (labelText === match || labelText.includes(match)) {
                                const forId = label.getAttribute('for');
                                const radio = forId
                                    ? document.getElementById(forId)
                                    : label.querySelector('input[type="radio"]');
                                if (selectRadio(radio)) return true;
                            }
                        }

                        for (const radio of document.querySelectorAll('input[type="radio"]')) {
                            const cell = radio.closest('td, label, span, div, tr')
                                || radio.parentElement;
                            if (!cell) continue;
                            const labelText = cell.textContent.trim().toLowerCase();
                            if (labelText === match || labelText.includes(match)) {
                                if (selectRadio(radio)) return true;
                            }
                        }
                        return false;
                    };

                    const setCheckboxByLabel = (labelText, shouldCheck) => {
                        const match = labelText.toLowerCase();
                        for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
                            const row = cb.closest('tr, label, div, span, td')
                                || cb.parentElement;
                            const text = (row ? row.textContent : '').toLowerCase();
                            if (!text.includes(match)) continue;
                            if (cb.checked !== shouldCheck) {
                                cb.checked = shouldCheck;
                                cb.click();
                                cb.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                            return true;
                        }
                        for (const label of document.querySelectorAll('label')) {
                            if (!label.textContent.toLowerCase().includes(match)) continue;
                            const forId = label.getAttribute('for');
                            const cb = forId
                                ? document.getElementById(forId)
                                : label.querySelector('input[type="checkbox"]');
                            if (!cb || cb.type !== 'checkbox') continue;
                            if (cb.checked !== shouldCheck) {
                                cb.checked = shouldCheck;
                                cb.click();
                                cb.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                            return true;
                        }
                        return false;
                    };

                    const setExpandLevel = (levelText, shouldCheck) => {
                        const match = levelText.toLowerCase();
                        const resolveCheckboxForLabel = (row, label) => {
                            const want = label.toLowerCase();
                            const tds = Array.from(row.querySelectorAll('td'));
                            for (let i = 0; i < tds.length; i++) {
                                const tdText = tds[i].textContent.trim().toLowerCase();
                                if (tdText !== want && !tdText.startsWith(want)) {
                                    continue;
                                }
                                const cb = tds[i].querySelector('input[type="checkbox"]')
                                    || (i > 0
                                        ? tds[i - 1].querySelector(
                                            'input[type="checkbox"]'
                                        )
                                        : null);
                                if (cb) return cb;
                            }
                            return null;
                        };

                        for (const row of document.querySelectorAll('tr')) {
                            if (!row.textContent.toLowerCase().includes(
                                'expand all levels'
                            )) {
                                continue;
                            }
                            const cb = resolveCheckboxForLabel(row, match);
                            if (!cb) {
                                const cbs = Array.from(
                                    row.querySelectorAll('input[type="checkbox"]')
                                );
                                if (cbs.length === 2) {
                                    const pick = match === 'row' ? cbs[0] : cbs[1];
                                    if (pick) {
                                        if (pick.checked !== shouldCheck) {
                                            pick.checked = shouldCheck;
                                            pick.click();
                                            pick.dispatchEvent(
                                                new Event('change', { bubbles: true })
                                            );
                                        }
                                        return true;
                                    }
                                }
                                continue;
                            }
                            if (cb.checked !== shouldCheck) {
                                cb.checked = shouldCheck;
                                cb.click();
                                cb.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                            return true;
                        }
                        return false;
                    };

                    let sheetSet = false;
                    const select = document.querySelector('select');
                    if (select) {
                        for (const opt of select.options) {
                            if (opt.text.trim() === opts.sheetName) {
                                select.value = opt.value;
                                select.dispatchEvent(new Event('change', { bubbles: true }));
                                sheetSet = true;
                                break;
                            }
                        }
                    }

                    const results = {
                        sheetSet,
                        formatDashboard: selectRadioByText(
                            opts.formatDashboard, 'format'
                        ),
                        dataRangeAll: selectRadioByText(
                            opts.dataRangeAll, 'data range'
                        ),
                        cellStyleFlat: selectRadioByText(
                            opts.cellStyleFlat, 'cell style'
                        ),
                        leadingSpaceOff: setCheckboxByLabel(
                            opts.leadingSpaceLabel, false
                        ),
                        filterInfoOn: setCheckboxByLabel(
                            opts.filterInfoLabel, true
                        ),
                        excel2007: selectRadioByText(opts.excel2007Label, 'excel'),
                        cellFormatNumber: selectRadioByText(
                            opts.cellFormatNumber, 'cell format'
                        ),
                        expandRowOn: setExpandLevel(opts.expandRow, true),
                        expandColumnOn: setExpandLevel(opts.expandColumn, true),
                        imageForPrint: selectRadioByText(
                            opts.imageQualityPrint, 'image quality'
                        ),
                    };

                    const required = [
                        'sheetSet',
                        'formatDashboard',
                        'dataRangeAll',
                        'cellStyleFlat',
                        'filterInfoOn',
                        'excel2007',
                        'cellFormatNumber',
                        'expandRowOn',
                        'expandColumnOn',
                        'imageForPrint',
                    ];
                    const ok = required.every((key) => results[key]);

                    return { ok, results };
                }""",
                {
                    "sheetName": self.export_sheet_label,
                    "formatDashboard": self.excel_format_dashboard_label,
                    "dataRangeAll": self.excel_data_range_all_label,
                    "cellStyleFlat": self.cell_style_flat_label,
                    "leadingSpaceLabel": self.excel_leading_space_label,
                    "filterInfoLabel": self.excel_filter_info_label,
                    "excel2007Label": self.excel_2007_label,
                    "cellFormatNumber": self.excel_cell_format_number_label,
                    "expandRow": self.excel_expand_row_label,
                    "expandColumn": self.excel_expand_column_label,
                    "imageQualityPrint": self.excel_image_quality_print_label,
                },
            )
            if result.get("ok"):
                logger.info(
                    "Excel export options set (sheet=%s, Dashboard, Flat, "
                    "filter info on, expand all levels Row+Column, "
                    "xlsx, Number, For Print)",
                    self.export_sheet_label,
                )
                return True
            logger.warning("Fast Excel export configure partial result: %s", result)
        except Exception as exc:
            logger.warning("Fast Excel export configure error: %s", exc)
        return False

    def configure_excel_export_options(self) -> None:
        """Set Excel export dialog options (ExportExcel.aspx) before Export."""
        target_frame = self._export_frame_ref
        if target_frame is None:
            for frame in self._iter_export_targets():
                if self._frame_has_export_options(frame) or self._frame_has_export_button(
                    frame
                ):
                    target_frame = frame
                    break
        if target_frame is None:
            target_frame = self._export_frame()

        if not self._configure_excel_export_options_fast(target_frame):
            logger.warning("Fast Excel export configure failed — falling back step-by-step")
            self._select_export_sheet_in_frame(target_frame, self.export_sheet_label)
            self._select_flat_cell_style_in_frame(target_frame)

        if not self._ensure_checkbox_checked_in_frame(
            target_frame, self.excel_filter_info_label
        ):
            logger.warning(
                "Could not check %s — dumping export dialog inputs",
                self.excel_filter_info_label,
            )
            self._dump_export_frame_inputs(target_frame)

        if not self._ensure_expand_all_levels_on_export_dialog():
            logger.warning(
                "Could not check %s Row+Column after retries",
                self.excel_expand_all_levels_label,
            )

    def configure_export_options(self) -> None:
        """Set CSV export dialog options before clicking Export."""
        target_frame = self._export_frame_ref
        if target_frame is None:
            for frame in self._iter_export_targets():
                if self._frame_has_export_options(frame) or self._frame_has_export_button(
                    frame
                ):
                    target_frame = frame
                    break
        if target_frame is None:
            target_frame = self._export_frame()

        if not self._configure_export_options_fast(target_frame):
            logger.warning("Fast export configure failed — falling back to sheet select")
            self._select_export_sheet_in_frame(target_frame, self.export_sheet_label)

        for label in (
            self.column_header_label,
            self.row_hierarchy_label,
            self.column_hierarchy_label,
        ):
            if not self._ensure_checkbox_checked_in_frame(target_frame, label):
                logger.warning(
                    "Could not check %s — dumping export dialog inputs",
                    label,
                )
                self._dump_export_frame_inputs(target_frame)
                break

        if not self._select_flat_cell_style_in_frame(target_frame):
            logger.warning(
                "Could not set Cell Style to %s — dumping export dialog inputs",
                self.cell_style_flat_label,
            )
            self._dump_export_frame_inputs(target_frame)

    def _configure_export_options_fast(self, frame) -> bool:
        """Set sheet + checkboxes in one shot using known IQVIA element ids."""
        try:
            result = frame.evaluate(
                """({ sheetName, checkboxIdsByLabel, flatLabel }) => {
                    const check = (id) => {
                        const el = document.getElementById(id);
                        if (!el || el.type !== 'checkbox') return false;
                        if (!el.checked) {
                            el.checked = true;
                            el.click();
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return true;
                    };

                    const checkAny = (ids) => {
                        for (const id of ids) {
                            if (check(id)) return true;
                        }
                        return false;
                    };

                    const selectRadio = (el) => {
                        if (!el || el.type !== 'radio') return false;
                        if (!el.checked) {
                            el.checked = true;
                            el.click();
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return true;
                    };

                    const selectFlat = () => {
                        const matchText = flatLabel.toLowerCase();
                        const knownIds = [
                            'rbFlat',
                            'rdoFlat',
                            'cellStyleFlat',
                        ];
                        for (const id of knownIds) {
                            if (selectRadio(document.getElementById(id))) return true;
                        }
                        for (const label of document.querySelectorAll('label')) {
                            if (label.textContent.trim().toLowerCase() === matchText) {
                                const forId = label.getAttribute('for');
                                const radio = forId
                                    ? document.getElementById(forId)
                                    : label.querySelector('input[type="radio"]');
                                if (selectRadio(radio)) return true;
                            }
                        }
                        for (const radio of document.querySelectorAll('input[type="radio"]')) {
                            const cell = radio.closest('td, tr, div, span, li, p')
                                || radio.parentElement;
                            if (cell && cell.textContent.trim().toLowerCase() === matchText) {
                                if (selectRadio(radio)) return true;
                            }
                        }
                        return false;
                    };

                    const select = document.querySelector('select');
                    let sheetSet = false;
                    if (select) {
                        for (const opt of select.options) {
                            if (opt.text.trim() === sheetName) {
                                select.value = opt.value;
                                select.dispatchEvent(new Event('change', { bubbles: true }));
                                sheetSet = true;
                                break;
                            }
                        }
                    }

                    const checkboxResults = {};
                    for (const [label, ids] of Object.entries(checkboxIdsByLabel)) {
                        checkboxResults[label] = checkAny(ids);
                    }

                    const flat = selectFlat();

                    const options = select
                        ? Array.from(select.options).map((opt) => opt.text.trim())
                        : [];

                    const allChecked = Object.values(checkboxResults).every(Boolean);

                    return {
                        ok: sheetSet && allChecked && flat,
                        sheetSet,
                        checkboxResults,
                        flat,
                        sheetOptions: options,
                    };
                }""",
                {
                    "sheetName": self.export_sheet_label,
                    "checkboxIdsByLabel": {
                        label: list(ids)
                        for label, ids in self._checkbox_known_ids.items()
                    },
                    "flatLabel": self.cell_style_flat_label,
                },
            )
            if result.get("ok"):
                sheet_options = result.get("sheetOptions") or []
                logger.info(
                    "Export options set (sheet=%s, hierarchies + Flat cell style)",
                    self.export_sheet_label,
                )
                if sheet_options:
                    logger.info("Export dialog sheet tabs: %s", ", ".join(sheet_options))
                return True
            logger.warning("Fast export configure partial result: %s", result)
        except Exception as exc:
            logger.warning("Fast export configure error: %s", exc)
        return False

    def check_include_column_header(self) -> None:
        """Backward-compatible alias for configure_export_options()."""
        self.configure_export_options()

    def clickExport(self) -> None:
        frame = self._export_frame_ref or self._export_frame()
        if not self._ensure_expand_all_levels_checked_in_frame(frame):
            self._ensure_expand_all_levels_on_export_dialog()
        export_btn = frame.locator("#btnExport")
        export_btn.wait_for(state="visible", timeout=5_000)
        export_btn.click(timeout=5_000)
        logger.info("Export button clicked")