"""
The dictation pipeline: hotkey actions in, finished text in the focused app.

    press -> open mic (Scribe's Recorder) + warm the API connections
    release -> stop -> audio prep -> speech-to-text -> cleanup or command
            -> dictionary + snippets -> history -> paste -> latency log

Platform-neutral. Everything that touches the OS (key injection,
clipboard, foreground window, overlay, tray) goes through a Platform
object, so the whole flow runs in tests with fakes.
"""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from scribe_plus import audio, engines, textproc
from scribe_plus.hotkeys import Action
from scribe_plus.metrics import MetricsLog, save_clip, timestamp

log = logging.getLogger(__name__)

MIN_AUDIO_MS = 300
SPACE_WINDOW_S = 60.0


@dataclass
class AppContext:
    hwnd: int = 0
    process: str = ""
    title: str = ""
    style: str = "default"


class Platform:
    """Interface the pipeline needs from the OS. See scribe_plus.win.platform."""

    def capture_context(self) -> AppContext: return AppContext()
    def inject_mask(self) -> None: pass
    def wait_modifiers_released(self, timeout: float) -> bool: return True
    def paste(self, text: str, restore: Optional[str] = None) -> None: pass
    def capture_selection(self) -> tuple[str, Optional[str]]: return "", None
    def restore_clipboard(self, text: Optional[str]) -> None: pass
    def copy(self, text: str) -> None: pass
    def notify(self, title: str, message: str) -> None: pass
    def ui(self, state: str, **kw: Any) -> None: pass


@dataclass
class CoreHooks:
    """The pieces of upstream scribe_core the pipeline uses (injectable)."""
    recorder_factory: Callable[[], Any]
    append_history: Callable[[str, int], None]
    last_history_text: Callable[[], str]
    is_garbage: Callable[[str], bool]
    save_unsent: Callable[[bytes], Any]


@dataclass
class Session:
    id: int
    t_press: float
    context: AppContext = field(default_factory=AppContext)
    recorder: Any = None
    opened: bool = False
    end: Optional[str] = None          # dictate | command | handsfree | cancel
    cancel_reason: str = ""
    command: bool = False
    handsfree: bool = False
    t_release: float = 0.0
    mic_open_ms: float = 0.0
    stop_ms: float = 0.0
    meter_stop: threading.Event = field(default_factory=threading.Event)
    # Set once this session's recorder is fully stopped (or never started).
    stopped: threading.Event = field(default_factory=threading.Event)
    previous: Optional["Session"] = None


@dataclass
class _LastPaste:
    hwnd: int
    text: str
    t: float


