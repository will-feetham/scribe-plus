"""
Notifications on a Mac, sent as Scribe Plus.

pystray shows notifications through AppleScript, which macOS attributes to
Script Editor (and shows only if Script Editor may notify). Inside
Scribe Plus.app this uses the UserNotifications framework instead, so they
come from Scribe Plus with its icon; macOS asks once whether to allow them.
Run outside the app (development), or if macOS refuses the framework, it
falls back to AppleScript.
"""
from __future__ import annotations

import logging
import subprocess
import uuid

log = logging.getLogger(__name__)

_delegate_class = None


def in_app_bundle() -> bool:
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        return str(bundle.bundlePath()).endswith(".app") and bool(bundle.bundleIdentifier())
    except Exception:
        return False


def _as_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def applescript(title: str, message: str) -> None:
    script = f"display notification {_as_quote(message)} with title {_as_quote(title)}"
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _make_delegate():
    """Lets notifications show even while a Scribe Plus dialog is in front."""
    global _delegate_class
    if _delegate_class is None:
        import objc
        import UserNotifications as UN
        from Foundation import NSObject

        class ScribePlusNotificationDelegate(
                NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
            def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                    self, center, notification, handler):
                handler(UN.UNNotificationPresentationOptionBanner
                        | UN.UNNotificationPresentationOptionList)

        _delegate_class = ScribePlusNotificationDelegate
    return _delegate_class.alloc().init()


class Notifier:
    """status: "pending" (waiting for macOS), "allowed", "denied" (turned off
    in System Settings, so nothing is shown) or "applescript" (fallback)."""

    def __init__(self) -> None:
        self.status = "applescript"
        self._center = None
        self._delegate = None
        if not in_app_bundle():
            return
        try:
            import UserNotifications as UN
            center = UN.UNUserNotificationCenter.currentNotificationCenter()
            try:
                self._delegate = _make_delegate()
                center.setDelegate_(self._delegate)
            except Exception as exc:
                log.info("notification delegate not set: %s", exc)
            self._UN, self._center = UN, center
            self.status = "pending"
            center.requestAuthorizationWithOptions_completionHandler_(
                UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound, self._authorized)
        except Exception as exc:
            log.info("UserNotifications unavailable (%s); using AppleScript", exc)
            self._center = None
            self.status = "applescript"

    def _authorized(self, granted, error) -> None:
        if granted:
            self.status = "allowed"
        elif error is not None:
            log.info("notifications refused by macOS (%s); using AppleScript", error)
            self.status = "applescript"
        else:
            self.status = "denied"
        log.info("notifications: %s", self.status)

    def post(self, title: str, message: str) -> None:
        if self._center is None or self.status == "applescript":
            applescript(title, message)
            return
        if self.status == "denied":
            log.info("notification (turned off in System Settings): %s: %s", title, message)
            return
        UN = self._UN
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(message)
        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None)
        self._center.addNotificationRequest_withCompletionHandler_(request, self._added)

    @staticmethod
    def _added(error) -> None:
        if error is not None:
            log.info("notification not shown: %s", error)
