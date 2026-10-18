"""Mac key mapping and Mac hotkey gestures (pure Python; runs anywhere)."""
from scribe_plus.hotkeys import (MAC_PRESETS, PASTE_LAST_MAC, VK_ESCAPE, VK_FN, VK_RMENU,
                                 VK_RWIN, HotkeyMachine)
from scribe_plus.mac import keymap as K


def test_keycodes_map_to_machine_codes():
    assert K.vk_for(K.KC_ROPT) == VK_RMENU
    assert K.vk_for(K.KC_RCMD) == VK_RWIN
    assert K.vk_for(K.KC_FN) == VK_FN
    assert K.vk_for(K.KC_ESCAPE) == VK_ESCAPE
    assert K.vk_for(0) == K.OTHER_BASE  # "A" is just some other key
    assert all(K.VK_TO_MAC[v] == k for k, v in K.MAC_TO_VK.items())


def test_left_and_right_modifiers_are_told_apart():
    shift_l, shift_r = K.FLAG_BITS[K.KC_LSHIFT], K.FLAG_BITS[K.KC_RSHIFT]
    both = K.MASK_SHIFT | shift_l | shift_r
    assert K.modifier_down(K.KC_RSHIFT, both) is True
    # Right Shift released while Left Shift is still held:
    assert K.modifier_down(K.KC_RSHIFT, K.MASK_SHIFT | shift_l) is False
    assert K.modifier_down(K.KC_LSHIFT, K.MASK_SHIFT | shift_l) is True
    assert K.modifier_down(K.KC_FN, K.MASK_FN) is True
    assert K.modifier_down(57, 0x10000) is None  # Caps Lock: not tracked


def feed(m, keycode, down, t):
    return m.feed(K.vk_for(keycode), down, t).actions


def kinds(actions):
    return [a.kind for a in actions]


def test_hold_right_option_to_dictate_without_mask_keys():
    m = HotkeyMachine(MAC_PRESETS["right_option"], paste_groups=PASTE_LAST_MAC)
    assert kinds(feed(m, K.KC_ROPT, True, 0.0)) == ["start"]
    assert [(a.kind, a.mode) for a in feed(m, K.KC_ROPT, False, 1.0)] == [("stop", "dictate")]


def test_add_shift_for_command_mode():
    m = HotkeyMachine(MAC_PRESETS["right_option"], paste_groups=PASTE_LAST_MAC)
    feed(m, K.KC_ROPT, True, 0.0)
    assert kinds(feed(m, K.KC_LSHIFT, True, 0.1)) == ["command"]
    feed(m, K.KC_LSHIFT, False, 0.9)
    assert [(a.kind, a.mode) for a in feed(m, K.KC_ROPT, False, 1.0)] == [("stop", "command")]


def test_control_command_v_pastes_last_and_is_swallowed():
    m = HotkeyMachine(MAC_PRESETS["right_option"], paste_groups=PASTE_LAST_MAC)
    feed(m, K.KC_LCTRL, True, 0.0)
    feed(m, K.KC_LCMD, True, 0.0)
    res = m.feed(K.vk_for(K.KC_V), True, 0.1)
    assert kinds(res.actions) == ["paste_last"] and res.suppress
    # Control+Option+V (the Windows combo's Mac twin) does nothing on a Mac.
    m2 = HotkeyMachine(MAC_PRESETS["right_shift"], paste_groups=PASTE_LAST_MAC)
    feed(m2, K.KC_LCTRL, True, 0.0)
    feed(m2, K.KC_LOPT, True, 0.0)
    assert feed(m2, K.KC_V, True, 0.1) == []


def test_two_key_preset_and_fn_preset():
    m = HotkeyMachine(MAC_PRESETS["ctrl_option"], paste_groups=PASTE_LAST_MAC)
    feed(m, K.KC_LCTRL, True, 0.0)
    assert kinds(feed(m, K.KC_LOPT, True, 0.01)) == ["start"]
    assert kinds(feed(m, K.KC_LCMD, True, 0.2)) == ["command"]
    f = HotkeyMachine(MAC_PRESETS["fn"], paste_groups=PASTE_LAST_MAC)
    assert kinds(feed(f, K.KC_FN, True, 0.0)) == ["start"]
    assert kinds(feed(f, 0, True, 0.1)) == ["cancel"]  # Fn+A is a shortcut, not dictation


def test_every_mac_preset_is_mask_free_and_labelled():
    for spec in MAC_PRESETS.values():
        assert not spec.needs_mask
        assert spec.label and spec.command_label.startswith(spec.label.split(" /")[0][:4])
