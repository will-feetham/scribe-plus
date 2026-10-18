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
import logging.handlers
import os
import subprocess
import sys
import threading
from pathlib import Path

from scribe_plus import settings as S

log = logging.getLogger("scribe_plus")

APP_TITLE = "Scribe Plus"


# ---------------------------------------------------------------------------
# Logging: pythonw has no console, so everything (including upstream
# scribe_core's prints) goes to %APPDATA%\scribe\plus\scribe-plus.log.
# ---------------------------------------------------------------------------


class _LogWriter:
    """File-like object that turns print() output into log lines.

    Re-entrancy guard: if writing the log ever fails (Windows can refuse a
    log rollover while another program has the log open), logging reports
    the error by printing to stderr, which is this object again. Without the
    guard that loops until a RecursionError kills the calling thread.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self.logger, self.level, self._buf = logger, level, ""
        self._local = threading.local()

    def write(self, msg: str) -> int:
        if getattr(self._local, "busy", False):
            return len(msg)
        self._local.busy = True
        try:
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self.logger.log(self.level, line.rstrip())
        except Exception:
            pass
        finally:
            self._local.busy = False
        return len(msg)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("log writer has no file descriptor")


def setup_logging(debug: bool) -> None:
    # A failing log write must never take the app down with it.
    logging.raiseExceptions = False
    S.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        S.LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if debug and sys.__stderr__ is not None:
        handlers.append(logging.StreamHandler(sys.__stderr__))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if sys.stdout is None or not debug:
        sys.stdout = _LogWriter(logging.getLogger("stdout"), logging.INFO)
    if sys.stderr is None or not debug:
        sys.stderr = _LogWriter(logging.getLogger("scribe"), logging.INFO)


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


def open_path(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        log.warning("could not open %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------


def make_icon(state: str = "idle"):
    from scribe_plus.win.icons import make_icon as draw
    return draw(state)


STT_CHOICES = [
    ("Groq Whisper Turbo (fastest)", "groq", "whisper-large-v3-turbo"),
    ("Groq Whisper Large v3 (most accurate)", "groq", "whisper-large-v3"),
    ("OpenAI gpt-4o-mini-transcribe", "openai", "gpt-4o-mini-transcribe"),
    ("Local Whisper on this PC (offline)", "local", None),
]
CLEANUP_CHOICES = [
    ("Groq gpt-oss-20b (fastest)", "groq", "openai/gpt-oss-20b"),
    ("Groq gpt-oss-120b (smarter)", "groq", "openai/gpt-oss-120b"),
    ("Claude Haiku 5.5", "anthropic", "claude-haiku-5-5"),
    ("Local model via Ollama", "ollama", None),
    ("Off (raw transcript)", "off", None),
]
FEATURE_TOGGLES = [
    ("App-aware style", "app_aware_style"),
    ("Command Mode", "command_mode"),
    ("Hands-free double-tap", "hands_free"),
    ("Quiet-voice boost", "quiet_voice_boost"),
    ("Trim silence before upload", "trim_silence"),
    ("Smart spacing between dictations", "auto_space"),
    ("Save audio for benchmarks", "save_audio"),
]
KEY_NAMES = [("Groq", "GROQ_API_KEY", "https://console.groq.com/keys"),
             ("Anthropic", "ANTHROPIC_API_KEY", "https://platform.claude.com/"),
             ("OpenAI", "OPENAI_API_KEY", "https://platform.openai.com/api-keys")]


class App:
    def __init__(self, debug: bool) -> None:
        import tkinter as tk

        import pystray
        import scribe_core as core
        from scribe_plus import engines
        from scribe_plus.hotkeys import DEFAULT_HOTKEY, PRESETS, HotkeyMachine
        from scribe_plus.metrics import MetricsLog
        from scribe_plus.pipeline import CoreHooks, DictationEngine
        from scribe_plus.win import keys
        from scribe_plus.win.overlay import Pill
        from scribe_plus.win.platform import UiBridge, WinPlatform

        self.tk, self.pystray, self.core, self.engines = tk, pystray, core, engines
        self.PRESETS, self.DEFAULT_HOTKEY = PRESETS, DEFAULT_HOTKEY
        self.debug = debug
        core.load_dotenv()
        S.load_env_file()
        S.ensure_user_files()
        self.settings = S.SettingsStore()
        self._mics: list[str] | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.bridge = UiBridge(self.root)
        self.pill = Pill(self.root)

        self.icon = pystray.Icon("scribe-plus", icon=make_icon("idle"), title=APP_TITLE)
        self.platform = WinPlatform(self.settings, self.bridge, self.pill,
                                    notifier=self.notify, tray_state=self._tray_state)

        def last_text() -> str:
            rows = core.load_history(limit=1)
            return rows[0]["text"] if rows else ""

        hooks = CoreHooks(
            recorder_factory=core.Recorder,
            append_history=core.append_history,
            last_history_text=last_text,
            is_garbage=core.is_garbage,
            save_unsent=core.save_unsent_recording,
        )
        self.engine = DictationEngine(self.settings, self.platform, hooks, metrics=MetricsLog(S.METRICS_FILE))
        spec = PRESETS.get(self.settings.get("hotkey"), PRESETS[DEFAULT_HOTKEY])
        self.machine = HotkeyMachine(
            spec, hands_free=bool(self.settings.get("hands_free")),
            command_mode=bool(self.settings.get("command_mode")),
            max_record_s=float(self.settings.get("max_record_s") or 360),
            is_down=keys.physically_down,
        )
        self.hook = keys.KeyboardHook(self.machine, self.engine.post)
        self.icon.menu = self._menu()

    # -- run ---------------------------------------------------------------

    def run(self) -> None:
        self.hook.start()
        self.icon.run_detached()
        self.core.prewarm_recorder()
        if self.settings.get("stt_engine") == "local":
            self.engines.LOCAL_WHISPER.preload(self.settings.stt_model("local"))
        spec = self._spec()
        log.info("Scribe Plus ready: hold %s to dictate", spec.label)
        self.root.after(1500, self._first_run_checks)
        self.root.mainloop()

    def _first_run_checks(self) -> None:
        needs = {self.settings.get("stt_engine"), self.settings.get("cleanup_engine")}
        if "groq" in needs and not self.engines.api_key("groq"):
            self.notify(APP_TITLE, "Add your free Groq API key to start dictating.")
            self._ask_key("Groq", "GROQ_API_KEY", "https://console.groq.com/keys")
        else:
            self.notify(APP_TITLE, f"Ready. Hold {self._spec().label} and talk.")

    # -- helpers -------------------------------------------------------------

    def _spec(self):
        return self.PRESETS.get(self.settings.get("hotkey"), self.PRESETS[self.DEFAULT_HOTKEY])

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            log.info("notify: %s: %s", title, message)

    def _tray_state(self, state: str) -> None:
        mapping = {"recording": "recording", "processing": "processing", "error": "error"}
        icon_state = "paused" if self.engine.paused else mapping.get(state, "idle")
        try:
            self.icon.icon = make_icon(icon_state)
        except Exception:
            pass

    def _rebuild(self) -> None:
        try:
            self.icon.menu = self._menu()
            self.icon.update_menu()
        except Exception:
            log.exception("menu rebuild failed")

    def _reconfigure_hotkeys(self) -> None:
        with self.hook.lock:
            self.machine.configure(
                self._spec(), hands_free=bool(self.settings.get("hands_free")),
                command_mode=bool(self.settings.get("command_mode")),
                max_record_s=float(self.settings.get("max_record_s") or 360))
            self.machine.reset()

    # -- menu ------------------------------------------------------------------

    def _menu(self):
        M, I = self.pystray.Menu, self.pystray.MenuItem
        spec = self._spec()
        status = "Paused" if self.engine.paused else f"Hold {spec.label} to dictate"
        local_ok = self.engines.LOCAL_WHISPER.available()

        stt = [I(label + ("" if engine != "local" or local_ok else " (not installed)"),
                 self._set_stt(engine, model), checked=self._is_stt(engine, model), radio=True)
               for label, engine, model in STT_CHOICES]
        cleanup = [I(label, self._set_cleanup(engine, model),
                     checked=self._is_cleanup(engine, model), radio=True)
                   for label, engine, model in CLEANUP_CHOICES]
        langs = [I(label, self._set_value("language", code),
                   checked=self._is_value("language", code), radio=True)
                 for code, label in S.LANGUAGES]
        hotkeys = [I(f"{p.label}   (Command: {p.command_label})", self._set_hotkey(hid),
                     checked=self._is_value("hotkey", hid), radio=True)
                   for hid, p in self.PRESETS.items()]
        features = [I(label, self._toggle(key), checked=self._is_true(key))
                    for label, key in FEATURE_TOGGLES]
        mics = [I("System default", self._set_mic(""), checked=self._is_mic(""), radio=True)]
        for name in (self._mics or []):
            mics.append(I(name, self._set_mic(name), checked=self._is_mic(name), radio=True))
        mics.append(M.SEPARATOR)
        mics.append(I("Refresh list", self._refresh_mics))

        history = []
        for row in self.core.load_history(limit=10):
            text = row.get("text", "")
            preview = text if len(text) <= 60 else text[:57] + "..."
            history.append(I(f"Copy: {preview}", self._copy_cb(text)))
        if not history:
            history.append(I("(nothing dictated yet)", None, enabled=False))
        history += [M.SEPARATOR,
                    I("Copy last dictation  (Ctrl+Alt+V pastes it)", self._copy_last),
                    I("Open history file", lambda *_: open_path(self.core.HISTORY_FILE)),
                    I("Clear history...", self._clear_history)]

        keys_menu = [I(f"{name} key...", self._key_cb(name, env, url))
                     for name, env, url in KEY_NAMES]

        return M(
            I(status, None, enabled=False),
            M.SEPARATOR,
            I("Transcription", M(*stt)),
            I("Cleanup model", M(*cleanup)),
            I("Language", M(*langs)),
            I("Hotkey", M(*hotkeys)),
            I("Microphone", M(*mics)),
            I("Features", M(*features)),
            I("History", M(*history)),
            M.SEPARATOR,
            I("Edit dictionary...", lambda *_: open_path(S.DICTIONARY_FILE)),
            I("Edit snippets...", lambda *_: open_path(S.SNIPPETS_FILE)),
            I("Edit all settings...", lambda *_: open_path(S.SETTINGS_FILE)),
            I("Open latency log", self._open_metrics),
            I("Run benchmark...", self._run_benchmark),
            I("Check setup", self._check_setup),
            I("Open data folder", lambda *_: open_path(S.DATA_DIR)),
            M.SEPARATOR,
            I("API keys", M(*keys_menu)),
            I("Resume dictation" if self.engine.paused else "Pause dictation", self._toggle_pause),
            I(f"Quit {APP_TITLE}", self._quit),
        )

    def _copy_last(self, *_):
        rows = self.core.load_history(limit=1)
        if not rows:
            self.notify("Nothing to copy", "History is empty.")
            return
        self.platform.copy(rows[0]["text"])
        self.notify("Copied last dictation", rows[0]["text"][:90])

    def _is_stt(self, engine, model):
        return lambda _i: (self.settings.get("stt_engine") == engine
                           and (model is None or self.settings.stt_model(engine) == model))

    def _set_stt(self, engine, model):
        def cb(_icon, _item):
            if engine == "local" and not self.engines.LOCAL_WHISPER.available():
                self.notify("Local Whisper not installed",
                            "Run 'Install local Whisper.bat' in the Scribe Plus folder first.")
                return
            if model:
                self.settings.set_nested("stt_models", engine, model)
            self.settings.set("stt_engine", engine)
            if engine == "local":
                self.engines.LOCAL_WHISPER.preload(self.settings.stt_model("local"))
            self._rebuild()
        return cb

    def _is_cleanup(self, engine, model):
        return lambda _i: (self.settings.get("cleanup_engine") == engine
                           and (model is None or self.settings.cleanup_model(engine) == model))

    def _set_cleanup(self, engine, model):
        def cb(_icon, _item):
            if model:
                self.settings.set_nested("cleanup_models", engine, model)
            self.settings.set("cleanup_engine", engine)
            if engine in ("groq", "anthropic") and not self.engines.api_key(engine):
                name = "Groq" if engine == "groq" else "Anthropic"
                self.notify(f"{name} key needed", f"Add it from API keys > {name} key...")
            self._rebuild()
        return cb

    def _is_value(self, key, value):
        return lambda _i: self.settings.get(key) == value

    def _set_value(self, key, value):
        def cb(_icon, _item):
            self.settings.set(key, value)
            self._rebuild()
        return cb

    def _set_hotkey(self, hotkey_id):
        def cb(_icon, _item):
            self.settings.set("hotkey", hotkey_id)
            self._reconfigure_hotkeys()
            self._rebuild()
            spec = self._spec()
            self.notify("Hotkey changed", f"Hold {spec.label} to dictate; "
                                          f"{spec.command_label} for Command Mode.")
        return cb

    def _is_true(self, key):
        return lambda _i: bool(self.settings.get(key))

    def _toggle(self, key):
        def cb(_icon, _item):
            self.settings.set(key, not bool(self.settings.get(key)))
            if key in ("hands_free", "command_mode"):
                self._reconfigure_hotkeys()
            self._rebuild()
        return cb

    def _is_mic(self, name):
        return lambda _i: self.core.load_input_device() == name

    def _set_mic(self, name):
        def cb(_icon, _item):
            self.core.save_input_device(name)
            self._rebuild()
        return cb

    def _refresh_mics(self, *_):
        def work() -> None:
            names = self.core.list_input_devices()
            if names is None:
                self.notify("Microphones", "Couldn't read the device list; see the log.")
                return
            self._mics = names
            self._rebuild()
        threading.Thread(target=work, daemon=True).start()

    def _copy_cb(self, text):
        def cb(_icon, _item):
            self.platform.copy(text)
            self.notify("Copied", text[:90])
        return cb

    def _clear_history(self, *_):
        def ask() -> None:
            from scribe_plus.win.dialogs import confirm
            if confirm(self.root, "Clear dictation history?",
                       "This deletes every saved transcript. The latency log "
                       "and saved clips are kept."):
                self.core.clear_history()
                self._rebuild()
        self.bridge.call(ask)

    def _key_cb(self, name, env, url):
        return lambda *_: self._ask_key(name, env, url)

    def _ask_key(self, name: str, env: str, url: str) -> None:
        def ask() -> None:
            from scribe_plus.win.dialogs import ask_secret
            val = ask_secret(
                self.root, f"{APP_TITLE}: {name} API key",
                f"Paste your {name} API key. Get one at {url}\n\n"
                f"It is saved only in this folder's .env file, which git ignores.")
            if val:
                S.save_env_key(env, val)
                self.notify(f"{name} key saved", "You're set. Hold the hotkey and talk.")
                self._rebuild()
        self.bridge.call(ask)

    def _open_metrics(self, *_):
        if not S.METRICS_FILE.exists():
            self.notify("No log yet", "Dictate something first.")
            return
        open_path(S.METRICS_FILE)

    def _run_benchmark(self, *_):
        script = S.APP_DIR / "benchmark.py"
        try:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen([console_python(), str(script)], cwd=str(S.APP_DIR),
                             creationflags=flags)
        except Exception as exc:
            self.notify("Benchmark failed to start", str(exc)[:150])

    def _check_setup(self, *_):
        def work() -> None:
            self.settings.maybe_reload()
            lines = self.engines.check_setup(self.settings, self.engine.pool)
            for ok, line in lines:
                log.info("check setup: %s %s", "OK " if ok else "FAIL", line)
            bad = [line for ok, line in lines if not ok]
            if bad:
                self.notify("Setup problem", "\n".join(bad)[:250])
            else:
                self.notify("Setup OK", "\n".join(line for _ok, line in lines)[:250])
        threading.Thread(target=work, daemon=True).start()

    def _toggle_pause(self, *_):
        self.engine.paused = not self.engine.paused
        self._tray_state("idle")
        self._rebuild()

    def _quit(self, *_):
        log.info("quitting")
        try:
            self.hook.stop()
            self.icon.stop()
        except Exception:
            pass
        # Same as upstream: the hook thread can sit inside WinAPI, so exit hard.
        # The audio child exits by itself when our end of its stdin closes.
        os._exit(0)


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
