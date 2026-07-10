"""Block user input in the automation browser (headed mode only)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, Frame, Locator, Page

logger = logging.getLogger(__name__)

BYPASS_SCRIPT = """
(enabled) => {
  window.__rpaGuardBypass = !!enabled;
}
"""

# Silent blocking in every frame — no overlay or banner.
APPLY_GUARD_SCRIPT = """
([enabled]) => {
  if (!window.__rpaGuardInit) {
    window.__rpaGuardInit = true;
    window.__rpaGuardEnabled = false;
    window.__rpaGuardBypass = false;

    const blockIfUser = (event) => {
      if (!window.__rpaGuardEnabled) return;
      if (window.__rpaGuardBypass) return;
      if (!event.isTrusted) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };

    for (const eventName of [
      'pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click',
      'dblclick', 'contextmenu', 'touchstart', 'touchend', 'wheel',
      'keydown', 'keyup', 'keypress',
    ]) {
      document.addEventListener(eventName, blockIfUser, true);
    }
  }

  window.__rpaGuardEnabled = !!enabled;
  if (!enabled) {
    window.__rpaGuardBypass = false;
  }

  return {
    ok: true,
    enabled: !!enabled,
    blocking: !!enabled && !window.__rpaGuardBypass,
    href: location.href.slice(0, 80),
  };
}
"""

GUARD_INIT_SCRIPT = APPLY_GUARD_SCRIPT


class _GuardedLocator:
    """Wrap a Playwright locator so bot actions briefly bypass the input guard."""

    def __init__(self, locator: Locator, guard: "AutomationGuard") -> None:
        self._locator = locator
        self._guard = guard

    def click(self, *args, **kwargs):
        with self._guard.suspend_overlay():
            return self._locator.click(*args, **kwargs)

    def dblclick(self, *args, **kwargs):
        with self._guard.suspend_overlay():
            return self._locator.dblclick(*args, **kwargs)

    def fill(self, *args, **kwargs):
        with self._guard.suspend_overlay():
            return self._locator.fill(*args, **kwargs)

    def press(self, *args, **kwargs):
        with self._guard.suspend_overlay():
            return self._locator.press(*args, **kwargs)

    def check(self, *args, **kwargs):
        with self._guard.suspend_overlay():
            return self._locator.check(*args, **kwargs)

    def uncheck(self, *args, **kwargs):
        with self._guard.suspend_overlay():
            return self._locator.uncheck(*args, **kwargs)

    def select_option(self, *args, **kwargs):
        with self._guard.suspend_overlay():
            return self._locator.select_option(*args, **kwargs)

    def __getattr__(self, name: str):
        if name == "first":
            return _GuardedLocator(self._locator.first, self._guard)
        if name == "last":
            return _GuardedLocator(self._locator.last, self._guard)
        if name == "nth":
            return lambda index: _GuardedLocator(self._locator.nth(index), self._guard)
        value = getattr(self._locator, name)
        if name in ("locator", "get_by_role", "get_by_text", "get_by_label", "filter"):
            return lambda *args, **kwargs: _GuardedLocator(
                value(*args, **kwargs), self._guard
            )
        return value


class _GuardedFrameLocator:
    def __init__(self, frame_locator, guard: "AutomationGuard") -> None:
        self._frame_locator = frame_locator
        self._guard = guard

    def locator(self, *args, **kwargs):
        return _GuardedLocator(self._frame_locator.locator(*args, **kwargs), self._guard)

    def get_by_role(self, *args, **kwargs):
        return _GuardedLocator(
            self._frame_locator.get_by_role(*args, **kwargs), self._guard
        )

    def get_by_text(self, *args, **kwargs):
        return _GuardedLocator(
            self._frame_locator.get_by_text(*args, **kwargs), self._guard
        )

    def get_by_label(self, *args, **kwargs):
        return _GuardedLocator(
            self._frame_locator.get_by_label(*args, **kwargs), self._guard
        )

    def __getattr__(self, name: str):
        return getattr(self._frame_locator, name)


class AutomationGuard:
    """Silently block real user input in every frame."""

    def __init__(self, context: BrowserContext, *, headless: bool) -> None:
        self._context = context
        self._headless = headless
        self._active = False
        self._bypass_depth = 0
        if headless:
            return

        context.add_init_script(
            f"() => {{ ({APPLY_GUARD_SCRIPT})([false]); }}"
        )
        context.on("page", self._on_page)

    def _on_page(self, page: Page) -> None:
        page.on("framenavigated", self._on_frame_navigated)
        page.on("frameattached", self._on_frame_attached)
        self._patch_page_input(page)
        if self._active:
            self.refresh()

    def _on_frame_navigated(self, _frame: Frame) -> None:
        if self._active:
            self.refresh()

    def _on_frame_attached(self, frame: Frame) -> None:
        self._patch_frame_input(frame)
        if self._active:
            self._apply_frame(frame)

    def enable(self) -> None:
        if self._headless:
            return
        self._active = True
        for page in self._context.pages:
            self._patch_page_input(page)
        applied = self.refresh()
        logger.info("Automation guard enabled — blocked %d frame(s)", applied)

    def disable(self) -> None:
        if self._headless:
            return
        self._active = False
        self._bypass_depth = 0
        self._set_bypass(False)
        self.refresh()
        logger.info("Automation guard disabled — user can interact with the browser")

    def refresh(self) -> int:
        if self._headless:
            return 0
        applied = 0
        for page in self._context.pages:
            self._patch_page_input(page)
            applied += self._apply_page(page)
        return applied

    def _apply_page(self, page: Page) -> int:
        count = 0
        for frame in page.frames:
            if self._apply_frame(frame):
                count += 1
        return count

    def _apply_frame(self, frame: Frame) -> bool:
        try:
            if frame.is_detached():
                return False
        except Exception:
            return False
        try:
            result = frame.evaluate(APPLY_GUARD_SCRIPT, [self._active])
            if self._active and self._bypass_depth > 0:
                frame.evaluate(BYPASS_SCRIPT, True)
            return bool(result and result.get("ok"))
        except Exception as exc:
            url = getattr(frame, "url", "") or ""
            if self._active:
                logger.debug(
                    "Could not apply guard in frame %s: %s",
                    url[:80],
                    exc,
                )
            return False

    def _set_bypass(self, enabled: bool) -> None:
        for page in self._context.pages:
            for frame in page.frames:
                try:
                    frame.evaluate(BYPASS_SCRIPT, enabled)
                except Exception:
                    pass

    def _patch_frame_input(self, frame: Frame) -> None:
        if self._headless or getattr(frame, "_rpaLocatorPatched", False):
            return
        frame._rpaLocatorPatched = True
        guard = self
        original = frame.locator

        def patched_locator(*args, **kwargs):
            return _GuardedLocator(original(*args, **kwargs), guard)

        frame.locator = patched_locator  # type: ignore[method-assign]

    def _patch_page_input(self, page: Page) -> None:
        for frame in page.frames:
            self._patch_frame_input(frame)

        if self._headless or getattr(page, "_rpaInputPatched", False):
            return
        page._rpaInputPatched = True
        guard = self
        page._rpaGuard = guard
        mouse = page.mouse
        keyboard = page.keyboard

        for method_name in ("click", "dblclick", "down", "up", "move"):
            original = getattr(mouse, method_name)

            def make_mouse_patched(orig, _guard=guard):
                def patched(*args, **kwargs):
                    with _guard.suspend_overlay():
                        return orig(*args, **kwargs)

                return patched

            setattr(mouse, method_name, make_mouse_patched(original))

        for method_name in ("press", "type", "down", "up", "insert_text"):
            original = getattr(keyboard, method_name)

            def make_keyboard_patched(orig, _guard=guard):
                def patched(*args, **kwargs):
                    with _guard.suspend_overlay():
                        return orig(*args, **kwargs)

                return patched

            setattr(keyboard, method_name, make_keyboard_patched(original))

        original_locator = page.locator

        def patched_locator(*args, **kwargs):
            return _GuardedLocator(original_locator(*args, **kwargs), guard)

        page.locator = patched_locator  # type: ignore[method-assign]

        original_frame_locator = page.frame_locator

        def patched_frame_locator(*args, **kwargs):
            return _GuardedFrameLocator(original_frame_locator(*args, **kwargs), guard)

        page.frame_locator = patched_frame_locator  # type: ignore[method-assign]

    @contextmanager
    def suspend_overlay(self) -> Iterator[None]:
        """Briefly allow Playwright input through the guard in every frame."""
        if self._headless:
            yield
            return
        self._bypass_depth += 1
        if self._bypass_depth == 1:
            self._set_bypass(True)
        try:
            yield
        finally:
            self._bypass_depth = max(0, self._bypass_depth - 1)
            if self._bypass_depth == 0:
                self._set_bypass(False)

    @contextmanager
    def bypass(self) -> Iterator[None]:
        with self.suspend_overlay():
            yield
