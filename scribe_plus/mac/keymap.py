"""
Mac key codes translated to the Windows-style codes the hotkey state machine
uses (Command reports as Win, Option as Alt). Pure Python, unit-tested.

Modifier keys arrive as "flags changed" events, so whether a modifier went
down or up is read from the device-dependent flag bits, which tell left and
right apart (holding Left Shift while tapping Right Shift stays correct).
"""
from __future__ import annotations

from scribe_plus.hotkeys import (VK_ESCAPE, VK_FN, VK_LCONTROL, VK_LMENU, VK_LSHIFT,
                                 VK_LWIN, VK_RCONTROL, VK_RMENU, VK_RSHIFT, VK_RWIN, VK_V)

# Mac virtual key codes (Carbon kVK_*; layout independent for these keys).
KC_LCMD, KC_RCMD = 55, 54
KC_LSHIFT, KC_RSHIFT = 56, 60
KC_LOPT, KC_ROPT = 58, 61
KC_LCTRL, KC_RCTRL = 59, 62
KC_FN = 63
KC_ESCAPE = 53
KC_V, KC_C = 9, 8

# Device-dependent modifier bits in CGEventFlags (IOKit NX_DEVICE*KEYMASK),
# plus the secondary-Fn bit for the Fn / Globe key.
FLAG_BITS = {
    KC_LCTRL: 0x00000001,
    KC_LSHIFT: 0x00000002,
    KC_RSHIFT: 0x00000004,
    KC_LCMD: 0x00000008,
    KC_RCMD: 0x00000010,
    KC_LOPT: 0x00000020,
    KC_ROPT: 0x00000040,
    KC_RCTRL: 0x00002000,
    KC_FN: 0x00800000,
}

# Device-independent masks, used to check "is any modifier still held".
MASK_SHIFT, MASK_CONTROL, MASK_OPTION, MASK_COMMAND = 0x20000, 0x40000, 0x80000, 0x100000
MASK_FN = 0x800000
ANY_MODIFIER = MASK_SHIFT | MASK_CONTROL | MASK_OPTION | MASK_COMMAND

MAC_TO_VK = {
    KC_LCMD: VK_LWIN, KC_RCMD: VK_RWIN,
    KC_LSHIFT: VK_LSHIFT, KC_RSHIFT: VK_RSHIFT,
    KC_LOPT: VK_LMENU, KC_ROPT: VK_RMENU,
    KC_LCTRL: VK_LCONTROL, KC_RCTRL: VK_RCONTROL,
    KC_FN: VK_FN,
    KC_ESCAPE: VK_ESCAPE,
    KC_V: VK_V,
}
VK_TO_MAC = {vk: kc for kc, vk in MAC_TO_VK.items()}

# Every other key gets a code above the Windows range, so the state machine
# treats it as "some other key" (which cancels a hold, as on Windows).
OTHER_BASE = 0x2000


def vk_for(keycode: int) -> int:
    return MAC_TO_VK.get(int(keycode), OTHER_BASE + int(keycode))


def is_modifier(keycode: int) -> bool:
    return int(keycode) in FLAG_BITS


def modifier_down(keycode: int, flags: int) -> bool | None:
    """For a flags-changed event: is this modifier key now down? None if the
    key code isn't a modifier we track (Caps Lock, for example)."""
    bit = FLAG_BITS.get(int(keycode))
    if bit is None:
        return None
    return bool(int(flags) & bit)
