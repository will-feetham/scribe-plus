"""
Windows glue exercised off Windows: the Win32 calls are faked or degrade to
no-ops, but the wiring (hook -> state machine -> pipeline -> pill -> paste,
tray menu callbacks) is real. Needs a display: run under xvfb-run on Linux.
"""
import os
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32" and not os.environ.get("DISPLAY"),
                                reason="needs a display (use xvfb-run)")

from tests.test_pipeline import Api, FakeRecorder, speech_wav  # noqa: E402


class FakeData:
    def __init__(self, vk, flags=0):
        self.vkCode, self.flags = vk, flags


class FakeListener:
    def __init__(self):
        self.suppressed = 0

    def suppress_event(self):
        self.suppressed += 1
        raise KeyboardInterrupt("suppress")  # stands in for pynput's SuppressException

    def is_alive(self):
        return True

    def stop(self):
        pass


WM_KEYDOWN, WM_KEYUP = 0x100, 0x101


def test_keyboard_hook_plumbing():
    from scribe_plus.hotkeys import PRESETS, HotkeyMachine
    from scribe_plus.win import keys
    posted = []
    hook = keys.KeyboardHook(HotkeyMachine(PRESETS["ctrl_win"]), posted.append)
    hook._listener = FakeListener()
    assert hook._filter(WM_KEYDOWN, FakeData(0xA2)) is False
    hook._filter(WM_KEYDOWN, FakeData(0x5B))
    assert [a.kind for a in posted] == ["mask", "start"]
    hook._filter(WM_KEYDOWN, FakeData(0x41, flags=0x10))  # injected: ignored
    assert len(posted) == 2
    with pytest.raises(KeyboardInterrupt):
        hook._filter(WM_KEYDOWN, FakeData(0x1B))  # Esc is swallowed
    assert hook._listener.suppressed == 1 and posted[-1].kind == "cancel"
    hook._filter(0x0051, FakeData(0))  # unrelated message: ignored, no crash


def test_hook_survives_a_bad_event():
    from scribe_plus.hotkeys import PRESETS, HotkeyMachine
    from scribe_plus.win import keys
    hook = keys.KeyboardHook(HotkeyMachine(PRESETS["ctrl_win"]), lambda a: None)
    hook._listener = FakeListener()
    assert hook._filter(WM_KEYDOWN, object()) is False  # no vkCode: logged, not raised


def test_clipboard_paste_restore_and_selection(monkeypatch):
    from scribe_plus.win import clipboard as C
    board = {"v": "old"}
    chords = []
    monkeypatch.setattr(C, "_get", lambda: board["v"])
    monkeypatch.setattr(C, "_set", lambda t: board.__setitem__("v", t))
    monkeypatch.setattr(C, "_chord", lambda vk: chords.append(vk))
    C.paste_text("a\nb", restore_delay_ms=0)
    assert chords == [C.VK_V] and board["v"] == "old"

    def user_copies_meanwhile(vk):
        board["v"] = "user copied this"
    monkeypatch.setattr(C, "_chord", user_copies_meanwhile)
    C.paste_text("x", restore_delay_ms=0)
    assert board["v"] == "user copied this"  # never clobber the user's own copy

    board["v"] = "old"
    monkeypatch.setattr(C, "_chord", lambda vk: board.__setitem__("v", "picked\r\ntext") if vk == C.VK_C else None)
    assert C.capture_selection(timeout=0.2) == ("picked\ntext", "old")

    board["v"] = "old"
    monkeypatch.setattr(C, "_chord", lambda vk: None)  # nothing selected
    assert C.capture_selection(timeout=0.1) == ("", "old")
    assert board["v"] == "old"  # sentinel never left behind

    assert C.to_crlf("a\nb\r\nc") == "a\r\nb\r\nc"
    held = {"n": 3}

    def down(vk):
        held["n"] -= 1
        return held["n"] > 0
    monkeypatch.setattr(C, "physically_down", down)
    assert C.wait_modifiers_released(1.0)


