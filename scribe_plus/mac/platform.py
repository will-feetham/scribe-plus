"""
The Mac implementation of the pipeline's Platform interface, plus the
bridge that hands work from any thread to the main (AppKit) thread.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from scribe_plus.pipeline import AppContext, Platform
from scribe_plus.settings import SettingsStore, resolve_style

log = logging.getLogger(__name__)


class MainBridge:
    """Runs callables on the main thread's run loop, in order."""

    def call(self, fn: Callable, *args: Any, **kw: Any) -> None:
        from PyObjCTools.AppHelper import callAfter

        def run() -> None:
            try:
                fn(*args, **kw)
            except Exception:
                log.exception("ui call failed")
        callAfter(run)


class MacPlatform(Platform):
    paste_hint = "⌘V"

    def __init__(self, settings: SettingsStore, bridge: MainBridge, pill,
                 notifier: Callable[[str, str], None],
                 tray_state: Optional[Callable[[str], None]] = None) -> None:
        self.settings = settings
        self.bridge = bridge
        self.pill = pill
        self._notify = notifier
        self._tray_state = tray_state or (lambda state: None)

    def capture_context(self) -> AppContext:
        from scribe_plus.mac import context
        pid, app, title = context.foreground()
        style = resolve_style(app, title, self.settings.get("app_rules") or [])
        return AppContext(hwnd=pid, process=app, title=title, style=style)

    def inject_mask(self) -> None:
        pass  # Windows-only concern (Start menu); nothing to do on a Mac

    def wait_modifiers_released(self, timeout: float) -> bool:
        from scribe_plus.mac import clipboard
        return clipboard.wait_modifiers_released(timeout)

    def paste(self, text: str, restore=None) -> None:
        from scribe_plus.mac import clipboard
        clipboard.paste_text(
            text, restore=restore,
            restore_delay_ms=int(self.settings.get("clipboard_restore_delay_ms") or 350))

    def capture_selection(self):
        from scribe_plus.mac import clipboard
        return clipboard.capture_selection()

    def restore_clipboard(self, saved) -> None:
        from scribe_plus.mac import clipboard
        clipboard.restore_snapshot(saved)

    def copy(self, text: str) -> None:
        from scribe_plus.mac import clipboard
        clipboard.copy(text)

    def notify(self, title: str, message: str) -> None:
        try:
            self._notify(title, message)
        except Exception:
            log.info("notify: %s: %s", title, message)

    def ui(self, state: str, **kw: Any) -> None:
        self.bridge.call(self.pill.set_state, state, **kw)
        if state != "level":
            self.bridge.call(self._tray_state, state)
