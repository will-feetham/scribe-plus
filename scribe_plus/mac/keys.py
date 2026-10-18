"""
macOS keyboard event tap feeding the hotkey state machine.

Built the same way as Scribe's Mac hotkey watcher (a Quartz CGEventTap on
its own run-loop thread, re-armed when macOS disables it), with two
additions: every key is fed to the shared HotkeyMachine, so the gestures
match Windows, and the tap is an active one when Accessibility is granted,
so the few keys we act on (Esc while recording, V in Control+Command+V)
can be swallowed. Without Accessibility it falls back to listen-only.

Rules for the callback: return fast, never raise. macOS disables a tap
whose callback is slow.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    CGEventCreateKeyboardEvent,
    CGEventGetFlags,
    CGEventGetIntegerValueField,
    CGEventMaskBit,
    CGEventPost,
    CGEventSetFlags,
    CGEventSetIntegerValueField,
    CGEventSourceFlagsState,
    CGEventSourceKeyState,
    CGEventTapCreate,
    CGEventTapEnable,
    kCFRunLoopCommonModes,
    kCGEventFlagsChanged,
    kCGEventKeyDown,
    kCGEventKeyUp,
    kCGEventSourceStateHIDSystemState,
    kCGEventTapDisabledByTimeout,
    kCGEventTapDisabledByUserInput,
    kCGHeadInsertEventTap,
    kCGHIDEventTap,
    kCGKeyboardEventKeycode,
    kCGSessionEventTap,
)

import Quartz

from scribe_plus.hotkeys import Action, HotkeyMachine
from scribe_plus.mac import keymap

kCGEventSourceUserData = getattr(Quartz, "kCGEventSourceUserData", 42)

log = logging.getLogger(__name__)

# Tag on events we post ourselves (Command+V, Command+C), so the tap can
# tell them from the user's typing.
MAGIC = 0x5C21B7E
TAP_DEFAULT, TAP_LISTEN_ONLY = 0, 1


def physically_down(vk: int) -> bool:
    """Is the key held right now, according to the HID system?"""
    kc = keymap.VK_TO_MAC.get(vk)
    if kc is None:
        return True
    try:
        return bool(CGEventSourceKeyState(kCGEventSourceStateHIDSystemState, kc))
    except Exception:
        return True


def modifiers_down() -> bool:
    try:
        return bool(CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState)
                    & (keymap.ANY_MODIFIER | keymap.MASK_FN))
    except Exception:
        return False


def post_command_key(keycode: int) -> None:
    """Send Command + key to the frontmost app (tagged as ours)."""
    for is_down in (True, False):
        ev = CGEventCreateKeyboardEvent(None, keycode, is_down)
        CGEventSetFlags(ev, keymap.MASK_COMMAND)
        CGEventSetIntegerValueField(ev, kCGEventSourceUserData, MAGIC)
        CGEventPost(kCGHIDEventTap, ev)


class KeyboardHook:
    def __init__(self, machine: HotkeyMachine, post: Callable[[Action], None],
                 on_silent: Callable[[], None] | None = None) -> None:
        self.machine = machine
        self.post = post
        self.on_silent = on_silent
        self.lock = threading.Lock()
        self._tap = None
        self._loop = None
        self.active = False          # True when the tap can swallow keys
        self.events_seen = 0
        self._stop = threading.Event()
        self._errors = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._run, name="hotkey-tap", daemon=True).start()
        threading.Thread(target=self._ticker, name="hotkey-ticker", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._loop is not None:
                CFRunLoopStop(self._loop)
        except Exception:
            pass

    def _create_tap(self):
        mask = (CGEventMaskBit(kCGEventFlagsChanged) | CGEventMaskBit(kCGEventKeyDown)
                | CGEventMaskBit(kCGEventKeyUp))
        for option in (TAP_DEFAULT, TAP_LISTEN_ONLY):
            tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, option,
                                   mask, self._callback, None)
            if tap:
                self.active = option == TAP_DEFAULT
                return tap
        return None

    def _run(self) -> None:
        tap = self._create_tap()
        if not tap:
            log.warning("could not create the keyboard event tap: grant Input Monitoring "
                        "and Accessibility to Scribe Plus in System Settings")
            if self.on_silent:
                self.on_silent()
            return
        log.info("keyboard tap running (%s)", "active" if self.active else "listen-only")
        self._tap = tap
        self._loop = CFRunLoopGetCurrent()
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(self._loop, source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)

        def silence_check() -> None:
            # A tap created without Input Monitoring exists but sees nothing.
            if not self._stop.wait(20) and self.events_seen == 0 and self.on_silent:
                self.on_silent()
        threading.Thread(target=silence_check, daemon=True).start()
        CFRunLoopRun()

    # -- tap callback -----------------------------------------------------

    def _callback(self, proxy, etype, event, refcon):
        try:
            if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
                # macOS switched the tap off (slow callback, sleep, fast user
                # switch). Re-arm it, and forget keys whose release we missed.
                if self._tap is not None:
                    CGEventTapEnable(self._tap, True)
                with self.lock:
                    self.machine.reset()
                log.info("keyboard tap re-enabled")
                return event
            self.events_seen += 1
            if CGEventGetIntegerValueField(event, kCGEventSourceUserData) == MAGIC:
                return event
            keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            if etype == kCGEventFlagsChanged:
                down = keymap.modifier_down(keycode, CGEventGetFlags(event))
                if down is None:
                    return event
            else:
                down = etype == kCGEventKeyDown
            with self.lock:
                res = self.machine.feed(keymap.vk_for(keycode), down, time.monotonic())
            for action in res.actions:
                self.post(action)
            if res.suppress and self.active:
                return None
        except Exception:
            self._errors += 1
            if self._errors <= 20:
                log.exception("keyboard tap error")
        return event

    # -- timers ------------------------------------------------------------

    def _ticker(self) -> None:
        while not self._stop.wait(0.05):
            try:
                with self.lock:
                    actions = self.machine.tick(time.monotonic())
                for action in actions:
                    self.post(action)
            except Exception:
                log.exception("hotkey ticker error")
