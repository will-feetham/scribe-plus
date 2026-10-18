"""
Small modal dialogs on the main thread (API keys, confirmations).

Scribe Plus is a menu bar app with no Dock icon, and macOS won't bring
such an app's alert to the front. Like Scribe, it briefly becomes a regular
app while a dialog is open, then goes back to the menu bar.
"""
from __future__ import annotations

from contextlib import contextmanager

from AppKit import (NSAlert, NSApp, NSApplicationActivationPolicyAccessory,
                    NSApplicationActivationPolicyRegular, NSMakeRect, NSSecureTextField)

FIRST_BUTTON = 1000  # NSAlertFirstButtonReturn


@contextmanager
def _in_front():
    app = NSApp()
    try:
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)
        yield
    finally:
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def ask_secret(title: str, prompt: str) -> str | None:
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(prompt)
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")
    field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 24))
    alert.setAccessoryView_(field)
    alert.window().setInitialFirstResponder_(field)
    with _in_front():
        response = alert.runModal()
    if response != FIRST_BUTTON:
        return None
    return str(field.stringValue() or "").strip() or None


def confirm(title: str, message: str) -> bool:
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("Yes")
    alert.addButtonWithTitle_("Cancel")
    with _in_front():
        return alert.runModal() == FIRST_BUTTON


def info(title: str, message: str) -> None:
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("OK")
    with _in_front():
        alert.runModal()
