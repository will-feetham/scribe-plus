"""
Runs call recording: notices a call, records both sides, hands the finished
transcript over (file, "latest" copy, clipboard, notification).

The OS-specific parts come in through a small platform object:

    available()                     -> (bool, why not)
    start_watch(on_procs) / stop_watch()
    tap_source(detected, system, on_audio, on_event) -> a source (start/stop)
    window_titles(bundles)          -> list of window titles (may be empty)

so everything here runs in tests with fakes. scribe_plus.mac.calls is the
real one.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from scribe_plus import engines, textproc
from scribe_plus.calls import sources
from scribe_plus.calls.detector import DEFAULT_APPS, CallDetector, Detected, matches
from scribe_plus.calls.recorder import CallRecorder
from scribe_plus.calls.transcript import Transcript, TranscriptFiles, duration_words

log = logging.getLogger(__name__)

MANUAL_NAME = "Manual recording"
GROQ_MIN_INTERVAL_S = 3.2      # Groq allows 20 speech requests a minute
ENGINE_LABELS = {
    "mlx": "on this Mac", "local": "on this computer",
    "groq": "by Groq (cloud)", "openai": "by OpenAI (cloud)",
}

def default_calls_dir() -> Path:
    return Path.home() / "Scribe Plus" / "Calls"


@dataclass
class _Call:
    name: str
    detected: Optional[Detected]
    recorder: CallRecorder
    files: TranscriptFiles
    started: datetime
    t0: float
    engine: str
    mic: Any = None
    tap: Any = None
    tap_events: list = field(default_factory=list)


class CallManager:
    def __init__(self, settings, platform, *, notify: Callable[[str, str], None],
                 copy: Callable[[str], None], pool: engines.HttpPool,
                 dictionary: Callable[[], Any] | None = None,
                 mic_device: Callable[[], str] | None = None,
                 mic_factory: Callable[..., Any] | None = None,
                 app_dir: Path | None = None) -> None:
        from scribe_plus import settings as S
        self.settings = settings
        self.platform = platform
        self._notify_fn, self._copy = notify, copy
        self.pool = pool
        self._dictionary = dictionary
        self._mic_device = mic_device or (lambda: "")
        self._mic_factory = mic_factory or sources.mic_source
        self.app_dir = Path(app_dir or S.APP_DIR)
        self.on_change: Callable[[], None] | None = None
        self.last_path: Path | None = None
        self.last_text = ""
        self.detector = CallDetector(self._apps(), start_delay=self._num("calls_start_delay_s", 1.0),
                                     end_delay=self._num("calls_end_delay_s", 1.5))
        self._call: _Call | None = None
        self._busy = False                 # a call is being started or wrapped up
        self._lock = threading.RLock()
        self._quit = threading.Event()
        self._seen: dict[str, str] = {}    # what each watched app was last doing (for the log)
        self._warned_engine = float("-inf")   # when the "can't transcribe" notice was last shown

    # -- settings ---------------------------------------------------------------

    def _num(self, key: str, default: float) -> float:
        try:
            return float(self.settings.get(key, default))
        except (TypeError, ValueError):
            return default

    def _apps(self) -> list[dict]:
        apps = self.settings.get("calls_apps")
        return apps if isinstance(apps, list) and apps else DEFAULT_APPS

    def calls_dir(self) -> Path:
        custom = str(self.settings.get("calls_dir") or "").strip()
        return Path(custom).expanduser() if custom else default_calls_dir()

    def resolve_engine(self) -> tuple[str, str, str]:
        """(engine, model, problem). `engine` is "" when calls can't be transcribed."""
        choice = str(self.settings.get("calls_engine") or "auto")
        from scribe_plus import settings as S
        models = dict(S.DEFAULTS["calls_models"])
        models.update(self.settings.get("calls_models") or {})
        if choice == "auto":
            if engines.MLX_WHISPER.available():
                return "mlx", models["mlx"], ""
            if engines.LOCAL_WHISPER.available():
                return "local", models["local"], ""
            return "", "", f"on-device speech-to-text isn't installed ({engines.MLX_HINT})"
        if choice == "mlx" and not engines.MLX_WHISPER.available():
            return "", "", f"on-device speech-to-text isn't installed ({engines.MLX_HINT})"
        if choice == "local" and not engines.LOCAL_WHISPER.available():
            return "", "", f"local Whisper isn't installed ({engines.LOCAL_WHISPER_HINT})"
        if choice in ("groq", "openai") and not engines.api_key(choice):
            return "", "", f"there is no {choice.capitalize()} API key"
        if choice not in models:
            return "", "", f"unknown calls_engine {choice!r}"
        return choice, models[choice], ""

    # -- lifecycle ----------------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        return self.platform.available()

    def start(self) -> None:
        ok, why = self.available()
        if not ok:
            log.info("call recording unavailable: %s", why)
            return
        self.platform.start_watch(self._on_procs)
        threading.Thread(target=self._tick_loop, name="call-detect", daemon=True).start()
        log.info("watching for calls in: %s",
                 ", ".join(str(a.get("name")) for a in self._apps() if a.get("enabled", True)))

    def shutdown(self) -> None:
        self._quit.set()
        try:
            self.platform.stop_watch()
        except Exception:
            pass
        call = self._call
        if call is not None:
            for src in (call.mic, call.tap):
                try:
                    if src is not None:
                        src.stop(0.5)
                except Exception:
                    pass

    # -- detection ------------------------------------------------------------------

    def _on_procs(self, procs: list[dict]) -> None:
        now = time.monotonic()
        with self._lock:
            self.detector.update(procs, now)
        # One log line whenever a watched app starts or stops using the mic or
        # playing sound: this is what shows how an app behaves around a call.
        for app in self._apps():
            name = str(app.get("name"))
            mine = [p for p in procs if matches(app, p)]
            state = ("mic " if any(p.get("input") for p in mine) else "") + \
                    ("sound" if any(p.get("output") for p in mine) else "")
            state = state.strip() or "idle"
            if self._seen.get(name, "idle") != state:
                self._seen[name] = state
                log.info("call watch: %s is now %s", name, state)

    def _tick_loop(self) -> None:
        while not self._quit.wait(0.25):
            try:
                self._tick(time.monotonic())
            except Exception:
                log.exception("call detection tick failed")

    def _tick(self, now: float) -> None:
        with self._lock:
            self.detector.apps = self._apps()
            event = self.detector.poll(now)
            call = self._call
        if event is not None:
            kind, detected = event
            if kind == "start":
                # When this one isn't recorded (detection off, or a recording is
                # already running) the detector still waits for the app to let
                # go of the microphone before it reports another call.
                if self.settings.get("calls_enabled") and call is None and not self._busy:
                    threading.Thread(target=self._begin, args=(detected,), daemon=True).start()
            elif kind == "stop" and call is not None and call.detected is not None:
                threading.Thread(target=self._end, args=("the app let go of the microphone",),
                                 daemon=True).start()
            return
        if call is not None and (now - call.t0) > 60.0 * self._num("calls_max_minutes", 180):
            threading.Thread(target=self._end, args=("the time limit was reached",),
                             daemon=True).start()

    # -- menu actions -----------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._call is not None

    def status(self) -> dict:
        call = self._call
        if call is None:
            return {"recording": False}
        return {"recording": True, "name": call.name, "started": call.started,
                "lines": call.recorder.lines_added, "manual": call.detected is None}

    def start_manual(self) -> None:
        threading.Thread(target=self._begin, args=(None,), daemon=True).start()

    def stop(self) -> None:
        threading.Thread(target=self._end, args=("stopped from the menu",), daemon=True).start()

    # -- a call ---------------------------------------------------------------------------

    def _notify(self, title: str, message: str, always: bool = False) -> None:
        if always or self.settings.get("calls_notify"):
            try:
                self._notify_fn(title, message)
            except Exception:
                log.info("notify: %s: %s", title, message)

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _transcriber(self, engine: str, model: str) -> Callable[[Any, str], str]:
        def run(samples, speaker: str) -> str:
            dictionary = self._dictionary() if self._dictionary else None
            prompt = textproc.whisper_prompt(dictionary) if dictionary else ""
            res = engines.transcribe_samples(engine, samples, model=model,
                                             language=self.settings.get("language"),
                                             prompt=prompt, pool=self.pool)
            return textproc.strip_prompt_echo(res.text, prompt)
        return run

    def _cleaner(self) -> Callable[[str], str]:
        def run(text: str) -> str:
            dictionary = self._dictionary() if self._dictionary else None
            return textproc.apply_replacements(text, dictionary) if dictionary else text
        return run

    def _begin(self, detected: Optional[Detected]) -> None:
        with self._lock:
            if self._call is not None or self._busy:
                return
            self._busy = True
        try:
            self._begin_locked(detected)
        except Exception as exc:
            log.exception("could not start recording the call")
            self._notify("Call recording failed to start", str(exc)[:160], always=True)
        finally:
            self._busy = False
            self._changed()

    def _begin_locked(self, detected: Optional[Detected]) -> None:
        self.settings.maybe_reload()
        engine, model, problem = self.resolve_engine()
        name = detected.name if detected else MANUAL_NAME
        if not engine:
            log.warning("call in %s not recorded: %s", name, problem)
            if time.monotonic() - self._warned_engine > 600:
                self._warned_engine = time.monotonic()
                self._notify("Call not recorded", f"Scribe Plus noticed a call, but {problem}.",
                             always=True)
            return
        started = datetime.now()
        titles: list[str] = []
        if detected and self.settings.get("calls_window_titles"):
            titles = self._titles(detected)
        transcript = Transcript(name, started, app=detected.name if detected else "",
                                windows=titles, engine=engine)
        files = TranscriptFiles(self.calls_dir(), transcript)
        recorder = CallRecorder(
            transcript, files, self._transcriber(engine, model), clean=self._cleaner(),
            drop_echo=bool(self.settings.get("calls_drop_echo")),
            min_interval_s=GROQ_MIN_INTERVAL_S if engine == "groq" else 0.0,
            audio_dir=self.calls_dir() / "audio" if self.settings.get("calls_keep_audio") else None,
            on_error=self._speech_error)
        call = _Call(name, detected, recorder, files, started, time.monotonic(), engine)
        system = detected is None or str(self.settings.get("calls_capture")) == "system"
        call.tap = self.platform.tap_source(
            detected, system,
            lambda s, r, a: recorder.feed("Them", s, r, a),
            lambda ev: call.tap_events.append(ev) if ev.get("event") != "level" else None)
        call.mic = self._mic_factory(self._mic_device(),
                                     lambda s, r, a: recorder.feed("Me", s, r, a),
                                     app_dir=self.app_dir)
        for label, src in (("the other side", call.tap), ("the microphone", call.mic)):
            try:
                src.start()
            except Exception as exc:
                log.warning("could not start recording %s: %s", label, exc)
                transcript.notes.append(f"Recording {label} could not start ({str(exc)[:100]}).")
        if engine == "mlx":
            if not engines.MLX_WHISPER.downloaded(model):
                self._notify("Getting the speech model",
                             "A one-time 1.5 GB download. This call's transcript appears when it "
                             "is done.", always=True)
            engines.MLX_WHISPER.preload(model)
        with self._lock:
            self._call = call
        log.info("call started: %s (speech-to-text %s, %s; capture %s)", name, engine, model,
                 "all sound" if system else "the app's sound")
        self._notify("Recording a call", f"{name}: both sides, transcribed {ENGINE_LABELS.get(engine, engine)}.")

    def _titles(self, detected: Detected) -> list[str]:
        try:
            return [t for t in self.platform.window_titles(detected.bundles) if t][:4]
        except Exception as exc:
            log.debug("window titles unavailable: %s", exc)
            return []

    def _speech_error(self, exc: Exception) -> None:
        self._notify("Call transcription problem", str(exc)[:160], always=True)

    def _end(self, reason: str) -> None:
        with self._lock:
            call = self._call
            if call is None or self._busy:
                return
            self._busy = True
        t_end = time.monotonic()
        try:
            self._end_locked(call, reason, t_end)
        except Exception:
            log.exception("could not finish the call cleanly")
        finally:
            # The detector is left alone: after a stop from the menu it must not
            # start again until the app lets go of the microphone.
            with self._lock:
                self._call = None
                self._busy = False
            self._changed()

    def _end_locked(self, call: _Call, reason: str, t_end: float) -> None:
        stoppers = [threading.Thread(target=src.stop, daemon=True)
                    for src in (call.mic, call.tap) if src is not None]
        for t in stoppers:
            t.start()
        for t in stoppers:
            t.join(3.0)
        rec, tr = call.recorder, call.recorder.transcript
        if call.detected and self.settings.get("calls_window_titles"):
            for title in self._titles(call.detected):
                if title not in tr.windows:
                    tr.windows.append(title)
        caught_up = rec.finish(timeout=12.0)
        me, them = rec.channels["Me"], rec.channels["Them"]
        hints: list[str] = []
        if them.seconds < 1.0:
            detail = getattr(call.tap, "last_error", "") or "it sent no audio"
            hints.append(f"The other side was not recorded ({detail}).")
        elif them.peak <= 0.0:
            hints.append("The other side was silent for the whole call. If they did speak, turn on "
                         "System Audio Recording for Scribe Plus in System Settings > Privacy & Security.")
        if me.seconds < 1.0:
            hints.append("The microphone was not recorded.")
        elif me.peak <= 0.0:
            hints.append("The microphone was silent for the whole call.")
        if hints:
            tr.notes.extend(hints)
            rec._write()
        ready_s = time.monotonic() - t_end
        summary = rec.summary()
        log.info("call ended: %s (%s); %s; ready %.1f s after it ended; %s%s", call.name, reason,
                 duration_words((tr.ended - tr.started).total_seconds()) if tr.ended else "?",
                 ready_s, summary, "" if caught_up else "; speech model still catching up")
        lines = rec.lines_added
        if lines or hints:
            self.last_path = call.files.path
            self.last_text = tr.render()
        if lines and self.settings.get("calls_copy_on_end"):
            try:
                self._copy(self.last_text)
            except Exception as exc:
                log.warning("could not copy the call transcript: %s", exc)
        if lines:
            copied = " and copied" if self.settings.get("calls_copy_on_end") else ""
            self._notify("Call transcript ready",
                         f"{call.name}: {lines} lines saved{copied}, {ready_s:.0f} s after the call ended."
                         + (" " + hints[0] if hints else ""), always=bool(hints))
        else:
            self._notify("Call ended, nothing transcribed",
                         hints[0] if hints else "No speech was heard on either side.", always=True)

    # -- last call -------------------------------------------------------------------------

    def last_file(self) -> Path | None:
        if self.last_path and self.last_path.exists():
            return self.last_path
        latest = self.calls_dir() / "latest.md"
        return latest if latest.exists() else None

    def copy_last(self) -> bool:
        path = self.last_file()
        if path is None:
            return False
        try:
            self._copy(path.read_text(encoding="utf-8"))
        except OSError:
            return False
        return True
