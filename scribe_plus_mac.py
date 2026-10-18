#!/usr/bin/env python3
"""
Scribe Plus for macOS: AI dictation built on Scribe by Avi Ibrahms (github.com/avibrahms/scribe).

Hold Right Option, talk, let go: the speech is transcribed, cleaned up by a
fast language model and pasted wherever your cursor is.

Normally started from "Scribe Plus.app", which the installer builds. For
development:   venv/bin/python scribe_plus_mac.py --debug
Smoke test:    venv/bin/python scribe_plus_mac.py --smoke-test OUTDIR
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from scribe_plus import __version__
from scribe_plus import settings as S
from scribe_plus.applog import setup_logging
from scribe_plus.tray import APP_TITLE, TrayApp

log = logging.getLogger("scribe_plus")


def single_instance():
    """An exclusive lock on a file in the data folder; None if already held."""
    S.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(S.DATA_DIR / ".instance.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def osascript_alert(text: str) -> None:
    script = f'display alert {_as_quote(APP_TITLE)} message {_as_quote(text)}'
    subprocess.run(["osascript", "-e", script], check=False)


def _as_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def bundle_path() -> str | None:
    try:
        from Foundation import NSBundle
        path = str(NSBundle.mainBundle().bundlePath())
        return path if path.endswith(".app") else None
    except Exception:
        return None


def install_edit_menu(nsapp) -> None:
    """A menu bar app has no Edit menu, which leaves Command+V dead in its
    dialogs (where you paste an API key). Add a hidden one."""
    from AppKit import NSMenu, NSMenuItem
    main = NSMenu.alloc().init()
    edit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                               ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a"),
                               ("Undo", "undo:", "z")):
        edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key))
    edit_item.setSubmenu_(edit)
    main.addItem_(edit_item)
    nsapp.setMainMenu_(main)


class App(TrayApp):
    paste_last_label = "⌃⌘V"

    # -- OS hooks --------------------------------------------------------------

    def _init_ui(self) -> None:
        from scribe_plus.mac.overlay import Pill
        from scribe_plus.mac.platform import MainBridge
        self.bridge = MainBridge()
        self.pill = Pill()

    def _make_tray_icon(self):
        from scribe_plus.mac.statusicon import MacIcon
        return MacIcon("scribe-plus", icon=self.make_icon("idle"), title=APP_TITLE)

    def _make_platform(self):
        from scribe_plus.mac.platform import MacPlatform
        return MacPlatform(self.settings, self.bridge, self.pill,
                           notifier=self.notify, tray_state=self._tray_state)

    def _make_hook(self):
        from scribe_plus.mac import keys
        return keys.KeyboardHook(self.machine, self.engine.post,
                                 on_silent=lambda: self.ui_call(self._permissions_help))

    def _physically_down(self, vk: int) -> bool:
        from scribe_plus.mac import keys
        return keys.physically_down(vk)

    def _paste_groups(self) -> tuple:
        from scribe_plus.hotkeys import PASTE_LAST_MAC
        return PASTE_LAST_MAC

    def make_icon(self, state: str):
        from scribe_plus.icons import make_menubar_icon
        return make_menubar_icon(state)

    def open_path(self, path) -> None:
        try:
            subprocess.Popen(["open", str(path)])
        except Exception as exc:
            log.warning("could not open %s: %s", path, exc)

    def ask_secret(self, title: str, prompt: str) -> str | None:
        from scribe_plus.mac.dialogs import ask_secret
        return ask_secret(title, prompt)

    def confirm(self, title: str, message: str) -> bool:
        from scribe_plus.mac.dialogs import confirm
        return confirm(title, message)

    def run_benchmark(self) -> None:
        cmd = f"cd {shlex.quote(str(S.APP_DIR))} && {shlex.quote(sys.executable)} benchmark.py"
        script = (f'tell application "Terminal" to do script {_as_quote(cmd)}\n'
                  'tell application "Terminal" to activate')
        try:
            subprocess.Popen(["osascript", "-e", script])
        except Exception as exc:
            self.notify("Benchmark failed to start", str(exc)[:150])

    def extra_menu_items(self) -> list:
        from scribe_plus.mac import permissions as P
        M, I = self.pystray.Menu, self.pystray.MenuItem
        missing = P.missing()
        title = "Permissions" + (f" (needs {', '.join(missing)})" if missing else "")
        return [I(title, M(
            I("Input Monitoring (hotkey)...", lambda *_: P.open_pane("input")),
            I("Accessibility (paste)...", lambda *_: P.open_pane("accessibility")),
            I("Microphone...", lambda *_: P.open_pane("microphone")),
            M.SEPARATOR,
            I("Restart Scribe Plus", lambda *_: self.restart()),
        ))]

    def quit(self) -> None:
        log.info("quitting")
        try:
            self.hook.stop()
        except Exception:
            pass
        os._exit(0)

    def restart(self) -> None:
        """Permission changes only take effect in a fresh process."""
        app = bundle_path()
        if app:
            subprocess.Popen(["/bin/sh", "-c", f"sleep 1; open -n {shlex.quote(app)}"])
        else:
            subprocess.Popen([sys.executable] + sys.argv)
        self.quit()

    # -- permissions -------------------------------------------------------------

    def _check_permissions(self) -> None:
        from scribe_plus.mac import permissions as P
        missing = P.missing()
        if not missing:
            return
        log.info("missing permissions: %s", missing)
        if "Input Monitoring" in missing:
            P.request_input_monitoring()
        if "Accessibility" in missing:
            P.accessibility_granted(prompt=True)
        self._watch_permissions(missing)

    def _watch_permissions(self, missing: list[str]) -> None:
        """Restart by itself once the missing permissions are granted."""
        from scribe_plus.mac import permissions as P

        def poll() -> None:
            while P.missing():
                time.sleep(2.0)
            log.info("permissions granted; restarting so they take effect")
            self.notify(APP_TITLE, "Permissions granted. Restarting Scribe Plus...")
            time.sleep(1.0)
            self.ui_call(self.restart)
        threading.Thread(target=poll, name="permission-watch", daemon=True).start()

    def _permissions_help(self) -> None:
        from scribe_plus.mac import permissions as P
        missing = P.missing() or ["Input Monitoring"]
        self.notify("Scribe Plus can't see the hotkey yet",
                    f"Turn on Scribe Plus under {' and '.join(missing)} in "
                    "System Settings > Privacy & Security.")
        P.open_pane("input" if "Input Monitoring" in missing else "accessibility")
        self._rebuild()

    # -- run ---------------------------------------------------------------

    def run(self) -> None:
        from PyObjCTools.AppHelper import callLater
        self._check_permissions()
        self.hook.start()
        threading.Thread(target=self._warm_up, name="warm-up", daemon=True).start()
        callLater(1.5, self._first_run_checks)
        self.icon.run()


# ---------------------------------------------------------------------------
# Smoke test: builds the whole app without starting the keyboard tap, draws
# the pill in every state to PNGs and checks the menu. Used by CI on a Mac.
# ---------------------------------------------------------------------------


def smoke_test(out_dir: str) -> int:
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = [f"Scribe Plus {__version__}", f"python {sys.version.split()[0]}",
              f"executable {sys.executable}", f"prefix {sys.prefix}"]
    nsapp = NSApplication.sharedApplication()
    nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    install_edit_menu(nsapp)
    os.environ.setdefault("GROQ_API_KEY", "gsk_smoke")
    app = App(debug=True)

    def count(menu) -> int:
        n = 0
        for item in menu.items:
            n += 1
            if item.checked is not None:
                _ = item.checked
            if item.submenu:
                n += count(item.submenu)
        return n
    report.append(f"menu items {count(app._menu())}")
    from scribe_plus.mac import context, permissions
    report.append(f"frontmost {context.foreground()[1:]}")
    report.append(f"permissions missing {permissions.missing()}")
    from scribe_plus.mac import clipboard
    clipboard.copy("scribe plus smoke")
    report.append(f"pasteboard round trip {clipboard.get_text() == 'scribe plus smoke'}")

    pill = app.pill
    shots = []
    clock = {"t": 1000.0}
    pill.clock = lambda: clock["t"]
    for name, state, kw, levels in (
        ("recording", "recording", {"mode": "dictate"}, (0.3, 0.9, 0.5, 0.8, 0.4, 0.7)),
        ("command", "recording", {"mode": "command"}, (0.6, 0.3, 0.9, 0.5)),
        ("handsfree", "recording", {"mode": "handsfree"}, (0.2, 0.7, 0.4)),
        ("processing", "processing", {}, ()),
        ("done", "done", {}, ()),
        ("error", "error", {"message": "No speech heard"}, ()),
        ("info", "info", {"message": "Copied: press ⌘V"}, ()),
    ):
        pill.set_state(state, **kw)
        for lv in levels:
            pill.set_state("level", value=lv)
        for _ in range(40):  # 40 frames (2/3 s) of animation
            clock["t"] += 1 / 60
            pill.step_once()
        path = str(out / f"pill-{name}.png")
        shots.append((name, pill.snapshot_png(path), pill.anim.w, pill.anim.alpha))
    for name, ok, w, a in shots:
        report.append(f"pill {name}: png={ok} width={w:.0f} alpha={a:.2f}")
    app.icon.icon = app.make_icon("recording")
    report.append("status icon ok")
    (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    ok = all(s[1] for s in shots)
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Scribe Plus (macOS)")
    parser.add_argument("--debug", action="store_true", help="log to the console too")
    parser.add_argument("--smoke-test", metavar="OUTDIR", help="self-test for CI, then exit")
    args = parser.parse_args()
    if args.smoke_test:
        logging.basicConfig(level=logging.INFO)
        sys.exit(smoke_test(args.smoke_test))
    setup_logging(args.debug)
    lock = single_instance()
    if lock is None:
        osascript_alert("Scribe Plus is already running. Look for the five-bar icon "
                        "in the menu bar.")
        return
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    nsapp = NSApplication.sharedApplication()
    nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    install_edit_menu(nsapp)
    try:
        app = App(args.debug)
    except Exception as exc:
        log.exception("startup failed")
        osascript_alert(f"Scribe Plus couldn't start:\n\n{exc}\n\nDetails: {S.LOG_FILE}")
        return
    app._lock = lock  # held for the life of the process
    app.run()


if __name__ == "__main__":
    main()