def pump(root, seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        if until and until():
            return True
        time.sleep(0.01)
    return bool(until and until())


def with_tk_retry(make, attempts=3):
    """Windows CI runners sometimes fail to start a second Tk interpreter in
    one process ("Can't find a usable init.tcl ... couldn't read file ...
    No error"), the same flake matplotlib sees on windows-latest. The app
    itself only ever starts one, so retry here rather than fail the build."""
    import tkinter as tk
    for attempt in range(attempts):
        try:
            return make()
        except tk.TclError as exc:
            if "init.tcl" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(1.0)


def test_pill_renders_every_state():
    import tkinter as tk
    from scribe_plus.win.overlay import Pill
    root = with_tk_retry(tk.Tk)
    root.withdraw()
    try:
        pill = Pill(root)
        shots = os.environ.get("PILL_SHOTS")
        for i, (state, kw) in enumerate([
            ("recording", {"mode": "dictate"}), ("recording", {"mode": "command"}),
            ("recording", {"mode": "handsfree"}), ("processing", {}), ("done", {}),
            ("error", {"message": "No speech heard"}), ("info", {"message": "Copied: press Ctrl+V"}),
        ]):
            pill.set_state(state, **kw)
            for lv in (0.2, 0.9, 0.5, 0.7, 0.3, 0.8):
                pill.set_state("level", value=lv)
                pump(root, 0.06)
            # Width and fade are eased; wait for them to settle (bounded, so a
            # slow CI machine doesn't turn timing into a failure).
            assert pump(root, 2.0, until=lambda: abs(pill.anim.w - pill.anim.w_target) < 3
                        and pill.anim.alpha > 0.8), f"{state}: pill never settled"
            assert pill.state != "hidden"
            assert len(pill.canvas.find_all()) > 1, f"{state}: content not drawn"
            w, x, y = pill._geom
            assert w >= 56 and pill.h >= 30
            if shots:
                from PIL import ImageGrab
                x, y = pill.win.winfo_rootx(), pill.win.winfo_rooty()
                ImageGrab.grab(bbox=(x, y, x + w, y + pill.h), xdisplay=os.environ["DISPLAY"]).save(
                    os.path.join(shots, f"pill-{i}-{state}-{kw.get('mode', '')}.png"))
        # Closing is animated: still visible right after, gone shortly after.
        pill.set_state("idle")
        assert pill.state != "hidden" and pill.anim.closing
        assert pump(root, 1.5, until=lambda: pill.state == "hidden")
        assert not pill._running, "animation loop should stop when hidden"
        # Reopening mid-close cancels the close.
        pill.set_state("recording", mode="dictate")
        pump(root, 0.05)
        pill.set_state("idle")
        pump(root, 0.03)
        pill.set_state("processing")
        assert pump(root, 2.0, until=lambda: pill.anim.alpha > 0.8)
        assert pill.state == "processing" and not pill.anim.closing
        # Done and error hide themselves.
        pill.set_state("done")
        assert pump(root, 2.5, until=lambda: pill.state == "hidden")
        pill.set_state("error", message="Mic busy")
        assert pump(root, 4.0, until=lambda: pill.state == "hidden")
    finally:
        root.destroy()


def test_app_wiring_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    import scribe_core
    import scribe_plus_windows
    from scribe_plus import engines as E
    from scribe_plus.win import clipboard as C
    from scribe_plus.win import keys

    monkeypatch.setattr(scribe_core, "Recorder", lambda: FakeRecorder(speech_wav()))
    monkeypatch.setattr(scribe_core, "prewarm_recorder", lambda: None)
    pasted, masks = [], []
    monkeypatch.setattr(C, "paste_text", lambda text, **kw: pasted.append(text))
    monkeypatch.setattr(C, "wait_modifiers_released", lambda timeout: True)
    monkeypatch.setattr(keys, "send_mask", lambda: masks.append(1))
    # The fake key presses aren't physically held on a real Windows machine.
    monkeypatch.setattr(keys, "physically_down", lambda vk: True)

    app = with_tk_retry(lambda: scribe_plus_windows.App(debug=True))
    try:
        app.engine.pool = E.HttpPool(transport=httpx.MockTransport(Api()))
        app.hook._listener = FakeListener()

        # Every menu item builds and every check-state callable works.
        def walk(menu, depth=0):
            n = 0
            for item in menu.items:
                n += 1
                if item.checked is not None:
                    item.checked  # evaluates our callable
                if item.submenu:
                    n += walk(item.submenu, depth + 1)
            return n
        assert walk(app._menu()) > 40

        # A full dictation through the real hook filter and pipeline.
        app.hook._filter(WM_KEYDOWN, FakeData(0xA2))
        app.hook._filter(WM_KEYDOWN, FakeData(0x5B))
        pump(app.root, 0.4)
        assert app.pill.state == "recording"
        app.hook._filter(WM_KEYUP, FakeData(0x5B))
        app.hook._filter(WM_KEYUP, FakeData(0xA2))
        assert pump(app.root, 5, until=lambda: pasted), "nothing was pasted"
        assert pasted == ["We should meet Wednesday."] and masks
        assert pump(app.root, 2, until=lambda: app.pill.state in ("done", "hidden"))

        # Menu actions change settings and the live hotkey machine.
        app._set_stt("groq", "whisper-large-v3")(None, None)
        assert app.settings.stt_model("groq") == "whisper-large-v3"
        app._set_cleanup("groq", "openai/gpt-oss-120b")(None, None)
        assert app.settings.cleanup_model("groq") == "openai/gpt-oss-120b"
        app._toggle("hands_free")(None, None)
        assert app.machine.hands_free is False
        app._set_hotkey("right_ctrl")(None, None)
        assert app.machine.spec.id == "right_ctrl"
        app._toggle_pause()
        assert app.engine.paused
        app._toggle_pause()
        assert scribe_core.load_history(limit=1)[0]["text"] == "We should meet Wednesday."
        app._copy_last()
    finally:
        try:
            app.root.destroy()
        except Exception:
            pass


def test_tray_icons_for_every_state():
    from scribe_plus.win import icons
    seen = set()
    for state in ("idle", "recording", "processing", "error", "paused"):
        img = icons.make_icon(state, dark=True)
        assert img.size == (256, 256) and img.mode == "RGBA"
        assert img.getbbox() is not None
        seen.add(img.tobytes())
    assert len(seen) == 5, "each state should look different"
    # Idle follows the taskbar: light bars on dark, dark bars on light.
    assert icons.make_icon("idle", dark=True).getpixel((128, 128))[:3] == icons.LIGHT_INK
    assert icons.make_icon("idle", dark=False).getpixel((128, 128))[:3] == icons.DARK_INK
    assert icons.make_icon("recording", dark=True) is icons.make_icon("recording", dark=True)
    assert icons.app_icon().getpixel((20, 128))[:3] == icons.TILE
