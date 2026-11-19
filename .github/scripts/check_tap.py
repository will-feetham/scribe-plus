"""Summarize a capture written by macos/audiotap (CI helper).

    python check_tap.py capture.raw [--need-sound]

Prints the sample rate, length and loudness. With --need-sound, exits 1 when
the capture is missing or silent.
"""
import struct
import sys

import numpy as np

MAGIC = 0x54415053  # "SPAT"


def read_packets(data: bytes):
    pos, rates, chunks = 0, [], []
    while pos + 12 <= len(data):
        magic, rate, frames = struct.unpack_from("<III", data, pos)
        if magic != MAGIC:
            raise ValueError(f"bad packet header at byte {pos}")
        pos += 12
        end = pos + 4 * frames
        if end > len(data):
            break
        chunks.append(np.frombuffer(data, dtype="<f4", count=frames, offset=pos))
        rates.append(rate)
        pos = end
    samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return samples, rates


def main() -> int:
    path = sys.argv[1]
    need_sound = "--need-sound" in sys.argv
    try:
        data = open(path, "rb").read()
    except OSError as exc:
        print(f"{path}: {exc}")
        return 1 if need_sound else 0
    samples, rates = read_packets(data)
    rate = rates[0] if rates else 0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0
    secs = samples.size / rate if rate else 0.0
    print(f"{path}: {len(data)} bytes, {len(rates)} packets, rates {sorted(set(rates))}, "
          f"{secs:.2f} s, peak {peak:.4f}, rms {rms:.4f}")
    if need_sound and (secs < 1.0 or peak < 0.01):
        print("FAIL: expected audible audio")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
