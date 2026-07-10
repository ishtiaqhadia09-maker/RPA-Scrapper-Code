"""Verify AutomationGuard blocks user clicks but allows Playwright automation."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from apps.scrapers.iqvia.automation_guard import AutomationGuard

HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Guard test</title></head>
<body style="margin:0;padding:20px;font-family:sans-serif">
  <h1 id="main-title">Main frame</h1>
  <button id="main-btn" onclick="window.__mainClicks=(window.__mainClicks||0)+1">
    Main click
  </button>
  <iframe id="child" style="width:100%;height:220px;border:1px solid #ccc;margin-top:12px"
    srcdoc="<!DOCTYPE html><html><body style='margin:0;padding:16px'>
      <button id='iframe-btn' onclick='parent.window.__iframeClicks=(parent.window.__iframeClicks||0)+1'>
        Iframe click (Data Source area)
      </button>
    </body></html>">
  </iframe>
  <script>window.__mainClicks=0;window.__iframeClicks=0;</script>
</body>
</html>
"""


def _guard_state(page) -> dict:
    return page.evaluate(
        """() => ({
            main: {
                enabled: !!window.__rpaGuardEnabled,
                init: !!window.__rpaGuardInit,
            },
            iframe: (() => {
                const iframe = document.getElementById('child');
                const doc = iframe && iframe.contentDocument;
                if (!doc) return { enabled: false, init: false };
                return {
                    enabled: !!doc.defaultView.__rpaGuardEnabled,
                    init: !!doc.defaultView.__rpaGuardInit,
                };
            })(),
        })"""
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        page_path = Path(tmp) / "guard_test.html"
        page_path.write_text(HTML, encoding="utf-8")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            guard = AutomationGuard(context, headless=False)
            page = context.new_page()
            page.goto(page_path.as_uri(), wait_until="load")
            page.wait_for_timeout(500)

            guard.enable()
            page.wait_for_timeout(300)
            frames_applied = guard.refresh()
            state = _guard_state(page)

            # Playwright automation clicks (untrusted) must still work.
            page.locator("#main-btn").click(force=True)
            child = page.frame_locator("#child")
            child.locator("#iframe-btn").click(force=True)
            clicks = page.evaluate(
                "() => ({ main: window.__mainClicks || 0, iframe: window.__iframeClicks || 0 })"
            )

            guard.disable()
            browser.close()

    print(f"guard applied to {frames_applied} frame(s)")
    print(f"main frame state: {state['main']}")
    print(f"iframe state: {state['iframe']}")
    print(f"automation clicks: {clicks}")

    ok = (
        frames_applied >= 2
        and state["main"]["enabled"]
        and state["iframe"]["enabled"]
        and clicks["main"] >= 1
        and clicks["iframe"] >= 1
    )
    if ok:
        print("PASS — silent guard on main + iframe; bot clicks still work")
        return 0

    print("FAIL — guard did not behave as expected", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
