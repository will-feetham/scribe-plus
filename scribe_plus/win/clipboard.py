"""
Pasting into the focused app, and reading the current selection.

Same approach as upstream Scribe (pyperclip + synthetic Ctrl+V, previous
clipboard text restored afterwards), plus:
  * waits for the hotkey's modifiers to be physically released first, so
    the synthetic Ctrl+V can't become Win+Ctrl+V (which opens a Windows
    panel) or Ctrl+Alt+V (Paste Special in Office)
  * restores the old clipboard only if nothing else replaced ours meanwhile
  * CRLF line endings, which every Windows app accepts
  * Command Mode's selection read through a sentinel, so "nothing selected"
    can't be confused with "selection equals the old clipboard"

Like upstream, only text survives the clipboard round trip; an image on the
clipboard before a dictation is replaced by the dictated text.
"""
from __future__ import annotations

import logging
import time
import uuid

import pyperclip
from pynput import keyboard

from scribe_plus.win.keys import controller, physically_down

log = logging.getLogger(__name__)

VK_C, VK_V = 0x43, 0x56
_MODIFIER_VKS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)


def modifiers_down() -> bool:
    return any(physically_down(vk) for vk in _MODIFIER_VKS)


def wait_modifiers_released(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while modifiers_down():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.015)
    return True


def _get() -> str:
    try:
        return pyperclip.paste() or ""
    except Exception as exc:
        log.debug("clipboard read failed: %s", exc)
        return ""


def _set(text: str) -> None:
    pyperclip.copy(text)


def _chord(vk: int) -> None:
    c = controller()
    key = keyboard.KeyCode.from_vk(vk)
    c.press(keyboard.Key.ctrl)
    try:
        c.press(key)
        c.release(key)
    finally:
        c.release(keyboard.Key.ctrl)


def to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def paste_text(text: str, *, restore: str | None = None, method: str = "clipboard",
               restore_delay_ms: int = 350) -> None:
    """Put `text` into the focused field."""
    if method == "type":
        controller().type(text)
        return
    saved = restore if restore is not None else _get()
    payload = to_crlf(text)
    try:
        _set(payload)
        time.sleep(0.03)
        _chord(VK_V)
        time.sleep(max(0, restore_delay_ms) / 1000.0)
    finally:
        try:
            # Only put the old text back if the clipboard still holds ours.
            if saved and _get() in (payload, text):
                _set(saved)
        except Exception as exc:
            log.warning("clipboard restore failed: %s", exc)


def restore(saved: str | None) -> None:
    if saved:
        try:
            _set(saved)
        except Exception as exc:
            log.warning("clipboard restore failed: %s", exc)


def copy(text: str) -> None:
    try:
        _set(to_crlf(text))
    except Exception as exc:
        log.warning("clipboard copy failed: %s", exc)


def capture_selection(timeout: float = 0.45) -> tuple[str, str]:
    """Return (selected text or "", clipboard text to restore later)."""
    saved = _get()
    sentinel = f"⁣scribe-plus-{uuid.uuid4().hex}⁣"
    try:
        _set(sentinel)
    except Exception as exc:
        log.warning("clipboard unavailable for selection read: %s", exc)
        return "", saved
    _chord(VK_C)
    deadline = time.monotonic() + timeout
    selected = ""
    while time.monotonic() < deadline:
        time.sleep(0.025)
        current = _get()
        if current != sentinel:
            selected = current
            break
    if not selected:
        # Leave the user's clipboard as it was (never strand the sentinel).
        try:
            _set(saved)
        except Exception as exc:
            log.warning("clipboard restore failed: %s", exc)
    return selected.replace("\r\n", "\n"), saved
