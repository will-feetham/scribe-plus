"""
Audio preparation between the recorder and the speech model.

  * silence check: a clip with nothing above the noise floor is dropped
    before upload (Whisper invents "Thank you." on silent clips)
  * quiet-voice boost: whispered speech is normalized up to a usable level
  * edge trimming: leading/trailing silence is cut, keeping 200 ms margins

All of it is plain numpy on 16 kHz mono int16, which is what Scribe records.
"""
from __future__ import annotations

import io
import math
import wave
from dataclasses import dataclass

import numpy as np

FRAME_S = 0.02
MARGIN_S = 0.20
SILENCE_PEAK_DBFS = -50.0
SILENCE_LOUD_DBFS = -55.0
BOOST_BELOW_PEAK_DBFS = -6.0
BOOST_TARGET_PEAK_DBFS = -3.0
MAX_BOOST_DB = 24.0


def dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-9))


def wav_to_pcm(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit audio, got {8 * width}-bit")
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.int16)
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return pcm, sr


def pcm_to_wav(pcm: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.asarray(pcm, dtype="<i2").tobytes())
    return buf.getvalue()


@dataclass
class Prepared:
    wav: bytes
    sample_rate: int
    duration_s: float
    kept_s: float
    peak_dbfs: float
    loud_dbfs: float
    noise_dbfs: float
    gain_db: float
    silent: bool


def _frame_db(x: np.ndarray, frame: int) -> np.ndarray:
    n = len(x) // frame
    if n == 0:
        return np.array([dbfs(float(np.sqrt(np.mean(x ** 2)))) if len(x) else -180.0])
    sq = x[: n * frame].reshape(n, frame) ** 2
    rms = np.sqrt(sq.mean(axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1e-9))


def prepare(wav_bytes: bytes, *, boost: bool = True, trim: bool = True) -> Prepared:
    pcm, sr = wav_to_pcm(wav_bytes)
    duration = len(pcm) / sr if sr else 0.0
    if len(pcm) == 0:
        return Prepared(wav_bytes, sr, 0.0, 0.0, -180.0, -180.0, -180.0, 0.0, True)

    x = pcm.astype(np.float32) / 32768.0
    peak = dbfs(float(np.max(np.abs(x))))
    frame = max(1, int(sr * FRAME_S))
    fdb = _frame_db(x, frame)
    noise = float(np.percentile(fdb, 10))
    loud = float(np.percentile(fdb, 95))
    if peak < SILENCE_PEAK_DBFS or loud < SILENCE_LOUD_DBFS:
        return Prepared(wav_bytes, sr, duration, 0.0, peak, loud, noise, 0.0, True)

    start, end = 0, len(x)
    if trim and len(fdb) > 1:
        threshold = min(max(noise + 10.0, -60.0), loud - 6.0)
        active = np.nonzero(fdb > threshold)[0]
        if active.size:
            margin = int(MARGIN_S / FRAME_S)
            s = max(0, int(active[0]) - margin) * frame
            e = min(len(x), (int(active[-1]) + 1 + margin) * frame)
            if (e - s) / sr >= 0.3:
                start, end = s, e
    x = x[start:end]

    gain_db = 0.0
    if boost and peak < BOOST_BELOW_PEAK_DBFS:
        gain_db = min(BOOST_TARGET_PEAK_DBFS - peak, MAX_BOOST_DB)
        x = np.clip(x * (10.0 ** (gain_db / 20.0)), -1.0, 1.0)

    changed = (start, end) != (0, len(pcm)) or gain_db > 0
    out = pcm_to_wav((x * 32767.0).astype(np.int16), sr) if changed else wav_bytes
    return Prepared(out, sr, duration, len(x) / sr, peak, loud, noise, gain_db, False)


def level_from_pcm_bytes(raw: bytes) -> float:
    """0..1 loudness of a chunk of raw int16 PCM, for the on-screen meter."""
    n = len(raw) // 2
    if n == 0:
        return 0.0
    x = np.frombuffer(raw[: n * 2], dtype="<i2").astype(np.float32) / 32768.0
    level = (dbfs(float(np.sqrt(np.mean(x ** 2)))) + 55.0) / 45.0
    return float(min(1.0, max(0.0, level)))
