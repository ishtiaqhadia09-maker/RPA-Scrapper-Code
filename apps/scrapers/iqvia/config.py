"""Browser config for the IQVIA bot."""

from __future__ import annotations

from typing import Any

from apps.core.utils.read_utils import ReadConfig

DEFAULT_ENTRY_URL = "https://hub.bi.iqvia.com/iam/"
HEADLESS_VIEWPORT = {"width": 1920, "height": 1080}
# Same UA in headed and headless so auth/iqvia_auth.json works in both modes.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Playwright timing — tuned for ~15 min single-product runs (was ~30+ min).
FRAME_POLL_MS = 50
SETTLE_MS = 30
POLL_MS = 75
SLOW_POLL_MS = 150
QUERY_IDLE_POLL_MS = 100
QUERY_EXPORT_POLL_MS = 100
QUERY_EXPORT_STABLE_CHECKS = 2
QUERY_EXPORT_MAX_WAIT_MS = 900_000
DIALOG_SETTLE_MS = 350
COLUMN_EXPAND_PAUSE_MS = 200
DOWNLOAD_TIMEOUT_MS = 900_000


def get_entry_url() -> str:
    """Same entry URL used by the download bot (APP_URL in .env)."""
    ReadConfig.reload()
    app_url = ReadConfig.getAppURL().strip()
    return app_url or DEFAULT_ENTRY_URL


def chromium_launch_kwargs(*, headless: bool) -> dict[str, Any]:
    """Launch options shared by headed and headless runs."""
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-popup-blocking",
    ]
    if not headless:
        args.append("--start-maximized")
    return {"headless": headless, "args": args}


def browser_context_kwargs(*, headless: bool, **extra: Any) -> dict[str, Any]:
    """Context options — same fingerprint in headed and headless for session reuse."""
    kwargs: dict[str, Any] = dict(extra)
    kwargs.setdefault("user_agent", CHROME_USER_AGENT)
    kwargs.setdefault("locale", "en-US")
    if headless:
        kwargs.setdefault("viewport", HEADLESS_VIEWPORT)
    else:
        kwargs["no_viewport"] = True
    return kwargs
