"""
The tray (Windows) / menu bar (Mac) app, minus the OS-specific parts.

TrayApp wires settings, Scribe's recorder and history, the dictation
pipeline, the hotkey state machine and the menu together. Each OS supplies
a subclass (scribe_plus_windows.App, scribe_plus_mac.App) that provides the
window toolkit, the keyboard hook, the tray icon, dialogs and file opening.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from scribe_plus import __version__
from scribe_plus import settings as S

log = logging.getLogger("scribe_plus")

APP_TITLE = "Scribe Plus"
DOWNLOAD_PAGE = "https://scribeplus.feetham.us"

STT_CHOICES = [
    ("Groq Whisper Turbo (fastest)", "groq", "whisper-large-v3-turbo"),
    ("Groq Whisper Large v3 (most accurate)", "groq", "whisper-large-v3"),
    ("OpenAI gpt-4o-mini-transcribe", "openai", "gpt-4o-mini-transcribe"),
    ("Local Whisper on this computer (offline)", "local", None),
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


class TrayApp:
    """Shared app logic. Subclasses must implement the hooks marked below."""

    paste_last_label = "Ctrl+Alt+V"

    def __init__(self, debug: bool) -> None:
        import pystray

        import scribe_core as core
        from scribe_plus import engines
        from scribe_plus.hotkeys import HotkeyMachine, presets_for
        from scribe_plus.metrics import MetricsLog
        from scribe_plus.pipeline import CoreHooks, DictationEngine

        self.pystray, self.core, self.engines = pystray, core, engines
        self.PRESETS, self.DEFAULT_HOTKEY = presets_for(sys.platform)
        self.debug = debug
        core.load_dotenv()
        S.load_env_file()
        S.ensure_user_files()
        self.settings = S.SettingsStore()
        self._mics: list[str] | None = None

        self._init_ui()
        self.icon = self._make_tray_icon()
        self.platform = self._make_platform()

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
        self.engine = DictationEngine(self.settings, self.platform, hooks,
                                      metrics=MetricsLog(S.METRICS_FILE))
        self.machine = HotkeyMachine(
            self._spec(), hands_free=bool(self.settings.get("hands_free")),
            command_mode=bool(self.settings.get("command_mode")),
            max_record_s=float(self.settings.get("max_record_s") or 360),
            is_down=self._physically_down, paste_groups=self._paste_groups(),
        )
        self.hook = self._make_hook()
        self.icon.menu = self._menu()

    # -- OS hooks: subclasses implement these ------------------------------

    def _init_ui(self) -> None:
        """Create the UI toolkit objects (self.bridge, self.pill, ...)."""
        raise NotImplementedError

    def _make_tray_icon(self):
        raise NotImplementedError

    def _make_platform(self):
        raise NotImplementedError

    def _make_hook(self):
        """Keyboard hook with .start(), .stop() and a .lock."""
        raise NotImplementedError

    def _physically_down(self, vk: int) -> bool:
        return True

    def _paste_groups(self) -> tuple:
        from scribe_plus.hotkeys import PASTE_LAST_WINDOWS
        return PASTE_LAST_WINDOWS

    def make_icon(self, state: str):
        raise NotImplementedError

    def open_path(self, path: Path | str) -> None:
        raise NotImplementedError

    def ui_call(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run fn on the UI thread."""
        self.bridge.call(fn, *args)

    def ask_secret(self, title: str, prompt: str) -> str | None:
        """Modal text prompt (runs on the UI thread)."""
        raise NotImplementedError

    def confirm(self, title: str, message: str) -> bool:
        """Modal yes/no (runs on the UI thread)."""
        raise NotImplementedError

    def run_benchmark(self) -> None:
        raise NotImplementedError

    def extra_menu_items(self) -> list:
        """OS-specific items placed above API keys (Mac: Permissions)."""
        return []

    def quit(self) -> None:
        raise NotImplementedError

    # -- startup ---------------------------------------------------------------

    def _first_run_checks(self) -> None:
        needs = {self.settings.get("stt_engine"), self.settings.get("cleanup_engine")}
        if "groq" in needs and not self.engines.api_key("groq"):
            self.notify(APP_TITLE, "Add your free Groq API key to start dictating.")
            self._ask_key("Groq", "GROQ_API_KEY", "https://console.groq.com/keys")
        else:
            self.notify(APP_TITLE, f"Ready. Hold {self._spec().label} and talk.")

    def _warm_up(self) -> None:
        self.core.prewarm_recorder()
        if self.settings.get("stt_engine") == "local":
            self.engines.LOCAL_WHISPER.preload(self.settings.stt_model("local"))
        log.info("Scribe Plus %s ready: hold %s to dictate", __version__, self._spec().label)

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
            self.icon.icon = self.make_icon(icon_state)
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
                     checked=self._is_value("hotkey", hid) if hid != self.DEFAULT_HOTKEY
                     else self._is_default_hotkey(), radio=True)
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
                    I(f"Copy last dictation  ({self.paste_last_label} pastes it)", self._copy_last),
                    I("Open history file", lambda *_: self.open_path(self.core.HISTORY_FILE)),
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
            I("Edit dictionary...", lambda *_: self.open_path(S.DICTIONARY_FILE)),
            I("Edit snippets...", lambda *_: self.open_path(S.SNIPPETS_FILE)),
            I("Edit all settings...", lambda *_: self.open_path(S.SETTINGS_FILE)),
            I("Open latency log", self._open_metrics),
            I("Run benchmark...", lambda *_: self.run_benchmark()),
            I("Check setup", self._check_setup),
            I("Open data folder", lambda *_: self.open_path(S.DATA_DIR)),
            M.SEPARATOR,
            *self.extra_menu_items(),
            I("API keys", M(*keys_menu)),
            I("Resume dictation" if self.engine.paused else "Pause dictation", self._toggle_pause),
            I(f"Scribe Plus {__version__}: get updates...",
              lambda *_: self.open_path(DOWNLOAD_PAGE)),
            I(f"Quit {APP_TITLE}", lambda *_: self.quit()),
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
                            self.engines.LOCAL_WHISPER_HINT[0].upper()
                            + self.engines.LOCAL_WHISPER_HINT[1:] + ".")
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

    def _is_default_hotkey(self):
        # A hotkey id from the other OS (a shared settings file) counts as the default.
        return lambda _i: self.settings.get("hotkey") not in self.PRESETS or \
            self.settings.get("hotkey") == self.DEFAULT_HOTKEY

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
            self.ui_call(self._rebuild)
        threading.Thread(target=work, daemon=True).start()

    def _copy_cb(self, text):
        def cb(_icon, _item):
            self.platform.copy(text)
            self.notify("Copied", text[:90])
        return cb

    def _clear_history(self, *_):
        def ask() -> None:
            if self.confirm("Clear dictation history?",
                            "This deletes every saved transcript. The latency log "
                            "and saved clips are kept."):
                self.core.clear_history()
                self._rebuild()
        self.ui_call(ask)

    def _key_cb(self, name, env, url):
        return lambda *_: self._ask_key(name, env, url)

    def _ask_key(self, name: str, env: str, url: str) -> None:
        def ask() -> None:
            val = self.ask_secret(
                f"{APP_TITLE}: {name} API key",
                f"Paste your {name} API key. Get one at {url}\n\n"
                f"It is saved only in Scribe Plus's own .env file on this computer.")
            if val:
                S.save_env_key(env, val)
                self.notify(f"{name} key saved", "You're set. Hold the hotkey and talk.")
                self._rebuild()
        self.ui_call(ask)

    def _open_metrics(self, *_):
        if not S.METRICS_FILE.exists():
            self.notify("No log yet", "Dictate something first.")
            return
        self.open_path(S.METRICS_FILE)

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
