"""
Which app is the user dictating into? (frontmost app and its window title)

The app is matched by bundle identifier (com.tinyspeck.slackmacgap for
Slack, and so on). The window title, read through the Accessibility API,
lets rules catch web apps in a browser tab ("Gmail", "ChatGPT").
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _window_title(pid: int) -> str:
    try:
        from ApplicationServices import (AXUIElementCopyAttributeValue,
                                         AXUIElementCreateApplication,
                                         kAXFocusedWindowAttribute, kAXTitleAttribute)
        app = AXUIElementCreateApplication(pid)
        err, window = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, None)
        if err or window is None:
            return ""
        err, title = AXUIElementCopyAttributeValue(window, kAXTitleAttribute, None)
        return str(title) if not err and title else ""
    except Exception as exc:
        log.debug("window title lookup failed: %s", exc)
        return ""


def foreground() -> tuple[int, str, str]:
    """(pid, bundle id or app name in lower case, window title)."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return 0, "", ""
        pid = int(app.processIdentifier())
        key = str(app.bundleIdentifier() or app.localizedName() or "").lower()
        return pid, key, _window_title(pid)
    except Exception as exc:
        log.debug("frontmost app lookup failed: %s", exc)
        return 0, "", ""
