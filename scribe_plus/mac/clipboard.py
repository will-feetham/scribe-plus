"""
Pasting into the frontmost app on a Mac, and reading the current selection.

Like Scribe on a Mac, the whole pasteboard (text, images, files) is
snapshotted and put back after the paste, so nothing you had copied is
lost. Two extra guards:
  * waits for the hotkey's modifiers to be physically released first, so
    the synthetic Command+V can't turn into Option+Command+V
  * restores the old contents only if nothing else replaced ours meanwhile
    (the pasteboard's change count tells us)
"""
from __future__ import annotations

import logging
import time
import uuid

from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString
from Foundation import NSData

from scribe_plus.mac import keymap
from scribe_plus.mac.keys import modifiers_down, post_command_key

log = logging.getLogger(__name__)

Snapshot = list  # [{type: bytes}, ...], one dict per pasteboard item


def _pb():
    return NSPasteboard.generalPasteboard()


def get_text() -> str:
    try:
        s = _pb().stringForType_(NSPasteboardTypeString)
        return str(s) if s is not None else ""
    except Exception as exc:
        log.debug("pasteboard read failed: %s", exc)
        return ""


def set_text(text: str) -> int:
    pb = _pb()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    return int(pb.changeCount())


def change_count() -> int:
    return int(_pb().changeCount())


def snapshot() -> Snapshot:
    snap: Snapshot = []
    try:
        for item in _pb().pasteboardItems() or []:
            blob = {}
            for t in list(item.types() or []):
                data = item.dataForType_(t)
                if data is not None:
                    try:
                        blob[str(t)] = bytes(data)
                    except Exception:
                        pass
            if blob:
                snap.append(blob)
    except Exception as exc:
        log.debug("pasteboard snapshot failed: %s", exc)
    return snap


def restore_snapshot(snap: Snapshot | None) -> None:
    if not snap:
        return
    try:
        pb = _pb()
        pb.clearContents()
        items = []
        for blob in snap:
            item = NSPasteboardItem.alloc().init()
            for t, data in blob.items():
                item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), t)
            items.append(item)
        pb.writeObjects_(items)
    except Exception as exc:
        log.warning("pasteboard restore failed: %s", exc)


def wait_modifiers_released(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while modifiers_down():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.015)
    return True


def paste_text(text: str, *, restore: Snapshot | None = None, restore_delay_ms: int = 350,
               **_ignored) -> None:
    saved = restore if restore is not None else snapshot()
    ours = set_text(text)
    try:
        time.sleep(0.03)
        post_command_key(keymap.KC_V)
        time.sleep(max(0, restore_delay_ms) / 1000.0)
    finally:
        # Only put the old contents back if the pasteboard still holds ours.
        if saved and change_count() == ours:
            restore_snapshot(saved)


def copy(text: str) -> None:
    try:
        set_text(text)
    except Exception as exc:
        log.warning("pasteboard copy failed: %s", exc)


def capture_selection(timeout: float = 0.45) -> tuple[str, Snapshot]:
    """Return (selected text or "", pasteboard snapshot to restore later)."""
    saved = snapshot()
    try:
        before = set_text(f"⁣scribe-plus-{uuid.uuid4().hex}⁣")
    except Exception as exc:
        log.warning("pasteboard unavailable for selection read: %s", exc)
        return "", saved
    post_command_key(keymap.KC_C)
    deadline = time.monotonic() + timeout
    selected = ""
    while time.monotonic() < deadline:
        time.sleep(0.025)
        if change_count() != before:
            selected = get_text()
            break
    if not selected:
        restore_snapshot(saved)  # never leave the sentinel behind
    return selected, saved
