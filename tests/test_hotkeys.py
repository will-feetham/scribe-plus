from scribe_plus.hotkeys import (
    DOUBLE_TAP_WINDOW_S, IDLE, LOCKED, PRESETS, TAP_WAIT, VK_ESCAPE, VK_LCONTROL,
    VK_LMENU, VK_LSHIFT, VK_LWIN, VK_RCONTROL, VK_RMENU, VK_V, HotkeyMachine,
)

VK_D, VK_A = 0x44, 0x41


def machine(preset="ctrl_win", **kw):
    return HotkeyMachine(PRESETS[preset], **kw)


def acts(res):
    return [(a.kind, a.mode or a.reason) if (a.mode or a.reason) else a.kind for a in res.actions]


def down(m, vk, t, **kw):
    return m.feed(vk, True, t, **kw)


def up(m, vk, t, **kw):
    return m.feed(vk, False, t, **kw)


def test_hold_ctrl_win_dictates():
    m = machine()
    assert acts(down(m, VK_LCONTROL, 0.00)) == []
    assert acts(down(m, VK_LWIN, 0.05)) == ["mask", "start"]
    assert acts(down(m, VK_LWIN, 0.40)) == []  # key auto-repeat
    assert acts(up(m, VK_LWIN, 1.20)) == [("stop", "dictate")]
    assert acts(up(m, VK_LCONTROL, 1.25)) == []
    assert m.state == IDLE


def test_order_does_not_matter():
    m = machine()
    down(m, VK_LWIN, 0.0)
    assert acts(down(m, VK_RCONTROL, 0.02)) == ["mask", "start"]
    assert acts(up(m, VK_RCONTROL, 0.9)) == [("stop", "dictate")]


def test_alt_during_hold_switches_to_command_mode():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.01)
    assert acts(down(m, VK_LMENU, 0.2)) == ["command", "mask"]
    assert acts(up(m, VK_LWIN, 1.5)) == [("stop", "command")]


def test_alt_held_before_hotkey_is_command_mode():
    m = machine()
    down(m, VK_LMENU, 0.0)
    down(m, VK_LCONTROL, 0.0)
    assert acts(down(m, VK_LWIN, 0.01)) == ["mask", "start", "command"]
    assert acts(up(m, VK_LCONTROL, 1.0)) == [("stop", "command")]


def test_command_mode_off_ignores_alt():
    m = machine(command_mode=False)
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    assert acts(down(m, VK_LMENU, 0.2)) == []
    assert acts(up(m, VK_LWIN, 1.0)) == [("stop", "dictate")]


def test_double_tap_locks_hands_free_and_next_press_stops():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    assert acts(down(m, VK_LWIN, 0.0)) == ["mask", "start"]
    assert acts(up(m, VK_LWIN, 0.10)) == []
    assert m.state == TAP_WAIT
    assert acts(down(m, VK_LWIN, 0.25)) == ["mask", "lock"]
    assert m.state == LOCKED
    up(m, VK_LWIN, 0.32)
    up(m, VK_LCONTROL, 0.33)
    assert acts(down(m, VK_A, 3.0)) == []  # typing while hands-free is fine
    up(m, VK_A, 3.05)
    down(m, VK_LCONTROL, 9.0)
    assert acts(down(m, VK_LWIN, 9.01)) == ["mask", ("stop", "handsfree")]
    assert acts(up(m, VK_LWIN, 9.1)) == []
    assert m.state == IDLE


def test_single_tap_is_dropped_after_window():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    up(m, VK_LWIN, 0.1)
    up(m, VK_LCONTROL, 0.1)
    assert m.tick(0.1 + DOUBLE_TAP_WINDOW_S / 2) == []
    assert [(a.kind, a.reason) for a in m.tick(0.1 + DOUBLE_TAP_WINDOW_S + 0.01)] == [("cancel", "tap")]
    assert m.state == IDLE


def test_slow_second_tap_starts_a_new_recording():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    up(m, VK_LWIN, 0.1)
    # no tick ran in between; the late press must not lock hands-free
    assert acts(down(m, VK_LWIN, 0.1 + DOUBLE_TAP_WINDOW_S + 0.2)) == [("cancel", "tap"), "mask", "start"]


