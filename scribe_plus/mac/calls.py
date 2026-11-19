"""
Call recording on a Mac: the pieces that talk to macOS.

All of it goes through "audiotap", a small helper program built from
macos/audiotap.m and installed next to the Python code:

    audiotap watch     which apps have the microphone open -> a call started
    audiotap capture   what the calling app is playing     -> the other side

Needs macOS 14.2 or newer. Recording another app's sound needs the
"System Audio Recording" permission, which macOS asks for on the first call.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from scribe_plus.calls.detector import Detected
from scribe_plus.calls.sources import AudioCallback, EventCallback, PacketProcess

log = logging.getLogger(__name__)

HELPER_NAME = "audiotap"


class MacCallPlatform:
    def __init__(self, app_dir: Path) -> None:
        self.helper = Path(app_dir) / "macos" / HELPER_NAME
        self._available: Optional[tuple[bool, str]] = None
        self._watch: subprocess.Popen | None = None
        self._stop = threading.Event()

    # -- can this Mac do it? ---------------------------------------------------

    def available(self) -> tuple[bool, str]:
        if self._available is None:
            self._available = self._check()
        return self._available

    def _check(self) -> tuple[bool, str]:
        if not self.helper.exists():
            return False, "the call-recording helper isn't installed (run the installer again)"
        try:
            out = subprocess.run([str(self.helper), "version"], capture_output=True,
                                 timeout=5, check=False)
            info = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
        except Exception as exc:
            return False, f"the call-recording helper doesn't run ({exc})"
        if not info.get("supported"):
            return False, f"call recording needs macOS 14.2 or newer (this Mac has {info.get('macos', '?')})"
        return True, ""

    # -- noticing calls -----------------------------------------------------------

    def start_watch(self, on_procs: Callable[[list[dict]], None]) -> None:
        self._stop.clear()
        threading.Thread(target=self._watch_loop, args=(on_procs,), name="call-watch",
                         daemon=True).start()

    def _watch_loop(self, on_procs: Callable[[list[dict]], None]) -> None:
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen([str(self.helper), "watch", "--interval", "0.5"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
            except Exception as exc:
                log.warning("could not start the call watcher: %s", exc)
                return
            self._watch = proc
            assert proc.stdout is not None
            for raw in proc.stdout:
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                    procs = event.get("procs")
                    if isinstance(procs, list):
                        on_procs(procs)
                except Exception:
                    log.debug("unreadable line from the call watcher: %r", raw[:200])
            proc.wait()
            if self._stop.is_set():
                return
            # The watcher died: nothing is known to be using the microphone.
            try:
                on_procs([])
            except Exception:
                pass
            log.warning("the call watcher stopped (exit %s); starting it again", proc.returncode)
            self._stop.wait(5.0)

    def stop_watch(self) -> None:
        self._stop.set()
        proc = self._watch
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # -- the other side of the call --------------------------------------------------

    def tap_source(self, detected: Optional[Detected], system: bool, on_audio: AudioCallback,
                   on_event: Optional[EventCallback] = None) -> PacketProcess:
        argv = [str(self.helper), "capture"]
        if system or detected is None:
            argv.append("--system")
        else:
            # Only this app's sound. If the app has no audio open after all,
            # the helper records all sound instead of nothing.
            for bundle in detected.bundles:
                argv += ["--match", bundle]
            for root in detected.roots:
                argv += ["--path", root]
        return PacketProcess("call-tap", argv, on_audio, on_event=on_event)

    # -- context ------------------------------------------------------------------------

    def window_titles(self, bundles: list[str]) -> list[str]:
        from scribe_plus.mac import context
        return context.window_titles(bundles)
