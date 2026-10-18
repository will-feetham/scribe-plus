"""A failing log file must never kill the dictation threads (Windows can
refuse a log rollover while another program has the file open)."""
import logging
import sys
import threading

from scribe_plus_windows import _LogWriter


class FailingHandler(logging.Handler):
    def emit(self, record):
        try:
            raise PermissionError("[WinError 32] file in use (rollover)")
        except Exception:
            self.handleError(record)


def test_failing_log_handler_does_not_recurse_or_kill_threads():
    root = logging.getLogger()
    old_handlers, old_level, old_raise = root.handlers[:], root.level, logging.raiseExceptions
    old_stderr = sys.stderr
    root.handlers[:] = [FailingHandler()]
    root.setLevel(logging.INFO)
    sys.stderr = _LogWriter(logging.getLogger("scribe"), logging.INFO)
    result = {}
    old_hook = threading.excepthook
    threading.excepthook = lambda args: result.setdefault("died", args.exc_type.__name__)
    try:
        def worker():
            for i in range(3):
                try:
                    raise ValueError("ordinary failure")
                except Exception:
                    logging.getLogger("scribe_plus.pipeline").exception("action failed")
                result["iterations"] = i + 1
        for raise_exc in (True, False):  # with and without the production setting
            logging.raiseExceptions = raise_exc
            t = threading.Thread(target=worker)
            t.start()
            t.join(5)
            assert "died" not in result, result
            assert result["iterations"] == 3
    finally:
        threading.excepthook = old_hook
        sys.stderr = old_stderr
        root.handlers[:] = old_handlers
        root.setLevel(old_level)
        logging.raiseExceptions = old_raise


def test_log_writer_is_file_like():
    w = _LogWriter(logging.getLogger("x"), logging.INFO)
    assert w.isatty() is False and w.encoding == "utf-8"
    assert w.write("partial") == 7 and w.write(" line\n") == 6
