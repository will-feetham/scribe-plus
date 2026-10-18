"""
Windows low-level keyboard hook feeding the hotkey state machine.

Built on pynput's WH_KEYBOARD_LL listener with a `win32_event_filter`, so we
see left/right modifier codes, can tell injected keys from real ones, and can
swallow the few keys we act on (Esc while recording, V in Ctrl+Alt+V).

Rules for code in the hook callback: return fast, never block, never raise.
Windows silently unhooks a callback that is too slow, so the actions are
handed to another thread through a queue and the hook is re-armed
periodically as a safety net.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable

from pynput import keyboard

from scribe_plus.hotkeys import IDLE, VK_MASK, Action, HotkeyMachine

log = logging.getLogger(__name__)

WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
_DOWN = (WM_KEYDOWN, WM_SYSKEYDOWN)
_KEY_MSGS = (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP)
LLKHF_INJECTED, LLKHF_LOWER_IL_INJECTED = 0x10, 0x02

_user32 = None


def _u32():
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        _user32 = u
    return _user32


def physically_down(vk: int) -> bool:
    """Is the key held right now, according to Windows?"""
    try:
        return bool(_u32().GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return True


_controller: keyboard.Controller | None = None


def controller() -> keyboard.Controller:
    global _controller
    if _controller is None:
        _controller = keyboard.Controller()
    return _controller


def send_mask() -> None:
    """Tap the unassigned key 0xE8 so a held Win/Alt doesn't open a menu."""
    k = keyboard.KeyCode.from_vk(VK_MASK)
    c = controller()
    c.press(k)
    c.release(k)


class KeyboardHook:
    REFRESH_AFTER_S = 15 * 60

    def __init__(self, machine: HotkeyMachine, post: Callable[[Action], None]) -> None:
        self.machine = machine
        self.post = post
        self.lock = threading.Lock()
        self._listener: keyboard.Listener | None = None
        self._stop = threading.Event()
        self._started = 0.0
        self._last_event = 0.0
        self._errors = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._start_listener()
        threading.Thread(target=self._ticker, name="hotkey-ticker", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass

    def _start_listener(self) -> None:
        listener = keyboard.Listener(win32_event_filter=self._filter)
        listener.daemon = True
        self._listener = listener
        listener.start()
        self._started = time.monotonic()

    # -- hook callback ----------------------------------------------------

    def _filter(self, msg, data):
        suppress = False
        try:
            if msg in _KEY_MSGS:
                now = time.monotonic()
                injected = bool(data.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
                with self.lock:
                    res = self.machine.feed(int(data.vkCode), msg in _DOWN, now, injected)
                for action in res.actions:
                    self.post(action)
                suppress = res.suppress
                self._last_event = now
        except Exception:
            self._errors += 1
            if self._errors <= 20:
                log.exception("keyboard hook error")
        if suppress and self._listener is not None:
            # Raises pynput's SuppressException: the hook returns 1 and no
            # application sees this key event.
            self._listener.suppress_event()
        return False  # we never need pynput's own on_press/on_release

    # -- timers + safety net -------------------------------------------------

    def _ticker(self) -> None:
        while not self._stop.wait(0.05):
            try:
                with self.lock:
                    actions = self.machine.tick(time.monotonic())
                for action in actions:
                    self.post(action)
                self._maybe_rearm()
            except Exception:
                log.exception("hotkey ticker error")

    def _maybe_rearm(self) -> None:
        listener = self._listener
        dead = listener is None or not listener.is_alive()
        now = time.monotonic()
        quiet = (self.machine.state == IDLE and not self.machine.down
                 and now - self._last_event > 5.0)
        due = now - self._started > self.REFRESH_AFTER_S
        if dead or (quiet and due):
            log.info("re-arming keyboard hook (%s)", "listener died" if dead else "periodic")
            try:
                if listener is not None:
                    listener.stop()
            except Exception:
                pass
            with self.lock:
                self.machine.reset()
            self._start_listener()
