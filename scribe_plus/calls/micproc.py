"""
Microphone capture for calls, run as a child process:

    python -m scribe_plus.calls.micproc [--device NAME]

Writes audio packets (see stream.py) to stdout until stdin closes. It is a
separate process for the same reason Scribe's recorder is: all the sound
driver state lives here, so a stuck driver can be killed without taking the
app with it.
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading


def find_device(sd, name: str):
    """A chosen microphone is remembered by name; None means the system default."""
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d["max_input_channels"] > 0:
            return i
    sys.stderr.write(f"[mic] {name!r} is not connected; using the system default input\n")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scribe Plus call microphone")
    parser.add_argument("--device", default="")
    args = parser.parse_args(argv)

    import sounddevice as sd

    from scribe_plus.calls.stream import TARGET_RATE, pack

    blocks: "queue.Queue" = queue.Queue(maxsize=600)
    stop = threading.Event()

    def on_audio(indata, frames, time_info, status) -> None:
        try:
            blocks.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def watch_stdin() -> None:
        # os.read, not sys.stdin: a thread parked inside sys.stdin's buffer
        # makes Python abort when the program ends for another reason.
        try:
            while os.read(0, 64):
                pass
        except Exception:
            pass
        stop.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    device = find_device(sd, args.device)
    stream, rate = None, TARGET_RATE
    for attempt in (TARGET_RATE, None):
        try:
            if attempt is None:     # this device won't do 16 kHz: take its own rate
                info = sd.query_devices(device if device is not None else sd.default.device[0])
                attempt = int(info["default_samplerate"])
            stream = sd.InputStream(samplerate=attempt, channels=1, dtype="float32",
                                    blocksize=max(160, attempt // 10), device=device,
                                    callback=on_audio)
            stream.start()
            rate = attempt
            break
        except Exception as exc:
            sys.stderr.write(f"[mic] could not open at {attempt} Hz: {exc}\n")
            stream = None
    if stream is None:
        return 1
    sys.stderr.write(f"[mic] open at {rate} Hz\n")
    sys.stderr.flush()

    out = sys.stdout.buffer
    try:
        while not stop.is_set():
            try:
                block = blocks.get(timeout=0.25)
            except queue.Empty:
                continue
            out.write(pack(block, rate))
            out.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
