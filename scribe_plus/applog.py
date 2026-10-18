"""
Logging for the tray apps. They run without a console (pythonw on Windows,
an app bundle on a Mac), so everything, including upstream scribe_core's
prints, goes to scribe-plus.log in the data folder.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading

from scribe_plus import settings as S


class _LogWriter:
    """File-like object that turns print() output into log lines.

    Re-entrancy guard: if writing the log ever fails (Windows can refuse a
    log rollover while another program has the log open), logging reports
    the error by printing to stderr, which is this object again. Without the
    guard that loops until a RecursionError kills the calling thread.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self.logger, self.level, self._buf = logger, level, ""
        self._local = threading.local()

    def write(self, msg: str) -> int:
        if getattr(self._local, "busy", False):
            return len(msg)
        self._local.busy = True
        try:
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self.logger.log(self.level, line.rstrip())
        except Exception:
            pass
        finally:
            self._local.busy = False
        return len(msg)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("log writer has no file descriptor")


def setup_logging(debug: bool) -> None:
    # A failing log write must never take the app down with it.
    logging.raiseExceptions = False
    S.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        S.LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if debug and sys.__stderr__ is not None:
        handlers.append(logging.StreamHandler(sys.__stderr__))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if sys.stdout is None or not debug:
        sys.stdout = _LogWriter(logging.getLogger("stdout"), logging.INFO)
    if sys.stderr is None or not debug:
        sys.stderr = _LogWriter(logging.getLogger("scribe"), logging.INFO)
