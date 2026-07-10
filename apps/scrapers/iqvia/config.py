"""Browser config for the IQVIA bot."""

from __future__ import annotations

from typing import Any

from apps.core.utils.read_utils import ReadConfig

DEFAULT_ENTRY_URL = "https://hub.bi.iqvia.com/iam/"
HEADLESS_VIEWPORT = {"width": 1920, "height": 1080}


def get_entry_url() -> str:
    """Same entry URL used by the download bot (APP_URL in .env)."""
    ReadConfig.reload()
    app_url = ReadConfig.getAppURL().strip()
    return app_url or DEFAULT_ENTRY_URL


def chromium_launch_kwargs(*, headless: bool) -> dict[str, Any]:
    """Launch options so headed runs fill the actual browser window."""
    kwargs: dict[str, Any] = {"headless": headless}
    if not headless:
        kwargs["args"] = ["--start-maximized", "--disable-popup-blocking"]
    return kwargs


def browser_context_kwargs(*, headless: bool, **extra: Any) -> dict[str, Any]:
    """Context options — avoid fixed 1280x720 viewport in headed mode."""
    kwargs: dict[str, Any] = dict(extra)
    if headless:
        kwargs.setdefault("viewport", HEADLESS_VIEWPORT)
    else:
        kwargs["no_viewport"] = True
    return kwargs
