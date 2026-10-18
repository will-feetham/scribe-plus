"""
Hotkey state machine: raw key events in, dictation actions out.

Pure Python with no OS calls, so every gesture is unit-tested. The Windows
glue (scribe_plus.win.keys) feeds it low-level hook events and carries out
the actions it returns.

Gestures (shown for the default Ctrl+Win hotkey):
  * Hold Ctrl+Win, speak, release ......... dictate
  * Hold Ctrl+Win+Alt, speak, release ..... Command Mode (edit the selection)
  * Tap Ctrl+Win twice quickly ............ hands-free; tap again to finish
  * Esc while recording ................... cancel (the Esc is swallowed)
  * Any other key while holding ........... cancel, so OS shortcuts such as
                                            Ctrl+Win+D still work
  * Ctrl+Alt+V ............................ paste the last dictation again
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Windows virtual-key codes. Low-level hooks report the left/right variants;
# the generic ones are included in case another program injects them.
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LMENU, VK_RMENU = 0xA4, 0xA5
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_ESCAPE = 0x1B
VK_V = 0x56
# Unassigned virtual key. Injecting it while Win or Alt is held stops Windows
# from opening the Start menu (or an app's Alt menu) when the key comes up.
# AutoHotkey uses the same trick ("MenuMaskKey").
VK_MASK = 0xE8

GROUP_CTRL = frozenset({VK_LCONTROL, VK_RCONTROL, VK_CONTROL})
GROUP_WIN = frozenset({VK_LWIN, VK_RWIN})
GROUP_ALT = frozenset({VK_LMENU, VK_RMENU, VK_MENU})
GROUP_SHIFT = frozenset({VK_LSHIFT, VK_RSHIFT, VK_SHIFT})
MODIFIERS = GROUP_CTRL | GROUP_WIN | GROUP_ALT | GROUP_SHIFT
_MASKABLE = GROUP_WIN | GROUP_ALT

TAP_MAX_S = 0.30            # a press shorter than this is a tap
DOUBLE_TAP_WINDOW_S = 0.40  # max gap between the two taps of a double-tap
MIN_HOLD_S = 0.25           # shorter holds are treated as accidental
STALE_RELEASE_S = 10.0      # give up waiting for a release after this long


@dataclass(frozen=True)
class HotkeySpec:
    id: str
    label: str
    groups: tuple[frozenset, ...]
    command_group: frozenset
    command_label: str

    @property
    def needs_mask(self) -> bool:
        return any(g & _MASKABLE for g in self.groups)


def _single(vk: int) -> frozenset:
    return frozenset({vk})


PRESETS: dict[str, HotkeySpec] = {
    "ctrl_win": HotkeySpec("ctrl_win", "Ctrl+Win", (GROUP_CTRL, GROUP_WIN),
                           GROUP_ALT, "Ctrl+Win+Alt"),
    "right_alt": HotkeySpec("right_alt", "Right Alt", (_single(VK_RMENU),),
                            GROUP_SHIFT, "Right Alt+Shift"),
    "right_ctrl": HotkeySpec("right_ctrl", "Right Ctrl", (_single(VK_RCONTROL),),
                             GROUP_SHIFT, "Right Ctrl+Shift"),
    "left_ctrl": HotkeySpec("left_ctrl", "Left Ctrl", (_single(VK_LCONTROL),),
                            GROUP_SHIFT, "Left Ctrl+Shift"),
    "left_alt": HotkeySpec("left_alt", "Left Alt", (_single(VK_LMENU),),
                           GROUP_SHIFT, "Left Alt+Shift"),
    "right_shift": HotkeySpec("right_shift", "Right Shift", (_single(VK_RSHIFT),),
                              GROUP_CTRL, "Right Shift+Ctrl"),
    "right_win": HotkeySpec("right_win", "Right Win", (_single(VK_RWIN),),
                            GROUP_SHIFT, "Right Win+Shift"),
}
DEFAULT_HOTKEY = "ctrl_win"


@dataclass(frozen=True)
class Action:
    kind: str           # start | mask | command | lock | stop | cancel | paste_last
    mode: str = ""      # for stop: dictate | command | handsfree
    reason: str = ""    # for cancel: escape | shortcut | tap | short


@dataclass
class FeedResult:
    actions: list = field(default_factory=list)
    suppress: bool = False


IDLE, HOLDING, TAP_WAIT, LOCKED, WAIT_RELEASE = (
    "idle", "holding", "tap_wait", "locked", "wait_release")


class HotkeyMachine:
    def __init__(self, spec: HotkeySpec, *, hands_free: bool = True,
                 command_mode: bool = True, max_record_s: float = 360.0,
                 is_down: Callable[[int], bool] | None = None) -> None:
        self.spec = spec
        self.hands_free = hands_free
        self.command_mode = command_mode
        self.max_record_s = max_record_s
        # Physical key check, used to drop keys whose release we never saw
        # (the hook goes blind on the lock screen and UAC prompts).
        self._is_down = is_down or (lambda vk: True)
        self.state = IDLE
        self.down: set[int] = set()
        self._t_down = 0.0
        self._t_tap_release = 0.0
        self._t_wait = 0.0
        self._command_seen = False
        self._suppressed: set[int] = set()

    # -- configuration -------------------------------------------------

    def configure(self, spec: HotkeySpec | None = None, *, hands_free: bool | None = None,
                  command_mode: bool | None = None, max_record_s: float | None = None) -> None:
        if spec is not None:
            self.spec = spec
        if hands_free is not None:
            self.hands_free = hands_free
        if command_mode is not None:
            self.command_mode = command_mode
        if max_record_s is not None:
            self.max_record_s = max_record_s

    # -- helpers -------------------------------------------------------

    def _active(self) -> bool:
        return all(self.down & g for g in self.spec.groups)

    def _in_combo(self, vk: int) -> bool:
        return any(vk in g for g in self.spec.groups)

    def _prune_stale(self, except_vk: int) -> None:
        for vk in list(self.down):
            if vk != except_vk and not self._is_down(vk):
                self.down.discard(vk)

    def _start_actions(self, out: list) -> None:
        if self.spec.needs_mask:
            out.append(Action("mask"))
        out.append(Action("start"))

    # -- events --------------------------------------------------------

    def feed(self, vk: int, is_down: bool, t: float, injected: bool = False) -> FeedResult:
        res = FeedResult()
        if injected:
            # Our own mask key and synthetic Ctrl+V/Ctrl+C come back through
            # the hook; they are not the user.
            return res

        if not is_down:
            was_active = self._active()
            self.down.discard(vk)
            if vk in self._suppressed:
                self._suppressed.discard(vk)
                res.suppress = True
            if was_active and not self._active():
                self._on_combo_up(t, res.actions)
            return res

        # Key down (auto-repeat sends more of these while a key is held).
        repeat = vk in self.down
        if repeat and vk in self._suppressed:
            # Keep swallowing a held Esc or V, or its auto-repeats leak through.
            res.suppress = True
            return res
        was_active = self._active()
        if not repeat:
            self.down.add(vk)
            self._prune_stale(except_vk=vk)
        now_active = self._active()

        if now_active and not was_active:
            self._on_combo_down(t, res.actions)
            return res

        if repeat:
            return res

        if vk == VK_ESCAPE and self.state in (HOLDING, TAP_WAIT, LOCKED):
            res.actions.append(Action("cancel", reason="escape"))
            res.suppress = True
            self._suppressed.add(vk)
            self.state = WAIT_RELEASE if self._active() else IDLE
            self._t_wait = t
            return res

        if vk in self.spec.command_group and self.state == HOLDING:
            if self.command_mode and not self._command_seen:
                self._command_seen = True
                res.actions.append(Action("command"))
                if vk in _MASKABLE:
                    res.actions.append(Action("mask"))
            return res

        if vk not in MODIFIERS and not self._in_combo(vk):
            if self.state in (HOLDING, TAP_WAIT):
                # The user is pressing a shortcut that starts with our keys.
                res.actions.append(Action("cancel", reason="shortcut"))
                self.state = WAIT_RELEASE if self._active() else IDLE
                self._t_wait = t
                return res
            if (self.state == IDLE and vk == VK_V
                    and self.down & GROUP_CTRL and self.down & GROUP_ALT):
                res.actions.append(Action("paste_last"))
                res.suppress = True
                self._suppressed.add(vk)
        return res

    def _on_combo_down(self, t: float, out: list) -> None:
        if self.state == TAP_WAIT and t - self._t_tap_release > DOUBLE_TAP_WINDOW_S:
            # Too slow to count as a double-tap: drop the tap, start fresh.
            out.append(Action("cancel", reason="tap"))
            self.state = IDLE
        if self.state == IDLE:
            self.state = HOLDING
            self._t_down = t
            self._command_seen = False
            self._start_actions(out)
            if self.command_mode and self.down & self.spec.command_group:
                self._command_seen = True
                out.append(Action("command"))
        elif self.state == TAP_WAIT:
            self.state = LOCKED
            if self.spec.needs_mask:
                out.append(Action("mask"))
            out.append(Action("lock"))
        elif self.state == LOCKED:
            if self.spec.needs_mask:
                out.append(Action("mask"))
            out.append(Action("stop", mode="handsfree"))
            self.state = WAIT_RELEASE
            self._t_wait = t
        # WAIT_RELEASE / HOLDING: nothing new

    def _on_combo_up(self, t: float, out: list) -> None:
        if self.state == HOLDING:
            held = t - self._t_down
            if self._command_seen:
                if held < MIN_HOLD_S:
                    out.append(Action("cancel", reason="short"))
                else:
                    out.append(Action("stop", mode="command"))
                self.state = IDLE
            elif self.hands_free and held < TAP_MAX_S:
                self.state = TAP_WAIT
                self._t_tap_release = t
            elif held < MIN_HOLD_S:
                out.append(Action("cancel", reason="short"))
                self.state = IDLE
            else:
                out.append(Action("stop", mode="dictate"))
                self.state = IDLE
        elif self.state == WAIT_RELEASE:
            self.state = IDLE
        # TAP_WAIT and LOCKED ignore releases.

    def tick(self, t: float) -> list:
        """Timers: double-tap window, max recording length, stuck waits."""
        out: list = []
        if self.state == TAP_WAIT and t - self._t_tap_release > DOUBLE_TAP_WINDOW_S:
            out.append(Action("cancel", reason="tap"))
            self.state = IDLE
        elif self.state in (HOLDING, LOCKED) and t - self._t_down > self.max_record_s:
            out.append(Action("stop", mode="handsfree" if self.state == LOCKED else "dictate"))
            self.state = WAIT_RELEASE if self._active() else IDLE
            self._t_wait = t
        elif self.state == WAIT_RELEASE and t - self._t_wait > STALE_RELEASE_S:
            self.state = IDLE
            self.down.clear()
        return out

    def reset(self) -> None:
        self.state = IDLE
        self.down.clear()
        self._suppressed.clear()
