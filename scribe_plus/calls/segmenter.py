"""
Cuts a live audio stream into utterances.

Speech-to-text works on finished clips, so a call is transcribed one
utterance at a time: a stretch of speech, ended by a pause. That is what
lets the transcript appear while the call is still going.

The detector is a plain energy gate with a moving noise floor, which is
enough here: each channel carries one side of a call, and anything it lets
through by mistake comes back from the speech model as nothing.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

RATE = 16000


@dataclass
class Utterance:
    start: int            # first sample, counted from the start of the stream
    end: int              # one past the last sample
    samples: np.ndarray   # float32, 16 kHz mono
    speech_s: float       # how much of it was above the noise
    peak: float           # largest absolute sample

    @property
    def duration_s(self) -> float:
        return len(self.samples) / RATE


def _db(x: np.ndarray) -> float:
    if x.size == 0:
        return -180.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * np.log10(max(rms, 1e-9))


class Segmenter:
    def __init__(self, *, frame_ms: int = 20, end_silence_s: float = 0.7, max_s: float = 15.0,
                 pre_roll_s: float = 0.3, tail_s: float = 0.2, min_speech_s: float = 0.25,
                 margin_db: float = 8.0, floor_db: float = -65.0) -> None:
        self.frame = RATE * frame_ms // 1000
        self.frame_s = frame_ms / 1000.0
        self.end_frames = max(1, round(end_silence_s / self.frame_s))
        self.max_frames = max(10, round(max_s / self.frame_s))
        self.tail_frames = max(0, round(tail_s / self.frame_s))
        self.min_speech_frames = max(1, round(min_speech_s / self.frame_s))
        self.margin_db, self.floor_db = margin_db, floor_db
        self._pending = np.zeros(0, dtype=np.float32)
        self._pos = 0                       # sample index of the next frame
        self._noise: float | None = None
        self._recent: deque = deque(maxlen=max(5, round(pre_roll_s / self.frame_s)))
        self._utt: list[tuple[np.ndarray, bool, float]] = []   # (frame, is speech, dB)
        self._utt_start = 0
        self._quiet = 0                     # non-speech frames at the end of the utterance

    @property
    def noise_db(self) -> float:
        return -90.0 if self._noise is None else self._noise

    @property
    def speaking(self) -> bool:
        return bool(self._utt)

    def feed(self, x: np.ndarray) -> list[Utterance]:
        out: list[Utterance] = []
        data = np.concatenate([self._pending, np.asarray(x, dtype=np.float32)])
        n = len(data) // self.frame
        for i in range(n):
            self._frame(data[i * self.frame:(i + 1) * self.frame], out)
        self._pending = data[n * self.frame:]
        return out

    def flush(self) -> list[Utterance]:
        """End of the stream (or a gap in it): finish whatever is in progress."""
        out: list[Utterance] = []
        if self._utt:
            self._emit(len(self._utt), out, keep_tail=True)
        self._utt = []
        self._quiet = 0
        self._recent.clear()
        self._pos += len(self._pending)
        self._pending = np.zeros(0, dtype=np.float32)
        return out

    # -- internals ---------------------------------------------------------

    def _frame(self, f: np.ndarray, out: list[Utterance]) -> None:
        db = _db(f)
        if self._noise is None:
            self._noise = min(max(db, -90.0), -45.0)
        threshold = max(self._noise + self.margin_db, self.floor_db)
        speech = db > threshold
        if speech:
            self._noise += 0.0005 * (db - self._noise)          # in case the room itself got louder
        elif db < self._noise:
            self._noise += 0.2 * (db - self._noise)
        else:
            self._noise += 0.02 * (db - self._noise)
        self._noise = max(self._noise, -90.0)

        start = self._pos
        self._pos += len(f)
        if not self._utt:
            self._recent.append((f, speech, db, start))
            window = list(self._recent)[-5:]
            if sum(1 for r in window if r[1]) >= 3:
                # Speech has begun: open the utterance with the lead-in we kept.
                self._utt = [(r[0], r[1], r[2]) for r in self._recent]
                self._utt_start = self._recent[0][3]
                self._recent.clear()
                self._quiet = 0 if speech else 1
            return

        self._utt.append((f, speech, db))
        self._quiet = 0 if speech else self._quiet + 1
        if self._quiet >= self.end_frames:
            self._emit(len(self._utt), out, keep_tail=False)
            self._utt = []
            self._quiet = 0
        elif len(self._utt) >= self.max_frames:
            # Someone is talking without a pause: cut at the quietest recent spot.
            window = min(len(self._utt) - 1, round(3.0 / self.frame_s))
            first = len(self._utt) - window
            cut = min(range(first, len(self._utt)), key=lambda i: self._utt[i][2]) + 1
            rest = self._utt[cut:]
            self._emit(cut, out, keep_tail=True)
            self._utt = rest
            self._utt_start += cut * self.frame
            self._quiet = 0
            for _f, is_speech, _d in reversed(rest):
                if is_speech:
                    break
                self._quiet += 1

    def _emit(self, count: int, out: list[Utterance], keep_tail: bool) -> None:
        frames = self._utt[:count]
        if not keep_tail:
            # Drop the closing silence, except a short tail so words aren't clipped.
            trailing = 0
            for _f, is_speech, _d in reversed(frames):
                if is_speech:
                    break
                trailing += 1
            drop = max(0, trailing - self.tail_frames)
            if drop:
                frames = frames[:len(frames) - drop]
        speech_frames = sum(1 for _f, is_speech, _d in frames if is_speech)
        if speech_frames < self.min_speech_frames or not frames:
            return
        samples = np.concatenate([f for f, _s, _d in frames]).astype(np.float32)
        out.append(Utterance(self._utt_start, self._utt_start + len(samples), samples,
                             speech_frames * self.frame_s, float(np.max(np.abs(samples)))))
