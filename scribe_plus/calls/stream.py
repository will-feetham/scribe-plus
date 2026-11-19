"""
Audio packets and resampling.

Both capture helpers (the Mac's audiotap for the calling app's sound, and
micproc for the microphone) write the same thing to stdout: packets of

    "SPAT"  sample rate (uint32)  frame count (uint32)  mono float32 samples

all little-endian. The rate rides along with every packet because it can
change in the middle of a call (AirPods switch to a lower rate the moment
a call opens their microphone).
"""
from __future__ import annotations

import struct

import numpy as np

MAGIC = 0x54415053  # the bytes "SPAT"
_HEADER = struct.Struct("<III")
TARGET_RATE = 16000
MAX_FRAMES = 1 << 20


def pack(samples: np.ndarray, rate: int) -> bytes:
    x = np.asarray(samples, dtype="<f4")
    return _HEADER.pack(MAGIC, int(rate), int(x.size)) + x.tobytes()


class PacketReader:
    """Turns a byte stream into (rate, samples) packets, a piece at a time."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, np.ndarray]]:
        self._buf += data
        out: list[tuple[int, np.ndarray]] = []
        while len(self._buf) >= _HEADER.size:
            magic, rate, frames = _HEADER.unpack_from(self._buf, 0)
            if magic != MAGIC or frames > MAX_FRAMES or rate == 0:
                # Lost our place (should not happen): look for the next header.
                nxt = self._buf.find(b"SPAT", 1)
                del self._buf[: nxt if nxt > 0 else max(1, len(self._buf) - 3)]
                continue
            end = _HEADER.size + 4 * frames
            if len(self._buf) < end:
                break
            samples = np.frombuffer(bytes(self._buf[_HEADER.size:end]), dtype="<f4").astype(np.float32)
            del self._buf[:end]
            out.append((int(rate), samples))
        return out


def _lowpass(rate_in: int, rate_out: int, taps: int = 63) -> np.ndarray:
    """Windowed-sinc low-pass that keeps what the lower rate can hold."""
    cutoff = 0.45 * min(rate_in, rate_out) / rate_in     # as a fraction of rate_in
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.float64)


class Resampler:
    """Streaming sample-rate conversion (any rate to 16 kHz by default).

    Low-pass filter, then linear interpolation. Feed it consecutive blocks;
    it keeps the few samples of history it needs, so block edges are seamless.
    """

    def __init__(self, rate_in: int, rate_out: int = TARGET_RATE) -> None:
        self.rate_in, self.rate_out = int(rate_in), int(rate_out)
        self.step = self.rate_in / self.rate_out
        self._fir = _lowpass(self.rate_in, self.rate_out) if self.rate_in > self.rate_out else None
        self._tail = np.zeros(len(self._fir) - 1 if self._fir is not None else 0, dtype=np.float64)
        self._prev = np.zeros(1, dtype=np.float64)
        self._pos = 0.0   # where the next output sample falls, in input samples from this block's start

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)
        if self.rate_in == self.rate_out:
            return x.astype(np.float32)
        if self._fir is not None:
            joined = np.concatenate([self._tail, x])
            y = np.convolve(joined, self._fir, mode="valid")      # len(y) == len(x)
            self._tail = joined[len(joined) - len(self._tail):]
        else:
            y = x
        last = len(y) - 1
        n_out = int(np.floor((last - self._pos) / self.step)) + 1 if self._pos <= last else 0
        z = np.concatenate([self._prev, y])                       # z[0] is the sample before this block
        idx = self._pos + 1.0 + self.step * np.arange(n_out)
        out = np.interp(idx, np.arange(len(z)), z) if n_out else np.zeros(0)
        self._pos = self._pos + self.step * n_out - len(y)
        self._prev = y[-1:]
        return out.astype(np.float32)


def resample(x: np.ndarray, rate_in: int, rate_out: int = TARGET_RATE) -> np.ndarray:
    return Resampler(rate_in, rate_out).process(x)
