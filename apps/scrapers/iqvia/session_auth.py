"""Shared IQVIA session/login helpers for headed and headless runs."""

from __future__ import annotations

import logging

from apps.core.utils.read_utils import ReadConfig
from apps.scrapers.iqvia.page_locators import LoginPage, _poll_until

logger = logging.getLogger(__name__)

DECISION_CENTER_TITLE = "Decision Center"
LOGIN_TITLE = "Login"
VERIFICATION_CODE_TITLE = LoginPage.VERIFICATION_CODE_TITLE
INCOMPLETE_LOGIN_TITLES = frozenset({LOGIN_TITLE, VERIFICATION_CODE_TITLE})


class IqviaSessionAuthMixin:
    """Login/session flow shared by IQVIA bot variants."""

    headless: bool
    entry_url: str
    page: object | None
    guard: object | None

    def _is_authenticated(self) -> bool:
        if self.page is None:
            return False
        try:
            return self.page.title() == DECISION_CENTER_TITLE
        except Exception:
            return False

    def _try_restore_session(self) -> bool:
        if not self.auth.auth_file_exists(self._scraper_name) or self.page is None:
            return False
        if self._is_authenticated():
            return True

        logger.info(
            "Auth file present — reloading %s with saved session…",
            self.entry_url,
        )
        self.page.goto(
            self.entry_url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        self.page.wait_for_load_state("domcontentloaded")
        if self._is_authenticated():
            logger.info("Saved session accepted — skipping login")
            return True

        logger.warning(
            "Saved session not accepted (title: %r) — login/OTP required",
            self.page.title(),
        )
        return False

    def _ensure_authenticated(self) -> None:
        if self._is_authenticated():
            self._persist_auth_session()
            return

        if self._try_restore_session():
            self._persist_auth_session()
            self._refresh_automation_guard()
            return

        if self.headless and not ReadConfig.getOtp().strip():
            self._interactive_login_for_headless()
            return

        logger.info("Login required — signing in…")
        self._perform_login(LoginPage(self.page))
        self._wait_for_decision_center(timeout_sec=180)
        self._persist_auth_session()
        self._refresh_automation_guard()

    def _interactive_login_for_headless(self) -> None:
        """Open a visible browser once so the user can enter OTP without .env."""
        logger.info(
            "Headless run needs verification but IQVIA_OTP is not set — "
            "opening a visible browser window. Enter the OTP there; the session "
            "will be saved for future headless runs."
        )
        self._teardown_browser(persist_auth=False)
        was_headless = self.headless
        self.headless = False
        try:
            self._open_browser_with_auth()
            self._navigate_to_entry_url()
            self._perform_login(LoginPage(self.page))
            self._wait_for_decision_center(timeout_sec=300)
            self._persist_auth_session()
        finally:
            self._teardown_browser(persist_auth=True)

        if not was_headless:
            return

        self.headless = True
        self._open_browser_with_auth()
        self._navigate_to_entry_url()
        if not self._is_authenticated():
            raise RuntimeError(
                "Session still not valid in headless after interactive login "
                f"(title: {self.page.title()!r})"
            )
        logger.info("Headless browser resumed with saved session")
        self._persist_auth_session()
        self._refresh_automation_guard()

    def _wait_for_decision_center(self, timeout_sec: int = 180) -> None:
        if self.page is None:
            raise RuntimeError("Browser page not initialized")

        page = self.page
        if _poll_until(
            page,
            lambda: page.title() == DECISION_CENTER_TITLE,
            timeout_ms=timeout_sec * 1_000,
            poll_ms=200,
        ):
            logger.info("Decision Center loaded")
            return

        raise TimeoutError(
            f"Decision Center (title {DECISION_CENTER_TITLE!r}) did not load "
            f"within {timeout_sec}s — current title: {page.title()!r}"
        )

    def _teardown_browser(self, *, persist_auth: bool = False) -> None:
        if persist_auth:
            self._persist_auth_session()
        if self.guard:
            self.guard.disable()
            self.guard = None
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser and self.browser.is_connected():
            self.browser.close()
        self.browser = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None
        self.page = None
