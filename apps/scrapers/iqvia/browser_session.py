"""Launch IQVIA automation with a real Chrome profile or existing Chrome window."""

from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from apps.core.paths import AUTH_DIR
from apps.core.utils.read_utils import ReadConfig
from apps.scrapers.iqvia.config import browser_context_kwargs, chromium_launch_kwargs

logger = logging.getLogger(__name__)

IQVIA_CHROME_PROFILE_DIR = AUTH_DIR / "iqvia_chrome_profile"


def use_chrome_profile() -> bool:
    """Use auth/iqvia_chrome_profile (keeps OTP / trust-this-device like Chrome)."""
    ReadConfig.reload()
    raw = ReadConfig.get("IQVIA_USE_CHROME_PROFILE", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def use_chrome_channel() -> bool:
    """Launch installed Google Chrome instead of Playwright Chromium."""
    ReadConfig.reload()
    raw = ReadConfig.get("IQVIA_USE_CHROME_CHANNEL", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def get_chrome_cdp_url() -> str:
    """Optional: attach to Chrome started with --remote-debugging-port=9222."""
    ReadConfig.reload()
    return ReadConfig.get("IQVIA_CHROME_CDP_URL", "").strip()


def open_browser_with_auth(bot) -> None:
    """Start Playwright and attach browser/context/page on *bot*."""
    bot.playwright = sync_playwright().start()
    bot.browser = None
    bot.context = None
    bot.page = None
    bot._uses_chrome_profile = False
    bot._persistent_context = False
    bot._cdp_connected = False

    cdp_url = get_chrome_cdp_url()
    if cdp_url:
        _open_via_cdp(bot, cdp_url)
    elif use_chrome_profile():
        _open_persistent_chrome_profile(bot)
    else:
        _open_with_storage_state(bot)

    bot._start_automation_guard()
    logger.info("Step 1 complete — browser ready (navigation not started yet)")


def _open_via_cdp(bot, cdp_url: str) -> None:
    logger.info("Step 1 — connecting to your open Chrome at %s", cdp_url)
    bot.browser = bot.playwright.chromium.connect_over_cdp(cdp_url)
    bot._cdp_connected = True
    if bot.browser.contexts:
        bot.context = bot.browser.contexts[0]
    else:
        bot.context = bot.browser.new_context(
            **browser_context_kwargs(headless=bot.headless, accept_downloads=True)
        )
    bot.page = bot.context.pages[0] if bot.context.pages else bot.context.new_page()
    bot._configure_browser_downloads()
    logger.info("Step 1 — using existing Chrome window/tab (your IQVIA login)")


def _open_persistent_chrome_profile(bot) -> None:
    IQVIA_CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    launch_kwargs = chromium_launch_kwargs(headless=bot.headless)
    context_kwargs = browser_context_kwargs(
        headless=bot.headless,
        accept_downloads=True,
    )
    persistent_kwargs = {**context_kwargs, **launch_kwargs}
    if use_chrome_channel():
        persistent_kwargs["channel"] = "chrome"

    logger.info(
        "Step 1 — opening Chrome profile at %s (headless=%s, channel=%s)",
        IQVIA_CHROME_PROFILE_DIR,
        bot.headless,
        "chrome" if use_chrome_channel() else "chromium",
    )
    bot.context = bot.playwright.chromium.launch_persistent_context(
        str(IQVIA_CHROME_PROFILE_DIR),
        **persistent_kwargs,
    )
    bot._uses_chrome_profile = True
    bot._persistent_context = True
    bot.page = bot.context.pages[0] if bot.context.pages else bot.context.new_page()
    bot._configure_browser_downloads()


def _open_with_storage_state(bot) -> None:
    bot.browser = bot.playwright.chromium.launch(
        **chromium_launch_kwargs(headless=bot.headless)
    )
    auth_path = bot.auth.get_auth_path(bot._scraper_name)
    storage_state = bot.auth.get_storage_state_path(bot._scraper_name)
    context_kwargs = browser_context_kwargs(
        headless=bot.headless,
        accept_downloads=True,
    )

    logger.info("Step 1 — opening browser (downloads → %s)", bot.download_dir)
    if storage_state:
        logger.info("Step 1 — loading auth file: %s", auth_path)
        bot.context = bot.browser.new_context(
            storage_state=storage_state,
            **context_kwargs,
        )
    else:
        logger.warning(
            "Step 1 — no auth file at %s; browser starts without saved session",
            auth_path,
        )
        bot.context = bot.browser.new_context(**context_kwargs)

    bot.page = bot.context.new_page()
    bot._configure_browser_downloads()


def teardown_browser(bot, *, persist_auth: bool = False) -> None:
    if persist_auth:
        bot._persist_auth_session()
    if bot.guard:
        bot.guard.disable()
        bot.guard = None
    if bot.context and not getattr(bot, "_cdp_connected", False):
        try:
            bot.context.close()
        except Exception:
            pass
    if bot.browser and bot.browser.is_connected():
        try:
            bot.browser.close()
        except Exception:
            pass
    bot.context = None
    bot.browser = None
    bot.page = None
    if bot.playwright:
        bot.playwright.stop()
        bot.playwright = None
