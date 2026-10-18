"""
The Windows implementation of the pipeline's Platform interface, plus the
bridge that lets any thread hand work to the Tk (main) thread.
"""
from __future__ import annotations

import logging
import queue
import tkinter as tk
from typing import Any, Callable, Optional

from scribe_plus.pipeline import AppContext, Platform
from scribe_plus.settings import SettingsStore, resolve_style

log = logging.getLogger(__name__)


class UiBridge:
    """Thread-safe queue drained on the Tk thread every 20 ms."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._q: "queue.SimpleQueue[tuple[Callable, tuple, dict]]" = queue.SimpleQueue()
        self.root.after(20, self._drain)

    def call(self, fn: Callable, *args: Any, **kw: Any) -> None:
        self._q.put((fn, args, kw))

    def _drain(self) -> None:
        for _ in range(200):
            try:
                fn, args, kw = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kw)
            except Exception:
                log.exception("ui call failed")
        self.root.after(20, self._drain)


class WinPlatform(Platform):
    def __init__(self, settings: SettingsStore, bridge: UiBridge, pill,
                 notifier: Callable[[str, str], None],
                 tray_state: Optional[Callable[[str], None]] = None) -> None:
        self.settings = settings
        self.bridge = bridge
        self.pill = pill
        self._notify = notifier
        self._tray_state = tray_state or (lambda state: None)

    # imports kept local so this module imports cleanly in tests
    def capture_context(self) -> AppContext:
        from scribe_plus.win import context
        hwnd, proc, title = context.foreground()
        style = resolve_style(proc, title, self.settings.get("app_rules") or [])
        return AppContext(hwnd=hwnd, process=proc, title=title, style=style)

    def inject_mask(self) -> None:
        from scribe_plus.win import keys
        try:
            keys.send_mask()
        except Exception as exc:
            log.warning("mask key failed: %s", exc)

    def wait_modifiers_released(self, timeout: float) -> bool:
        from scribe_plus.win import clipboard
        return clipboard.wait_modifiers_released(timeout)

    def paste(self, text: str, restore: Optional[str] = None) -> None:
        from scribe_plus.win import clipboard
        clipboard.paste_text(
            text, restore=restore,
            method=str(self.settings.get("paste_method") or "clipboard"),
            restore_delay_ms=int(self.settings.get("clipboard_restore_delay_ms") or 350),
        )

    def capture_selection(self) -> tuple[str, Optional[str]]:
        from scribe_plus.win import clipboard
        return clipboard.capture_selection()

    def restore_clipboard(self, text: Optional[str]) -> None:
        from scribe_plus.win import clipboard
        clipboard.restore(text)

    def copy(self, text: str) -> None:
        from scribe_plus.win import clipboard
        clipboard.copy(text)

    def notify(self, title: str, message: str) -> None:
        try:
            self._notify(title, message)
        except Exception:
            log.info("notify: %s: %s", title, message)

    def ui(self, state: str, **kw: Any) -> None:
        self.bridge.call(self.pill.set_state, state, **kw)
        if state != "level":
            self._tray_state(state)