def test_escape_cancels_and_is_swallowed():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    res = down(m, VK_ESCAPE, 0.5)
    assert acts(res) == [("cancel", "escape")] and res.suppress
    assert up(m, VK_ESCAPE, 0.55).suppress
    assert acts(up(m, VK_LWIN, 0.8)) == []  # no stop after a cancel
    assert m.state == IDLE


def test_escape_cancels_hands_free():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    up(m, VK_LWIN, 0.1)
    down(m, VK_LWIN, 0.2)
    up(m, VK_LWIN, 0.3)
    up(m, VK_LCONTROL, 0.3)
    res = down(m, VK_ESCAPE, 2.0)
    assert acts(res) == [("cancel", "escape")] and res.suppress
    assert m.state == IDLE


def test_os_shortcut_cancels_but_passes_through():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    res = down(m, VK_D, 0.3)  # Ctrl+Win+D = new virtual desktop
    assert acts(res) == [("cancel", "shortcut")] and not res.suppress
    assert acts(up(m, VK_D, 0.35)) == []
    assert acts(up(m, VK_LWIN, 0.5)) == []
    assert m.state == IDLE


def test_injected_events_are_ignored():
    m = machine()
    assert acts(down(m, VK_LCONTROL, 0.0, injected=True)) == []
    assert acts(down(m, VK_LWIN, 0.0, injected=True)) == []
    assert m.state == IDLE and not m.down


def test_ctrl_alt_v_pastes_last_and_swallows_v():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LMENU, 0.0)
    res = down(m, VK_V, 0.1)
    assert acts(res) == ["paste_last"] and res.suppress
    assert up(m, VK_V, 0.2).suppress
    assert m.state == IDLE


def test_short_hold_without_hands_free_is_cancelled():
    m = machine(hands_free=False)
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    assert acts(up(m, VK_LWIN, 0.1)) == [("cancel", "short")]


def test_hold_between_tap_and_min_is_tap_when_hands_free():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    assert acts(up(m, VK_LWIN, 0.28)) == []
    assert m.state == TAP_WAIT


def test_max_duration_auto_stops():
    m = machine(max_record_s=5)
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    assert m.tick(4.0) == []
    assert [(a.kind, a.mode) for a in m.tick(5.1)] == [("stop", "dictate")]
    assert acts(up(m, VK_LWIN, 6.0)) == []


def test_missed_release_is_pruned_by_physical_check():
    held = {VK_LCONTROL}
    m = machine(is_down=lambda vk: vk in held)
    m.down.add(VK_LWIN)  # e.g. Win+L locked the PC and we never saw Win go up
    assert acts(down(m, VK_LCONTROL, 0.0)) == []
    assert VK_LWIN not in m.down


def test_right_alt_preset_uses_shift_for_command_and_masks_alt():
    m = machine("right_alt")
    assert acts(down(m, VK_RMENU, 0.0)) == ["mask", "start"]
    assert acts(down(m, VK_LSHIFT, 0.2)) == ["command"]
    assert acts(up(m, VK_RMENU, 1.0)) == [("stop", "command")]


def test_right_ctrl_preset_needs_no_mask():
    m = machine("right_ctrl")
    assert acts(down(m, VK_RCONTROL, 0.0)) == ["start"]
    assert acts(up(m, VK_RCONTROL, 0.8)) == [("stop", "dictate")]


def test_stuck_wait_release_recovers():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.0)
    down(m, VK_D, 0.3)  # cancel -> waiting for release that never comes
    m.tick(11.0)
    assert m.state == IDLE and not m.down


def test_configure_switches_preset():
    m = machine()
    m.configure(PRESETS["right_ctrl"], hands_free=False)
    assert acts(down(m, VK_RCONTROL, 0.0)) == ["start"]
    assert acts(up(m, VK_RCONTROL, 0.1)) == [("cancel", "short")]


def test_held_escape_auto_repeat_stays_swallowed():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LWIN, 0.05)
    assert down(m, VK_ESCAPE, 1.0).suppress
    assert down(m, VK_ESCAPE, 1.5).suppress  # auto-repeat while Esc is held
    assert up(m, VK_ESCAPE, 1.6).suppress


def test_held_v_auto_repeat_stays_swallowed():
    m = machine()
    down(m, VK_LCONTROL, 0.0)
    down(m, VK_LMENU, 0.01)
    assert down(m, VK_V, 0.1).suppress
    assert down(m, VK_V, 0.7).suppress
    assert up(m, VK_V, 0.8).suppress
