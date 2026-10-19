#!/usr/bin/env python3
"""
Scribe Plus for Windows: AI dictation built on Scribe by Avi Ibrahms (github.com/avibrahms/scribe).

Hold Ctrl+Win, talk, let go: the speech is transcribed, cleaned up by a
fast language model and pasted wherever your cursor is.

Run with:   venv\\Scripts\\pythonw.exe scribe_plus_windows.py          (normal)
            venv\\Scripts\\python.exe  scribe_plus_windows.py --debug  (console log)
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from scribe_plus import settings as S
from scribe_plus.applog import _LogWriter, setup_logging  # noqa: F401  (re-exported for tests)
from scribe_plus.tray import APP_TITLE, TrayApp

log = logging.getLogger("scribe_plus")


def message_box(text: str, title: str = APP_TITLE) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        print(f"{title}: {text}")


def single_instance() -> object | None:
    """Named mutex; returns None if another copy is already running."""
    if sys.platform != "win32":
        return object()
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    handle = k32.CreateMutexW(None, False, "Local\\ScribePlusSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def dpi_aware() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def console_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def open_path(path: Path | str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        log.warning("could not open %s: %s", path, exc)


def make_icon(state: str = "idle"):
    from scribe_plus.icons import make_icon as draw
    return draw(state)


class App(TrayApp):
    # -- OS hooks --------------------------------------------------------------

    def _init_ui(self) -> None:
        import tkinter as tk

        from scribe_plus.win.overlay import Pill
        from scribe_plus.win.platform import UiBridge
        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.bridge = UiBridge(self.root)
        self.pill = Pill(self.root)

    def _make_tray_icon(self):
        return self.pystray.Icon("scribe-plus", icon=make_icon("idle"), title=APP_TITLE)

    def _make_platform(self):
        from scribe_plus.win.platform import WinPlatform
        return WinPlatform(self.settings, self.bridge, self.pill,
                           notifier=self.notify, tray_state=self._tray_state)

    def _make_hook(self):
        from scribe_plus.win import keys
        return keys.KeyboardHook(self.machine, self.engine.post)

    def _physically_down(self, vk: int) -> bool:
        from scribe_plus.win import keys
        return keys.physically_down(vk)

    def make_icon(self, state: str):
        return make_icon(state)

    def open_path(self, path: Path | str) -> None:
        open_path(path)

    def ask_secret(self, title: str, prompt: str) -> str | None:
        from scribe_plus.win.dialogs import ask_secret
        return ask_secret(self.root, title, prompt)

    def confirm(self, title: str, message: str) -> bool:
        from scribe_plus.win.dialogs import confirm
        return confirm(self.root, title, message)

    def run_benchmark(self) -> None:
        script = S.APP_DIR / "benchmark.py"
        try:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen([console_python(), str(script)], cwd=str(S.APP_DIR),
                             creationflags=flags)
        except Exception as exc:
            self.notify("Benchmark failed to start", str(exc)[:150])

    def quit(self) -> None:
        log.info("quitting")
        try:
            self.hook.stop()
            self.icon.stop()
        except Exception:
            pass
        # Same as upstream: the hook thread can sit inside WinAPI, so exit hard.
        # The audio child exits by itself when our end of its stdin closes.
        os._exit(0)

    # Kept for older callers and tests.
    def _quit(self, *_):
        self.quit()

    def _run_benchmark(self, *_):
        self.run_benchmark()

    # -- run ---------------------------------------------------------------

    def run(self) -> None:
        self.hook.start()
        self.icon.run_detached()
        self._warm_up()
        threading.Thread(target=keep_start_menu_entry, name="start-menu", daemon=True).start()
        self.root.after(1500, self._first_run_checks)
        self.root.mainloop()


def app_identity() -> None:
    """Notifications say "Scribe Plus" instead of "Python". Must run before
    the first window (tray icon, pill) is created."""
    if sys.platform != "win32":
        return
    from scribe_plus.win import shell
    try:
        shell.register_identity()
    except Exception as exc:
        log.info("could not register the app identity: %s", exc)
    shell.set_process_identity()


def keep_start_menu_entry() -> None:
    """Put back the Start menu entry if it's missing, or fix it if this copy
    of Scribe Plus has moved."""
    if sys.platform != "win32":
        return
    from scribe_plus.win import shell
    try:
        changed = shell.ensure_installed()
        if changed:
            log.info("shortcuts updated: %s", ", ".join(changed))
    except Exception as exc:
        log.warning("could not update the Start menu entry: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scribe Plus (Windows)")
    parser.add_argument("--debug", action="store_true", help="log to the console too")
    args = parser.parse_args()
    setup_logging(args.debug)
    mutex = single_instance()
    if mutex is None:
        message_box("Scribe Plus is already running. Look for the five-bar icon in the "
                    "system tray (click ^ next to the clock if it's hidden).")
        return
    dpi_aware()
    app_identity()
    try:
        app = App(args.debug)
    except Exception as exc:
        log.exception("startup failed")
        message_box(f"Scribe Plus couldn't start:\n\n{exc}\n\nDetails: {S.LOG_FILE}")
        return
    app._mutex = mutex  # keep the handle alive for the life of the process
    app.run()


if __name__ == "__main__":
    main()
