"""
One call: two audio channels in, a live transcript out.

    feed("Me", samples, rate)   \\
                                  Channel (resample, cut into utterances)
    feed("Them", samples, rate) /        |
                                         v
                      a queue of utterances, oldest first
                                         |
                      one worker: speech-to-text, then a new line in the
                      transcript and both files rewritten

Everything is transcribed while the call is still going, so finish() only
has the last few seconds left to do.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from scribe_plus.calls.segmenter import RATE, Segmenter, Utterance
from scribe_plus.calls.stream import Resampler
from scribe_plus.calls.transcript import Line, Transcript, TranscriptFiles

log = logging.getLogger(__name__)

GAP_S = 1.0             # a hole this long in a stream counts as silence
AHEAD_S = 0.3           # audio arriving this far ahead of the clock pulls the clock back
RETRY_AFTER_S = 1.0     # a failed speech-to-text request is tried once more
BATCH_MAX_S = 25.0      # most speech sent to the speech model in one go
BATCH_PAUSE_S = 0.3     # silence put between utterances joined into one request
TARGET_PEAK = 0.7       # quiet speech is raised to about -3 dBFS
MAX_GAIN = 30.0         # ... by at most this many dB

# Things speech models write when they are handed noise instead of speech.
_NOISE_TEXT = {
    "you", "thanksforwatching", "thanksforwatchingthevideo", "thankyouforwatching",
    "pleasesubscribe", "subscribe", "music", "applause", "silence", "blankaudio",
    "subtitlesbytheamaraorgcommunity",
}
_BRACKETED = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")


def is_noise_text(text: str) -> bool:
    """Numbers count as content: "42." and "555-0199" are answers, not noise."""
    norm = "".join(ch for ch in text.lower() if ch.isalnum())
    if not norm or bool(_BRACKETED.match(text)):
        return True
    return norm in _NOISE_TEXT or (len(norm) < 2 and norm.isalpha())


def normalize(samples: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak <= 0 or peak >= TARGET_PEAK:
        return samples
    gain = min(TARGET_PEAK / peak, 10.0 ** (MAX_GAIN / 20.0))
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32)


@dataclass
class _Job:
    speaker: str
    t: float        # seconds from the start of the call
    end: float
    utterance: Utterance


class Channel:
    """One side of the call: packets at any sample rate in, timed utterances out."""

    def __init__(self, speaker: str, t0: float, *, segmenter: Segmenter | None = None,
                 audio_path: Path | None = None) -> None:
        self.speaker = speaker
        self.t0 = t0                       # time.monotonic() at the start of the call
        self.segmenter = segmenter or Segmenter()
        self._resampler: Resampler | None = None
        self._offset: float | None = None  # call time of this stream's sample 0
        self._fed = 0                      # 16 kHz samples handed to the segmenter
        self.seconds = 0.0
        self.peak = 0.0
        self.utterances = 0
        self.last_arrival: float | None = None
        self._lock = threading.Lock()
        self._wav: wave.Wave_write | None = None
        if audio_path is not None:
            try:
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                self._wav = wave.open(str(audio_path), "wb")
                self._wav.setnchannels(1)
                self._wav.setsampwidth(2)
                self._wav.setframerate(RATE)
            except Exception as exc:
                log.warning("could not keep %s audio: %s", speaker, exc)
                self._wav = None

    def push(self, samples: np.ndarray, rate: int, arrived: float | None = None) -> list[_Job]:
        arrived = time.monotonic() if arrived is None else arrived
        # One bad sample (NaN) would otherwise poison the filter and the noise
        # floor, and that side of the call would go deaf.
        samples = np.nan_to_num(np.asarray(samples, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        with self._lock:
            self.last_arrival = arrived
            if self._resampler is None or self._resampler.rate_in != int(rate):
                self._resampler = Resampler(int(rate))
            x = self._resampler.process(samples)
            jobs: list[_Job] = []
            if x.size == 0:
                return jobs
            dur = x.size / RATE
            now_call = arrived - self.t0
            if self._offset is None:
                self._offset = now_call - dur
            else:
                late = now_call - (self._offset + (self._fed + x.size) / RATE)
                if late > GAP_S:
                    # The stream stopped for a while (device change, a stall).
                    jobs += self._jobs(self.segmenter.flush())
                    self._offset += late
                elif late < -AHEAD_S:
                    # Audio is arriving faster than time passes: it was only held
                    # up, not missing, so an earlier "gap" moved the clock too far.
                    self._offset += late
            if self._wav is not None:
                try:
                    self._wav.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
                except Exception:
                    pass
            self.seconds += dur
            self.peak = max(self.peak, float(np.max(np.abs(x))))
            jobs += self._jobs(self.segmenter.feed(x))
            self._fed += x.size
            return jobs

    def flush(self) -> list[_Job]:
        with self._lock:
            jobs = self._jobs(self.segmenter.flush())
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:
                    pass
                self._wav = None
            return jobs

    def _jobs(self, utterances: list[Utterance]) -> list[_Job]:
        offset = self._offset or 0.0
        self.utterances += len(utterances)
        return [_Job(self.speaker, offset + u.start / RATE, offset + u.end / RATE, u)
                for u in utterances]


class CallRecorder:
    def __init__(self, transcript: Transcript, files: TranscriptFiles | None,
                 transcribe: Callable[[np.ndarray, str], str], *,
                 clean: Callable[[str], str] | None = None,
                 drop_echo: bool = True,
                 min_interval_s: float | Callable[[], float] = 0.0, burst: int = 1,
                 audio_dir: Path | None = None,
                 on_line: Callable[[Line], None] | None = None,
                 on_error: Callable[[Exception], None] | None = None,
                 retry_after_s: float = RETRY_AFTER_S) -> None:
        self.transcript = transcript
        self.files = files
        self._transcribe = transcribe
        self._clean = clean or (lambda text: text)
        self.drop_echo = drop_echo
        # Cloud speech-to-text allows so many requests a minute. Up to `burst`
        # go out at once (a quick exchange is transcribed as it happens), then
        # one per interval.
        self.min_interval_s = min_interval_s
        self.burst = max(1, int(burst))
        self._tokens = float(self.burst)
        self._tokens_at = time.monotonic()
        self._on_line = on_line
        self._on_error = on_error
        self.on_done: Callable[[], None] | None = None   # called if the transcript finishes late
        self.retry_after_s = retry_after_s
        self.write_error = ""
        self.t0 = time.monotonic()
        stem = Path(transcript.file_name()).stem
        self.channels: dict[str, Channel] = {}
        for speaker in ("Me", "Them"):
            path = (audio_dir / f"{stem} {speaker}.wav") if audio_dir else None
            self.channels[speaker] = Channel(speaker, self.t0, audio_path=path)
        self._queue: deque[_Job] = deque()
        self._cv = threading.Condition()
        self._closing = False
        self._busy = False
        self.failures = 0
        self.lines_added = 0
        self.stt_seconds = 0.0          # speech sent to the model
        self.stt_time = 0.0             # time the model took
        self._worker = threading.Thread(target=self._run, name="call-transcribe", daemon=True)
        self._worker.start()

    # -- audio in --------------------------------------------------------------

    def feed(self, speaker: str, samples: np.ndarray, rate: int,
             arrived: float | None = None) -> None:
        ch = self.channels.get(speaker)
        if ch is None or self._closing:
            return
        self._enqueue(ch.push(samples, rate, arrived))

    def _enqueue(self, jobs: list[_Job]) -> None:
        if not jobs:
            return
        with self._cv:
            self._queue.extend(jobs)
            self._cv.notify()

    def pending(self) -> int:
        with self._cv:
            return len(self._queue) + (1 if self._busy else 0)

    # -- the worker --------------------------------------------------------------

    def _take(self) -> list[_Job] | None:
        """The oldest utterance, plus any that follow it from the same speaker
        (when the model is behind, catching up in one request is quicker)."""
        with self._cv:
            while not self._queue:
                if self._closing:
                    return None
                self._cv.wait(0.5)
            batch = [self._queue.popleft()]
            total = batch[0].utterance.duration_s
            while self._queue and self._queue[0].speaker == batch[0].speaker \
                    and total + self._queue[0].utterance.duration_s <= BATCH_MAX_S:
                job = self._queue.popleft()
                total += job.utterance.duration_s
                batch.append(job)
            self._busy = True
            return batch

    def _run(self) -> None:
        while True:
            batch = self._take()
            if batch is None:
                return
            try:
                self._process(batch)
            except Exception:
                log.exception("call transcription worker failed on a batch")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _pace(self) -> None:
        """Wait, if needed, so requests stay inside the allowed rate."""
        interval = self.min_interval_s() if callable(self.min_interval_s) else self.min_interval_s
        now = time.monotonic()
        if interval <= 0:
            self._tokens, self._tokens_at = float(self.burst), now
            return
        self._tokens = min(float(self.burst), self._tokens + (now - self._tokens_at) / interval)
        self._tokens_at = now
        if self._tokens < 1.0:
            time.sleep((1.0 - self._tokens) * interval)
            self._tokens, self._tokens_at = 1.0, time.monotonic()
        self._tokens -= 1.0

    def _process(self, batch: list[_Job]) -> None:
        self._pace()
        pause = np.zeros(int(BATCH_PAUSE_S * RATE), dtype=np.float32)
        parts: list[np.ndarray] = []
        for i, job in enumerate(batch):
            if i:
                parts.append(pause)
            parts.append(job.utterance.samples)
        audio = normalize(np.concatenate(parts))
        speech_s = sum(j.utterance.speech_s for j in batch)
        t = time.perf_counter()
        try:
            try:
                text = self._transcribe(audio, batch[0].speaker) or ""
            except Exception as first:
                # Often passing (a rate limit, a dropped connection): once more.
                log.info("call speech-to-text failed, trying once more: %s", first)
                time.sleep(self.retry_after_s)
                self._pace()
                text = self._transcribe(audio, batch[0].speaker) or ""
        except Exception as exc:
            self.failures += 1
            log.warning("call speech-to-text failed (%s, %.1f s): %s",
                        batch[0].speaker, len(audio) / RATE, exc)
            if self.failures == 1:
                self.transcript.notes.append(
                    f"Some speech could not be transcribed ({str(exc)[:120]}).")
                self._write()
                if self._on_error:
                    try:
                        self._on_error(exc)
                    except Exception:
                        pass
            return
        self.stt_time += time.perf_counter() - t
        self.stt_seconds += len(audio) / RATE
        text = self._clean(text.strip()).strip()
        if not text or is_noise_text(text) or (speech_s < 0.6 and len(text.split()) > 8):
            return
        line = Line(batch[0].t, batch[0].speaker, text, batch[-1].end)
        if self.drop_echo:
            if line.speaker == "Me" and self.transcript.is_echo(line):
                log.info("dropped a line that repeated the other side (echo), %d words", len(text.split()))
                return
            if line.speaker == "Them":      # the microphone's copy got here first
                for echo in self.transcript.echoes_of(line):
                    if self.transcript.remove(echo):
                        self.lines_added -= 1
                        log.info("removed a line that repeated the other side (echo)")
        self.transcript.add(line)
        self.lines_added += 1
        self._write()
        if self._on_line:
            try:
                self._on_line(line)
            except Exception:
                pass

    def _write(self) -> None:
        """Nothing is written before the first line: a call in which nothing
        was said leaves no file."""
        if self.files is None or not self.transcript.lines():
            return
        try:
            self.files.write()
            self.write_error = ""
        except Exception as exc:
            self.write_error = str(exc)
            log.warning("could not write the call transcript: %s", exc)

    # -- the end -------------------------------------------------------------------

    def finish(self, timeout: float = 12.0, ended: Optional[datetime] = None,
               final: bool = False) -> bool:
        """Transcribe what is left, mark the call ended, write the files.
        Returns False if the speech model was still behind after `timeout`
        (it keeps going, and the files are rewritten when it catches up).
        `final`: the app is quitting, so there is no "later"."""
        ended = ended or datetime.now()
        for ch in self.channels.values():
            self._enqueue(ch.flush())
        deadline = time.monotonic() + timeout
        with self._cv:
            self._closing = True
            self._cv.notify_all()
            while (self._queue or self._busy) and time.monotonic() < deadline:
                self._cv.wait(0.1)
            done = not self._queue and not self._busy
        self.transcript.ended = ended
        if not done and final:
            self.transcript.notes.append(
                "Scribe Plus was closed before the last part of this call was transcribed.")
        elif not done:
            self.transcript.notes.append(
                "The last part was still being transcribed when this was saved; "
                "this file updates when it is done.")
            threading.Thread(target=self._finish_late, daemon=True).start()
        self._write()
        return done

    def _finish_late(self) -> None:
        self._worker.join(1800.0)
        if not self._worker.is_alive():
            self.transcript.notes = [n for n in self.transcript.notes
                                     if not n.startswith("The last part was still")]
        self._write()
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                log.exception("late call transcript hand-over failed")

    def summary(self) -> dict:
        me, them = self.channels["Me"], self.channels["Them"]
        return {
            "lines": self.lines_added, "failures": self.failures,
            "me_s": round(me.seconds, 1), "them_s": round(them.seconds, 1),
            "me_peak": round(me.peak, 4), "them_peak": round(them.peak, 4),
            "me_utterances": me.utterances, "them_utterances": them.utterances,
            "stt_audio_s": round(self.stt_seconds, 1), "stt_time_s": round(self.stt_time, 1),
        }
