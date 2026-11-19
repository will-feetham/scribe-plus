"""
Audio sources for a call. Each one is a child process that writes audio
packets to stdout (see stream.py): the microphone (micproc, any OS) and, on
a Mac, the helper that records what the calling app is playing.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from scribe_plus.calls.stream import PacketReader

log = logging.getLogger(__name__)

AudioCallback = Callable[[np.ndarray, int, float], None]
EventCallback = Callable[[dict], None]


class PacketProcess:
    """Runs a capture helper; calls on_audio(samples, rate, arrived) per packet
    and on_event(dict) for each status line the helper writes to stderr."""

    def __init__(self, name: str, argv: list[str], on_audio: AudioCallback, *,
                 cwd: Path | str | None = None, on_event: Optional[EventCallback] = None,
                 on_exit: Optional[Callable[[int], None]] = None) -> None:
        self.name, self.argv, self.cwd = name, argv, cwd
        self._on_audio, self._on_event, self._on_exit = on_audio, on_event, on_exit
        self._proc: subprocess.Popen | None = None
        self._stopping = False
        self.packets = 0
        self.last_error = ""

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.argv, cwd=str(self.cwd) if self.cwd else None, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read_audio, name=f"{self.name}-audio", daemon=True).start()
        threading.Thread(target=self._read_events, name=f"{self.name}-events", daemon=True).start()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _read_audio(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        reader = PacketReader()
        fd = proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            arrived = time.monotonic()
            for rate, samples in reader.feed(data):
                self.packets += 1
                try:
                    self._on_audio(samples, rate, arrived)
                except Exception:
                    log.exception("%s: audio callback failed", self.name)
        code = proc.wait()
        if not self._stopping:
            log.warning("%s stopped by itself (exit %s) %s", self.name, code, self.last_error)
        if self._on_exit:
            try:
                self._on_exit(code)
            except Exception:
                pass

    def _read_events(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            event: dict
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError
            except ValueError:
                event = {"event": "text", "text": line}
            if event.get("event") == "error":
                self.last_error = f"{event.get('stage')}: {event.get('detail')} ({event.get('code')})"
            if event.get("event") != "level":
                log.info("%s: %s", self.name, line[:500])
            if self._on_event:
                try:
                    self._on_event(event)
                except Exception:
                    log.exception("%s: event callback failed", self.name)

    def stop(self, timeout: float = 1.5) -> None:
        """Closing stdin tells the helper to finish; it is killed if it doesn't."""
        proc = self._proc
        if proc is None:
            return
        self._stopping = True
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(1.0)
            except Exception:
                pass


def mic_source(device_name: str, on_audio: AudioCallback, *, app_dir: Path,
               on_event: Optional[EventCallback] = None) -> PacketProcess:
    argv = [sys.executable, "-u", "-m", "scribe_plus.calls.micproc"]
    if device_name:
        argv += ["--device", device_name]
    return PacketProcess("call-mic", argv, on_audio, cwd=app_dir, on_event=on_event)
