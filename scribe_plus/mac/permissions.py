"""
The three macOS privacy permissions a dictation app needs, and how to ask.

  Input Monitoring  to see the hotkey (the keyboard event tap)
  Accessibility     to post Command+V / Command+C, and to swallow Esc
  Microphone        asked for automatically the first time it records
"""
from __future__ import annotations

import ctypes
import logging
import subprocess

log = logging.getLogger(__name__)

PANES = {
    "input": "Privacy_ListenEvent",
    "accessibility": "Privacy_Accessibility",
    "microphone": "Privacy_Microphone",
}
_LISTEN_EVENT = 1  # kIOHIDRequestTypeListenEvent
_iokit = None


def _iokit_lib():
    global _iokit
    if _iokit is None:
        _iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        _iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        _iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
        _iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
        _iokit.IOHIDRequestAccess.restype = ctypes.c_bool
    return _iokit


def input_monitoring_granted() -> bool:
    try:
        return _iokit_lib().IOHIDCheckAccess(_LISTEN_EVENT) == 0  # kIOHIDAccessTypeGranted
    except Exception as exc:
        log.debug("Input Monitoring check failed: %s", exc)
        return True


def request_input_monitoring() -> None:
    """Shows the system prompt (once); afterwards only Settings can change it."""
    try:
        _iokit_lib().IOHIDRequestAccess(_LISTEN_EVENT)
    except Exception as exc:
        log.debug("Input Monitoring request failed: %s", exc)


def accessibility_granted(prompt: bool = False) -> bool:
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: bool(prompt)}))
    except Exception as exc:
        log.debug("Accessibility check failed: %s", exc)
        return True


def open_pane(which: str) -> None:
    pane = PANES.get(which, which)
    url = f"x-apple.systempreferences:com.apple.preference.security?{pane}"
    try:
        subprocess.Popen(["open", url])
    except Exception as exc:
        log.warning("could not open System Settings: %s", exc)


def missing() -> list[str]:
    out = []
    if not input_monitoring_granted():
        out.append("Input Monitoring")
    if not accessibility_granted():
        out.append("Accessibility")
    return out
