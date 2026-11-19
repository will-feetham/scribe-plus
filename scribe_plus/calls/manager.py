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
from scribe_plus.calls.transcript import Transcript, TranscriptFiles, clock, duration_words

log = logging.getLogger(__name__)

MANUAL_NAME = "Manual recording"
# Groq allows 20 speech requests a minute: up to 3 at once, then one every
# 3.5 s (3 + 17 in any one minute).
GROQ_MIN_INTERVAL_S = 3.5
GROQ_BURST = 3
QUIET_UNDER_S = 5.0            # an empty "call" shorter than this ends without a notice
CHECK_EVERY_S = 1.0            # how often the recording helpers are checked during a call
MAX_RESTARTS = 2               # per side, per call
SPEAKER_LABELS = {"Them": "the other side", "Me": "the microphone"}
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
    sources: dict = field(default_factory=dict)      # "Them" / "Me" -> the running helper
    make: dict = field(default_factory=dict)         # "Them" / "Me" -> starts a fresh helper
    restarts: dict = field(default_factory=dict)     # "Them" / "Me" -> times restarted
    gave_up: set = field(default_factory=set)
    checked: float = 0.0
    checking: bool = False
    ended: bool = False
    ready_s: float = 0.0


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
        self.detector = CallDetector(self._apps(), start_delay=self._num("calls_start_delay_s", 0.6),
                                     end_delay=self._num("calls_end_delay_s", 1.5))
        self.detector.sound_hold = self._num("calls_end_delay_with_sound_s", 4.0)
        self._call: _Call | None = None
        self._busy = False                 # a recording is being started or stopped
        self._skip: Detected | None = None  # a call in progress that is not to be recorded
        self._want_manual = False
        self._want_stop = False
        self._last_ended: _Call | None = None
        self._wrapping: list[threading.Thread] = []   # transcripts still being finished
        self._awake = 0
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
        apps = [a for a in apps if isinstance(a, dict)] if isinstance(apps, list) else []
        return apps or DEFAULT_APPS

    def apps(self) -> list[dict]:
        """The apps being watched (a hand-edited settings file is tidied up)."""
        return self._apps()

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
        """Quitting: a call in progress is closed properly, not left "in progress"."""
        self._quit.set()
        try:
            self.platform.stop_watch()
        except Exception:
            pass
        with self._lock:
            call, self._call = self._call, None
            if call is not None:
                call.ended = True
        if call is not None:
            self._stop_sources(call, 0.5)
            try:
                call.recorder.finish(timeout=2.0, final=True)
            except Exception:
                log.exception("could not close the call transcript on quit")
            log.info("call ended: %s (Scribe Plus was quit); %s", call.name, call.recorder.summary())
        deadline = time.monotonic() + 3.0
        for thread in list(self._wrapping):
            thread.join(max(0.0, deadline - time.monotonic()))

    # -- detection ------------------------------------------------------------------

    def _on_procs(self, procs: list[dict]) -> None:
        now = time.monotonic()
        with self._lock:
            self.detector.update(procs, now)
        # One log line whenever a watched app starts or stops using the mic or
        # playing sound: this is what shows how an app behaves around a call.
        for app in self._apps():
            if not app.get("enabled", True):
                continue
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
        """Compares what should be happening with what is, and closes the gap.

        It looks at the state on every pass rather than reacting to one-off
        "started" and "stopped" moments, so nothing is lost when those happen
        while a recording is still being set up or closed: the next pass sees
        the difference and acts on it. That matters for a dialer, where the
        next call can begin a second after the last one ends.
        """
        begin: tuple[Optional[Detected]] | None = None
        end: tuple[_Call, str] | None = None
        with self._lock:
            self.detector.apps = self._apps()
            self.detector.poll(now)
            active = self.detector.active
            if self._skip is not None and self._skip is not active:
                self._skip = None                  # that call is over
            call = self._call
            if self._busy or self._quit.is_set():
                return
            if call is None:
                self._want_stop = False
                if self._want_manual:
                    self._want_manual = False
                    begin = (None,)
                elif active is not None and active is not self._skip \
                        and self.settings.get("calls_enabled"):
                    begin = (active,)
            else:
                self._want_manual = False
                reason, again = "", True
                if self._want_stop:
                    # Not picked up again until the app lets go of the microphone.
                    reason, again = "stopped from the menu", False
                elif call.detected is not None and call.detected is not active:
                    reason = "the app let go of the microphone"
                elif (now - call.t0) > 60.0 * self._num("calls_max_minutes", 180):
                    reason, again = "the time limit was reached", False
                if reason:
                    self._want_stop = False
                    if not again:
                        self._skip = active
                    end = (call, reason)
            check = False
            if begin is not None or end is not None:
                self._busy = True
            elif call is not None and not call.checking and now - call.checked >= CHECK_EVERY_S:
                call.checked, call.checking, check = now, True, True
        if begin is not None:
            threading.Thread(target=self._begin, args=begin, name="call-begin", daemon=True).start()
        elif end is not None:
            threading.Thread(target=self._end, args=end, name="call-end", daemon=True).start()
        elif check:
            try:
                self._check_sources(call, now)
            finally:
                call.checking = False

    def _kick(self) -> None:
        """Act on a menu click now rather than at the next pass."""
        try:
            self._tick(time.monotonic())
        except Exception:
            log.exception("call detection tick failed")

    # -- menu actions -----------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._call is not None

    @property
    def idle(self) -> bool:
        """Nothing being recorded, started, stopped or still written up."""
        with self._lock:
            return self._call is None and not self._busy and not self._want_manual \
                and not any(t.is_alive() for t in self._wrapping)

    def status(self) -> dict:
        call = self._call
        if call is None:
            return {"recording": False}
        return {"recording": True, "name": call.name, "started": call.started,
                "lines": call.recorder.lines_added, "manual": call.detected is None}

    def start_manual(self) -> None:
        self._want_manual = True
        self._kick()

    def stop(self) -> None:
        self._want_stop = True
        self._kick()

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

    def _transcriber(self) -> Callable[[Any, str], str]:
        """The engine is looked up for every piece of speech, so a change in
        the menu during a call takes effect at once (cloud back to this Mac
        included)."""
        def run(samples, speaker: str) -> str:
            engine, model, problem = self.resolve_engine()
            if not engine:
                raise engines.EngineError(problem)
            dictionary = self._dictionary() if self._dictionary else None
            prompt = textproc.whisper_prompt(dictionary) if dictionary else ""
            res = engines.transcribe_samples(engine, samples, model=model,
                                             language=self.settings.get("language"),
                                             prompt=prompt, pool=self.pool)
            return textproc.strip_prompt_echo(res.text, prompt)
        return run

    def _pace(self) -> float:
        """Seconds between speech requests: only the cloud needs any."""
        return GROQ_MIN_INTERVAL_S if self.resolve_engine()[0] == "groq" else 0.0

    def _cleaner(self) -> Callable[[str], str]:
        def run(text: str) -> str:
            dictionary = self._dictionary() if self._dictionary else None
            return textproc.apply_replacements(text, dictionary) if dictionary else text
        return run

    def _begin(self, detected: Optional[Detected]) -> None:
        """Runs with _busy held (claimed in _tick)."""
        call: _Call | None = None
        try:
            call = self._set_up(detected)
        except Exception as exc:
            log.exception("could not start recording the call")
            self._notify("Call recording failed to start", str(exc)[:160], always=True)
        finally:
            with self._lock:
                if call is None and detected is not None and self.detector.active is detected:
                    self._skip = detected          # don't try again on every pass
                self._busy = False
            self._changed()
            self._kick()        # the call may already be over, or a stop was asked for

    def _set_up(self, detected: Optional[Detected]) -> _Call | None:
        self.settings.maybe_reload()
        engine, model, problem = self.resolve_engine()
        name = detected.name if detected else MANUAL_NAME
        if not engine:
            log.warning("call in %s not recorded: %s", name, problem)
            if time.monotonic() - self._warned_engine > 600:
                self._warned_engine = time.monotonic()
                self._notify("Call not recorded", f"Scribe Plus noticed a call, but {problem}.",
                             always=True)
            return None
        started = datetime.now()
        transcript = Transcript(name, started, app=detected.name if detected else "",
                                engine=engine)
        files = TranscriptFiles(self.calls_dir(), transcript)
        recorder = CallRecorder(
            transcript, files, self._transcriber(), clean=self._cleaner(),
            drop_echo=bool(self.settings.get("calls_drop_echo")),
            min_interval_s=self._pace, burst=GROQ_BURST,
            audio_dir=self.calls_dir() / "audio" if self.settings.get("calls_keep_audio") else None,
            on_error=self._speech_error)
        call = _Call(name, detected, recorder, files, started, time.monotonic())
        system = detected is None or str(self.settings.get("calls_capture")) == "system"
        call.make["Them"] = lambda: self.platform.tap_source(
            detected, system, lambda s, r, a: recorder.feed("Them", s, r, a), None)
        call.make["Me"] = lambda: self._mic_factory(
            self._mic_device(), lambda s, r, a: recorder.feed("Me", s, r, a), app_dir=self.app_dir)
        try:
            for speaker, label in SPEAKER_LABELS.items():
                try:
                    src = call.make[speaker]()
                    call.sources[speaker] = src
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
        except BaseException:
            # Nothing half-started is left running.
            call.ended = True
            self._stop_sources(call, 0.5)
            recorder.finish(timeout=0.0, final=True)
            files.release()
            raise
        with self._lock:
            self._call = call
            self._awake += 1
            first = self._awake == 1
        if first:
            self._platform_hook("call_started")
        if detected and self.settings.get("calls_window_titles"):
            # In the background: asking an app for its windows can take a moment,
            # and a call that ends in that moment must still be closed at once.
            threading.Thread(target=self._add_titles, args=(call,), name="call-titles",
                             daemon=True).start()
        log.info("call started: %s (speech-to-text %s, %s; capture %s)", name, engine, model,
                 "all sound" if system else "the app's sound")
        self._notify("Recording a call", f"{name}: both sides, transcribed {ENGINE_LABELS.get(engine, engine)}.")
        return call

    def _add_titles(self, call: _Call) -> None:
        if call.detected is None:
            return
        windows = call.recorder.transcript.windows
        for title in self._titles(call.detected):
            if title not in windows:
                windows.append(title)

    def _check_sources(self, call: _Call, now: float) -> None:
        """A recording helper that died or went silent is started again (twice
        at most), and the transcript says so. Without this a call could run to
        the end with one side missing and nobody told."""
        for speaker, label in SPEAKER_LABELS.items():
            src = call.sources.get(speaker)
            probe = getattr(src, "problem", None)
            if probe is None or speaker in call.gave_up:
                continue
            try:
                problem = probe()
            except Exception:
                continue
            if not problem:
                continue
            at = clock(now - call.t0)
            tries = call.restarts.get(speaker, 0)
            transcript = call.recorder.transcript
            if tries >= MAX_RESTARTS:
                call.gave_up.add(speaker)
                log.warning("recording %s stopped at %s and is not coming back: %s", label, at, problem)
                transcript.notes.append(
                    f"Recording {label} stopped at {at} and could not be started again ({problem}).")
                call.recorder._write()
                self._notify("Call recording problem",
                             f"{label.capitalize()} is no longer being recorded ({problem}).", always=True)
                continue
            call.restarts[speaker] = tries + 1
            log.warning("recording %s has a problem at %s (%s); starting it again", label, at, problem)
            try:
                fresh = call.make[speaker]()
                fresh.start()
            except Exception as exc:
                log.warning("could not start recording %s again: %s", label, exc)
                continue
            with self._lock:
                if call.ended:
                    stale = fresh                  # the call ended meanwhile
                else:
                    stale, call.sources[speaker] = src, fresh
            threading.Thread(target=self._stop_one, args=(stale, 0.5), daemon=True).start()
            if tries == 0:
                transcript.notes.append(
                    f"Recording {label} stopped for a moment around {at} and was started again.")

    @staticmethod
    def _stop_one(src: Any, timeout: float) -> None:
        try:
            src.stop(timeout)
        except Exception:
            pass

    def _stop_sources(self, call: _Call, timeout: float) -> None:
        with self._lock:
            srcs = [s for s in call.sources.values() if s is not None]
        stoppers = [threading.Thread(target=self._stop_one, args=(s, timeout), daemon=True)
                    for s in srcs]
        for t in stoppers:
            t.start()
        for t in stoppers:
            t.join(timeout + 1.5)

    def _platform_hook(self, name: str) -> None:
        """Optional platform extras (the Mac keeps itself awake during a call)."""
        hook = getattr(self.platform, name, None)
        if hook:
            try:
                hook()
            except Exception as exc:
                log.debug("%s failed: %s", name, exc)

    def _titles(self, detected: Detected) -> list[str]:
        try:
            return [t for t in self.platform.window_titles(detected.bundles) if t][:4]
        except Exception as exc:
            log.debug("window titles unavailable: %s", exc)
            return []

    def _speech_error(self, exc: Exception) -> None:
        self._notify("Call transcription problem", str(exc)[:160], always=True)

    def _end(self, call: _Call, reason: str) -> None:
        """Runs with _busy held (claimed in _tick). Stops the recording, which
        is quick, and leaves the writing-up to its own thread so that the next
        call can be recorded straight away."""
        t_end = time.monotonic()
        try:
            with self._lock:
                call.ended = True
            self._stop_sources(call, 1.5)
        except Exception:
            log.exception("could not stop the call recording cleanly")
        finally:
            wrap = threading.Thread(target=self._wrap_up, args=(call, reason, t_end),
                                    name="call-wrap-up", daemon=True)
            with self._lock:
                if self._call is call:
                    self._call = None
                self._last_ended = call
                if call.files.path.exists():
                    self.last_path = call.files.path
                self._wrapping = [t for t in self._wrapping if t.is_alive()] + [wrap]
                self._busy = False
            wrap.start()
            self._changed()
            self._kick()        # the next call may already be on

    def _wrap_up(self, call: _Call, reason: str, t_end: float) -> None:
        try:
            self._finish_call(call, reason, t_end)
        except Exception:
            log.exception("could not finish the call cleanly")
        finally:
            with self._lock:
                self._awake -= 1
                last = self._awake == 0
            if last:
                self._platform_hook("call_ended")
            self._changed()

    def _finish_call(self, call: _Call, reason: str, t_end: float) -> None:
        rec, tr = call.recorder, call.recorder.transcript
        if call.detected and self.settings.get("calls_window_titles"):
            self._add_titles(call)
        rec.on_done = lambda: self._hand_over(call, [], late=True)
        caught_up = rec.finish(timeout=12.0)
        me, them = rec.channels["Me"], rec.channels["Them"]
        hints: list[str] = []          # (a side that was given up on is already explained)
        if "Them" in call.gave_up:
            pass
        elif them.seconds < 1.0:
            detail = getattr(call.sources.get("Them"), "last_error", "") or "it sent no audio"
            hints.append(f"The other side was not recorded ({detail}).")
        elif them.peak <= 0.0:
            hints.append("The other side was silent for the whole call. If they did speak, turn on "
                         "System Audio Recording for Scribe Plus in System Settings > Privacy & Security.")
        if "Me" in call.gave_up:
            pass
        elif me.seconds < 1.0:
            hints.append("The microphone was not recorded.")
        elif me.peak <= 0.0:
            hints.append("The microphone was silent for the whole call.")
        if hints:
            tr.notes.extend(hints)
            rec._write()            # (writes nothing for a call with no lines at all)
        call.ready_s = time.monotonic() - t_end
        log.info("call ended: %s (%s); %s; ready %.1f s after it ended; %s%s", call.name, reason,
                 duration_words((tr.ended - tr.started).total_seconds()) if tr.ended else "?",
                 call.ready_s, rec.summary(), "" if caught_up else "; speech model still catching up")
        if rec.lines_added or caught_up:
            self._hand_over(call, hints, late=False, caught_up=caught_up)

    def _hand_over(self, call: _Call, hints: list[str], *, late: bool, caught_up: bool = True) -> None:
        """The finished (or, when the speech model is behind, so-far) transcript:
        remembered as the last call, copied, announced."""
        rec, tr = call.recorder, call.recorder.transcript
        lines = rec.lines_added
        if not lines:
            call.files.release()
            if tr.ended and (tr.ended - tr.started).total_seconds() >= QUIET_UNDER_S:
                # (An app that opens the microphone for a moment is not worth a notice.)
                self._notify("Call ended, nothing transcribed",
                             hints[0] if hints else "No speech was heard on either side.", always=True)
            return
        text = tr.render()
        with self._lock:
            newest = self._last_ended is call        # no later call has ended since
            if newest:
                self.last_text = text
                if call.files.path.exists():
                    self.last_path = call.files.path
        copied = False
        if newest and self.settings.get("calls_copy_on_end"):
            try:
                self._copy(text)
                copied = True
            except Exception as exc:
                log.warning("could not copy the call transcript: %s", exc)
        count = "1 line" if lines == 1 else f"{lines} lines"
        also = " and copied" if copied else ""
        if rec.write_error:
            self._notify("Call transcript not saved",
                         f"{call.name}: {count} could not be written to the Calls folder "
                         f"({rec.write_error[:100]})." + (" It was copied to the clipboard." if copied else ""),
                         always=True)
        elif late:
            self._notify("Call transcript complete", f"{call.name}: {count} saved{also}.")
        elif not caught_up:
            self._notify("Call transcript: first part ready",
                         f"{call.name}: {count} so far, saved{also}. The rest is still being transcribed.")
        else:
            when = "as the call ended" if call.ready_s < 1.5 else f"{call.ready_s:.0f} s after the call ended"
            self._notify("Call transcript ready",
                         f"{call.name}: {count} saved{also}, {when}."
                         + (" " + hints[0] if hints else ""), always=bool(hints))

    # -- last call -------------------------------------------------------------------------

    def last_file(self) -> Path | None:
        """The call in progress once it has a transcript, else the last one finished."""
        call = self._call
        if call is not None and call.files.path.exists():
            return call.files.path
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