class DictationEngine:
    def __init__(self, settings, platform: Platform, hooks: CoreHooks, *,
                 pool: Optional[engines.HttpPool] = None,
                 metrics: Optional[MetricsLog] = None,
                 dictionary_path: Optional[Path] = None,
                 snippets_path: Optional[Path] = None,
                 clips_dir: Optional[Path] = None,
                 start_threads: bool = True) -> None:
        from scribe_plus import settings as S
        self.settings = settings
        self.platform = platform
        self.hooks = hooks
        self.pool = pool or engines.HttpPool()
        self.metrics = metrics or MetricsLog(S.METRICS_FILE)
        self._dictionary = textproc.dictionary_loader(dictionary_path or S.DICTIONARY_FILE)
        self._snippets = textproc.snippets_loader(snippets_path or S.SNIPPETS_FILE)
        self.clips_dir = Path(clips_dir or S.CLIPS_DIR)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._session: Optional[Session] = None
        self._latest: Optional[Session] = None
        self._last_paste: Optional[_LastPaste] = None
        self._actions: "queue.SimpleQueue[Action]" = queue.SimpleQueue()
        self._jobs: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()
        self.paused = False
        self.busy = 0
        if start_threads:
            threading.Thread(target=self._dispatch_loop, name="plus-actions", daemon=True).start()
            threading.Thread(target=self._process_loop, name="plus-process", daemon=True).start()

    # -- action intake (called from the keyboard hook; must not block) -----

    def post(self, action: Action) -> None:
        self._actions.put(action)

    def _dispatch_loop(self) -> None:
        while True:
            action = self._actions.get()
            try:
                self.handle(action)
            except Exception:
                try:
                    log.exception("action %s failed", action)
                except Exception:
                    pass

    def handle(self, a: Action) -> None:
        if a.kind == "mask":
            self.platform.inject_mask()
        elif a.kind == "start":
            if not self.paused:
                self._start()
        elif a.kind == "command":
            s = self._session
            if s is not None:
                s.command = True
                self.platform.ui("recording", mode="command")
        elif a.kind == "lock":
            s = self._session
            if s is not None:
                s.handsfree = True
                self.platform.ui("recording", mode="handsfree")
        elif a.kind == "stop":
            self._end(a.mode)
        elif a.kind == "cancel":
            self._end("cancel", a.reason)
        elif a.kind == "paste_last":
            threading.Thread(target=self._paste_last, daemon=True).start()

    # -- recording ---------------------------------------------------------

    def _start(self) -> None:
        with self._lock:
            if self._session is not None:
                return
            s = Session(next(self._ids), time.monotonic(), previous=self._latest)
            self._session = s
            self._latest = s
        self.settings.maybe_reload()
        try:
            s.context = self.platform.capture_context()
        except Exception as exc:
            log.warning("context capture failed: %s", exc)
        self.platform.ui("recording", mode="dictate")
        engines.prewarm_for(self.pool, self.settings.get("stt_engine"),
                            self.settings.get("cleanup_engine"))
        threading.Thread(target=self._open, args=(s,), name=f"mic-open-{s.id}",
                         daemon=True).start()

    def _open(self, s: Session) -> None:
        # Scribe's recorder is one shared capture process: a start while an
        # earlier session is still stopping would let that late stop cut off
        # this recording. So wait until the previous session has let go.
        prev = s.previous
        s.previous = None
        if prev is not None and not prev.stopped.wait(10.0):
            log.warning("previous recording still stopping after 10s; starting anyway")
        t0 = time.monotonic()
        try:
            s.recorder = self.hooks.recorder_factory()
            s.recorder.start()
        except Exception as exc:
            log.warning("mic open failed: %s", exc)
            s.stopped.set()
            with self._lock:
                if self._session is s:
                    self._session = None
            self.platform.ui("error", message="Microphone error")
            self.platform.notify("Microphone error", str(exc)[:180])
            return
        s.mic_open_ms = (time.monotonic() - t0) * 1000.0
        with self._lock:
            s.opened = True
            ended = s.end is not None
        if ended:
            self._finish(s)
        else:
            threading.Thread(target=self._meter, args=(s,), daemon=True).start()

    def _meter(self, s: Session) -> None:
        """Feed the pill's level bars from the recorder's growing PCM file."""
        path = getattr(s.recorder, "_tmp_path", None)
        if not path:
            return
        while not s.meter_stop.wait(0.05):
            try:
                with open(path, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 3200))  # last ~100 ms
                    chunk = f.read(3200)
                self.platform.ui("level", value=audio.level_from_pcm_bytes(chunk))
            except Exception:
                pass

    def _end(self, mode: str, reason: str = "") -> None:
        with self._lock:
            s = self._session
            if s is None:
                return
            self._session = None
            s.end = mode
            s.cancel_reason = reason
            s.t_release = time.monotonic()
            opened = s.opened
        s.meter_stop.set()
        if mode == "cancel":
            self.platform.ui("cancelled" if reason == "escape" else "idle")
        else:
            self.platform.ui("processing")
        if opened:
            threading.Thread(target=self._finish, args=(s,), daemon=True).start()
        # Otherwise _open() finishes the session as soon as the mic is open.

    def _finish(self, s: Session) -> None:
        t0 = time.monotonic()
        try:
            wav, dur_ms = s.recorder.stop()
        except Exception as exc:
            log.warning("recorder stop failed: %s", exc)
            wav, dur_ms = b"", 0
        finally:
            s.stopped.set()
        s.stop_ms = (time.monotonic() - t0) * 1000.0
        if s.end == "cancel":
            return
        self._jobs.put((s, wav, dur_ms))

    # -- processing (one at a time, so pastes land in order) ---------------

    def _process_loop(self) -> None:
        while True:
            s, wav, dur_ms = self._jobs.get()
            self.busy += 1
            try:
                self.process(s, wav, dur_ms)
            except Exception:
                try:
                    log.exception("processing failed")
                    self.platform.ui("error", message="Something went wrong")
                except Exception:
                    pass
            finally:
                self.busy -= 1

    def process(self, s: Session, wav: bytes, dur_ms: int) -> Optional[str]:
        st = self.settings
        mode = s.end or "dictate"
        row: dict = {
            "timestamp": timestamp(), "mode": mode, "app": s.context.process,
            "window_title": s.context.title if st.get("log_window_titles") else "",
            "style": s.context.style if st.get("app_aware_style") else "default",
            "mic_open_ms": s.mic_open_ms, "stop_ms": s.stop_ms,
        }

        def ms_since_release() -> float:
            return (time.monotonic() - s.t_release) * 1000.0

        if not wav or dur_ms < MIN_AUDIO_MS:
            self.platform.ui("idle")
            return None

        t = time.monotonic()
        try:
            prep = audio.prepare(wav, boost=bool(st.get("quiet_voice_boost")),
                                 trim=bool(st.get("trim_silence")))
        except Exception as exc:
            log.warning("audio prep failed, sending raw audio: %s", exc)
            prep = audio.Prepared(wav, 16000, dur_ms / 1000.0, dur_ms / 1000.0,
                                  0.0, 0.0, 0.0, 0.0, False)
        row.update(prep_ms=(time.monotonic() - t) * 1000.0, audio_s=prep.duration_s,
                   speech_s=prep.kept_s, gain_db=prep.gain_db)
        if prep.silent:
            row["error"] = "silent"
            self._finish_row(row, ms_since_release())
            self.platform.ui("error", message="No speech heard")
            return None

        clip_name = ""
        if st.get("save_audio"):
            try:
                clip_name = save_clip(wav, self.clips_dir, int(st.get("max_saved_clips") or 0))
            except Exception as exc:
                log.warning("clip save failed: %s", exc)
        row["clip"] = clip_name

        # Command Mode reads the selection while the speech is transcribed.
        selection: dict = {}
        sel_thread = None
        if mode == "command":
            sel_thread = threading.Thread(target=self._grab_selection, args=(selection,), daemon=True)
            sel_thread.start()

        dictionary = self._dictionary.get()
        snippets = self._snippets.get()

        stt_engine = st.get("stt_engine")
        stt_model = st.stt_model(stt_engine)
        prompt = textproc.whisper_prompt(dictionary)
        try:
            res = engines.transcribe(stt_engine, prep.wav, model=stt_model,
                                     language=st.get("language"), prompt=prompt,
                                     pool=self.pool, audio_s=prep.kept_s)
        except Exception as exc:
            log.warning("speech-to-text failed: %s", exc)
            row.update(stt_engine=stt_engine, stt_model=stt_model, error=f"stt: {exc}")
            self._finish_row(row, ms_since_release())
            saved = ""
            try:
                saved = str(self.hooks.save_unsent(wav))
            except Exception:
                pass
            self.platform.ui("error", message="Transcription failed")
            self.platform.notify("Transcription failed",
                                 f"{exc}"[:150] + (" Recording saved." if saved else ""))
            if sel_thread:
                sel_thread.join(1.0)
                self.platform.restore_clipboard(selection.get("saved"))
            return None

        raw = res.text.strip()
        row.update(stt_engine=res.engine, stt_model=res.model, stt_ms=res.ms,
                   raw_text=raw, words_raw=len(textproc.word_list(raw)))
        cost = res.cost_usd
        if not raw or self.hooks.is_garbage(raw) or (prompt and raw.strip(" .") == prompt.strip(" .")):
            row["error"] = "no speech"
            row["est_cost_usd"] = cost
            self._finish_row(row, ms_since_release())
            self.platform.ui("error", message="No speech detected")
            if sel_thread:
                sel_thread.join(1.0)
                self.platform.restore_clipboard(selection.get("saved"))
            return None

        style_key = s.context.style if st.get("app_aware_style") else "default"
        restore: Optional[str] = None
        if mode == "command":
            if sel_thread:
                sel_thread.join(2.0)
            restore = selection.get("saved")
            final, llm_cost = self._command(raw, selection.get("text", ""), style_key, s, row)
            if final is None:
                self._finish_row(row | {"est_cost_usd": cost + llm_cost}, ms_since_release())
                self.platform.restore_clipboard(restore)
                self.platform.ui("error", message="Command failed")
                return None
        else:
            final, llm_cost = self._cleanup(raw, style_key, s, row, dictionary, snippets)
        cost += llm_cost
        row["est_cost_usd"] = cost

        if not final.strip():
            self._finish_row(row, ms_since_release())
            self.platform.ui("idle")
            return None

        if (mode != "command" and st.get("auto_space") and style_key != "code"
                and self._last_paste and self._last_paste.hwnd == s.context.hwnd
                and time.monotonic() - self._last_paste.t < SPACE_WINDOW_S
                and textproc.needs_leading_space(self._last_paste.text, final)):
            final = " " + final

        # History first, so a failed paste can never lose the text.
        try:
            self.hooks.append_history(final.strip(), int(dur_ms))
        except Exception as exc:
            log.warning("history append failed: %s", exc)

        t = time.monotonic()
        if self.platform.wait_modifiers_released(2.0):
            self.platform.paste(final, restore=restore)
            self._last_paste = _LastPaste(s.context.hwnd, final, time.monotonic())
            self.platform.ui("done")
        else:
            # Keys still held: pasting now would fire Win+Ctrl+V and friends.
            self.platform.copy(final)
            self.platform.ui("info", message="Copied: press Ctrl+V")
        row["paste_ms"] = (time.monotonic() - t) * 1000.0
        row["final_text"] = final
        row["words_final"] = len(textproc.word_list(final))
        self._finish_row(row, ms_since_release())
        return final

    def _finish_row(self, row: dict, total_ms: float) -> None:
        row["total_ms"] = total_ms
        self.metrics.write_async(row)

    def _grab_selection(self, out: dict) -> None:
        try:
            self.platform.wait_modifiers_released(2.0)
            text, saved = self.platform.capture_selection()
            out["text"], out["saved"] = text, saved
        except Exception as exc:
            log.warning("selection capture failed: %s", exc)

    def _cleanup(self, raw: str, style_key: str, s: Session, row: dict,
                 dictionary: textproc.Dictionary, snippets: list) -> tuple[str, float]:
        st = self.settings
        engine = st.get("cleanup_engine")
        cost = 0.0
        final: Optional[str] = None
        if engine and engine != "off":
            model = st.cleanup_model(engine)
            system, messages = textproc.build_cleanup_messages(
                raw, style_key=style_key, style_text=st.style_text(style_key),
                app=s.context.process, dictionary=dictionary, snippets=snippets)
            row.update(cleanup_engine=engine, cleanup_model=model)
            try:
                res = engines.chat(engine, system, messages, model=model, pool=self.pool,
                                   timeout=float(st.get("cleanup_timeout_s") or 6.0),
                                   max_tokens=engines.max_tokens_for(raw),
                                   ollama_base=st.get("ollama_base_url"))
                cost = res.cost_usd
                row.update(cleanup_ms=res.ms, tokens_in=res.tokens_in, tokens_out=res.tokens_out)
                out = textproc.postprocess_output(res.text, raw)
                reason = textproc.guard(raw, out)
                if reason:
                    row["guard"] = reason
                    log.info("guard rejected cleanup (%s): %r -> %r", reason, raw, out)
                else:
                    final = out
            except Exception as exc:
                log.warning("cleanup failed, using basic cleanup: %s", exc)
                row["error"] = f"cleanup: {exc}"
        else:
            row.update(cleanup_engine="off")
        if final is None:
            final = textproc.basic_cleanup(raw)
        final = textproc.apply_replacements(final, dictionary)
        final = textproc.expand_snippets(final, snippets)
        return final, cost

    def _command(self, instruction: str, selection: str, style_key: str, s: Session,
                 row: dict) -> tuple[Optional[str], float]:
        st = self.settings
        engine = st.get("cleanup_engine")
        if not engine or engine == "off":
            engine = "groq"
        model = st.command_model(engine)
        system, messages = textproc.build_command_messages(
            selection, instruction, style_key=style_key,
            style_text=st.style_text(style_key), app=s.context.process)
        row.update(cleanup_engine=engine, cleanup_model=model)
        try:
            res = engines.chat(engine, system, messages, model=model, pool=self.pool,
                               timeout=float(st.get("command_timeout_s") or 15.0),
                               max_tokens=engines.max_tokens_for(selection + instruction) + 512,
                               ollama_base=st.get("ollama_base_url"))
        except Exception as exc:
            log.warning("command failed: %s", exc)
            row["error"] = f"command: {exc}"
            self.platform.notify("Command Mode failed", str(exc)[:150])
            return None, 0.0
        row.update(cleanup_ms=res.ms, tokens_in=res.tokens_in, tokens_out=res.tokens_out)
        out = textproc.postprocess_output(res.text, selection)
        return (out or None), res.cost_usd

    # -- misc --------------------------------------------------------------

    def _paste_last(self) -> None:
        text = ""
        try:
            text = self.hooks.last_history_text()
        except Exception as exc:
            log.warning("history read failed: %s", exc)
        if not text:
            self.platform.notify("Nothing to paste", "History is empty.")
            return
        if self.platform.wait_modifiers_released(2.0):
            self.platform.paste(text)
        else:
            self.platform.copy(text)

    @property
    def recording(self) -> bool:
        return self._session is not None
