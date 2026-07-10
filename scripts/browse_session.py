"""
Remove this file before deployment

Run from the project root::

    python scripts/browse_session.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from apps.scrapers.iqvia.config import (  # noqa: E402
    browser_context_kwargs,
    chromium_launch_kwargs,
    get_entry_url,
)
from apps.scrapers.iqvia.page_locators import _poll_until  # noqa: E402

AUTH_PATH = _ROOT / "auth" / "iqvia_auth.json"


def open_browser_with_session() -> None:
    if not AUTH_PATH.is_file():
        print(
            f"[ERROR] No auth session found at {AUTH_PATH}\n"
            "Run the download bot once first (it saves the session after OTP login)."
        )
        sys.exit(1)

    entry_url = get_entry_url()
    print(f"[browse-session] Auth file : {AUTH_PATH}")
    print(f"[browse-session] Opening   : {entry_url}")
    print(
        "[browse-session] Manual mode — browse freely, close the window when done."
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_launch_kwargs(headless=False))
        context = browser.new_context(
            storage_state=str(AUTH_PATH),
            **browser_context_kwargs(headless=False),
        )
        page = context.new_page()
        page.goto(entry_url, wait_until="domcontentloaded", timeout=120_000)
        _poll_until(
            page,
            lambda: page.title() in ("Decision Center", "Login"),
            timeout_ms=120_000,
            poll_ms=200,
        )
        title = page.title()
        print(f"[browse-session] Page title: {title!r}")
        if title.lower() == "login":
            print(
                "[browse-session] WARNING — login page shown.\n"
                "Session may be expired; run Download once to refresh auth."
            )
        while browser.is_connected():
            time.sleep(0.5)

    print("[browse-session] Browser closed.")


if __name__ == "__main__":
    open_browser_with_session()
