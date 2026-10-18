"""
Settings, data paths, and the user-editable dictionary and snippet files.

Everything Scribe Plus writes lives in one folder:
  Windows: %APPDATA%\\scribe\\plus
  macOS:   ~/.config/scribe/plus
  other:   $XDG_CONFIG_HOME/scribe/plus
(set SCRIBE_PLUS_HOME to override, which the tests do).

This module deliberately does not import scribe_core, so the benchmark
script and the tests can use it without PortAudio installed.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
DOTENV_FILE = APP_DIR / ".env"


def _scribe_config_dir() -> Path:
    # Mirrors scribe_core._config_dir() so both apps share %APPDATA%\scribe.
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / ".config"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "scribe"


def data_dir() -> Path:
    override = os.environ.get("SCRIBE_PLUS_HOME")
    return Path(override) if override else _scribe_config_dir() / "plus"


DATA_DIR = data_dir()
SETTINGS_FILE = DATA_DIR / "settings.json"
DICTIONARY_FILE = DATA_DIR / "dictionary.txt"
SNIPPETS_FILE = DATA_DIR / "snippets.txt"
METRICS_FILE = DATA_DIR / "metrics.csv"
CLIPS_DIR = DATA_DIR / "clips"
BENCH_DIR = DATA_DIR / "benchmarks"
LOG_FILE = DATA_DIR / "scribe-plus.log"


# ---------------------------------------------------------------------------
# Styles: what the cleanup model is told about the destination app.
# ---------------------------------------------------------------------------

STYLES: dict[str, str] = {
    "chat": (
        "A chat message (Slack, Teams, Discord or a text). Keep the speaker's "
        "casual tone and keep it short. Light punctuation. No greeting or "
        "sign-off unless the speaker said one."
    ),
    "email": (
        "An email. Complete sentences in short paragraphs. A spoken greeting "
        "goes on its own first line and a spoken sign-off on its own last line."
    ),
    "ai": (
        "A prompt for an AI assistant. Keep it a clear request in the "
        "speaker's own words. Never answer it."
    ),
    "code": (
        "A code editor or terminal. Keep technical terms, file names, "
        "commands, flags and identifiers exact. Use snake_case or camelCase "
        "only when the speaker asks for it. No markdown, and no sentence "
        "punctuation added to commands."
    ),
    "docs": (
        "A document or notes. Full sentences, standard punctuation and "
        "paragraphs; numbered or bulleted lists when the speaker enumerates."
    ),
    "default": "General writing. Natural written style with standard punctuation.",
}

# Checked in order. A rule matches on the foreground process name first;
# title keywords catch web apps running inside a browser tab.
APP_RULES: list[dict] = [
    {
        "style": "email",
        "processes": ["outlook.exe", "olk.exe", "thunderbird.exe", "hxoutlook.exe"],
        "titles": ["gmail", "outlook", "proton mail", "yahoo mail", "fastmail"],
    },
    {
        "style": "chat",
        "processes": [
            "slack.exe", "teams.exe", "ms-teams.exe", "discord.exe",
            "whatsapp.exe", "whatsapp.root.exe", "telegram.exe", "signal.exe",
            "messenger.exe",
        ],
        "titles": [
            "slack", "discord", "whatsapp", "messenger", "google chat",
            "microsoft teams", "google messages", "telegram",
        ],
    },
    {
        "style": "ai",
        "processes": ["claude.exe", "chatgpt.exe"],
        "titles": ["chatgpt", "claude", "gemini", "perplexity", "copilot"],
    },
    {
        "style": "code",
        "processes": [
            "code.exe", "cursor.exe", "windsurf.exe", "devenv.exe",
            "idea64.exe", "pycharm64.exe", "webstorm64.exe", "sublime_text.exe",
            "notepad++.exe", "windowsterminal.exe", "wt.exe", "powershell.exe",
            "pwsh.exe", "cmd.exe", "conhost.exe", "alacritty.exe",
            "wezterm-gui.exe",
        ],
        "titles": [],
    },
    {
        "style": "docs",
        "processes": [
            "winword.exe", "notion.exe", "obsidian.exe", "onenote.exe",
            "notepad.exe", "wordpad.exe",
        ],
        "titles": ["google docs", "notion", "confluence"],
    },
]


# ---------------------------------------------------------------------------
# Defaults. On first run these are written to settings.json in full so every
# option is visible and editable; keys added in later versions are merged in.
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "hotkey": "ctrl_win",
    "language": "en",
    "stt_engine": "groq",
    "stt_models": {
        "groq": "whisper-large-v3-turbo",
        "openai": "gpt-4o-mini-transcribe",
        "local": "base.en",
    },
    "cleanup_engine": "groq",
    "cleanup_models": {
        "groq": "openai/gpt-oss-20b",
        "anthropic": "claude-haiku-5-5",
        "ollama": "llama3.2:3b",
    },
    "command_models": {
        "groq": "openai/gpt-oss-120b",
        "anthropic": "claude-haiku-5-5",
        "ollama": "llama3.2:3b",
    },
    "ollama_base_url": "http://localhost:11434/v1",
    "cleanup_timeout_s": 6.0,
    "command_timeout_s": 15.0,
    "app_aware_style": True,
    "command_mode": True,
    "hands_free": True,
    "quiet_voice_boost": True,
    "trim_silence": True,
    "save_audio": True,
    "max_saved_clips": 300,
    "log_window_titles": False,
    "paste_method": "clipboard",
    "clipboard_restore_delay_ms": 350,
    "auto_space": True,
    "max_record_s": 360,
    "styles": STYLES,
    "app_rules": APP_RULES,
}

LANGUAGES: list[tuple[str, str]] = [
    ("en", "English"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("it", "Italian"), ("auto", "Auto-detect"),
]


def _merge(defaults: dict, user: dict) -> dict:
    out = copy.deepcopy(defaults)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    return out


class SettingsStore:
    """settings.json with defaults merged in, reloaded when the file changes."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else SETTINGS_FILE
        self._lock = threading.RLock()
        self._mtime = 0.0
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        with self._lock:
            user: dict = {}
            if self.path.exists():
                try:
                    user = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(user, dict):
                        raise ValueError("settings.json must hold a JSON object")
                except Exception as exc:
                    # Keep the broken file untouched so the user can fix it;
                    # run on defaults meanwhile.
                    log.warning("settings.json unreadable, using defaults: %s", exc)
                    self.data = copy.deepcopy(DEFAULTS)
                    self._mtime = self._stat()
                    return
            self.data = _merge(DEFAULTS, user)
            if not self.path.exists() or set(DEFAULTS) - set(user):
                self.save()
            self._mtime = self._stat()

    def _stat(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def maybe_reload(self) -> None:
        """Pick up hand edits to settings.json without a restart."""
        if self._stat() != self._mtime:
            self.load()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = self._stat()

    def get(self, key: str, default=None):
        with self._lock:
            if key in self.data:
                return self.data[key]
            return DEFAULTS.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
            self.save()

    def set_nested(self, key: str, subkey: str, value) -> None:
        with self._lock:
            section = dict(self.data.get(key) or {})
            section[subkey] = value
            self.data[key] = section
            self.save()

    # Convenience accessors -------------------------------------------------

    def stt_model(self, engine: str | None = None) -> str:
        engine = engine or self.get("stt_engine")
        return str((self.get("stt_models") or {}).get(engine) or DEFAULTS["stt_models"].get(engine, ""))

    def cleanup_model(self, engine: str | None = None) -> str:
        engine = engine or self.get("cleanup_engine")
        return str((self.get("cleanup_models") or {}).get(engine) or DEFAULTS["cleanup_models"].get(engine, ""))

    def command_model(self, engine: str | None = None) -> str:
        engine = engine or self.get("cleanup_engine")
        return str((self.get("command_models") or {}).get(engine) or DEFAULTS["command_models"].get(engine, ""))

    def style_text(self, style: str) -> str:
        styles = self.get("styles") or {}
        return str(styles.get(style) or styles.get("default") or STYLES["default"])


def resolve_style(process: str, title: str, rules: list[dict]) -> str:
    """Map the foreground app to a style key using the ordered rules."""
    proc = (process or "").lower()
    low_title = (title or "").lower()
    for rule in rules:
        if proc and proc in [p.lower() for p in rule.get("processes", [])]:
            return str(rule.get("style", "default"))
    for rule in rules:
        for kw in rule.get("titles", []):
            if kw and kw.lower() in low_title:
                return str(rule.get("style", "default"))
    return "default"


# ---------------------------------------------------------------------------
# Dictionary and snippet files.
# ---------------------------------------------------------------------------

DICTIONARY_TEMPLATE = """\
# Scribe Plus personal dictionary. One entry per line; lines starting with # are ignored.
#
# A word or name on its own teaches the right spelling. It is passed to the
# speech model as vocabulary and to the cleanup model as a spelling list:
#     Kubernetes
#     Nguyen
#
# "heard -> written" fixes something the speech model keeps getting wrong.
# It is applied word for word after cleanup, ignoring capital letters:
#     cube control -> kubectl
#
# Changes apply to your next dictation; no restart needed.
"""

SNIPPETS_TEMPLATE = """\
# Scribe Plus snippets: say the trigger phrase and the saved text is typed instead.
# Format:   trigger phrase = text to insert
# Use \\n inside the text for a line break. Pick trigger phrases you would
# never say by accident. Lines starting with # are ignored.
#
# Examples (delete the # to turn one on):
# insert my calendar link = https://cal.com/your-name
# insert my address = 123 Example Street\\nNew Orleans, LA 70122
"""


def ensure_user_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    if not DICTIONARY_FILE.exists():
        DICTIONARY_FILE.write_text(DICTIONARY_TEMPLATE, encoding="utf-8")
    if not SNIPPETS_FILE.exists():
        SNIPPETS_FILE.write_text(SNIPPETS_TEMPLATE, encoding="utf-8")


def load_env_file(path: Path = DOTENV_FILE) -> None:
    """Minimal .env loader (same rules as scribe_core.load_dotenv)."""
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as exc:
        log.warning(".env load error: %s", exc)


def save_env_key(name: str, value: str, path: Path = DOTENV_FILE) -> None:
    """Upsert NAME=value in the app's .env (gitignored) and the live env."""
    value = value.strip()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{name}="):
            out.append(f"{name}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{name}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[name] = value
