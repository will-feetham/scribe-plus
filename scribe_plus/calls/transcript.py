"""
The transcript of one call, and the files it lives in.

While a call is going, two files are rewritten every time a line arrives:

    Calls/2026-11-19 1142 Groove OmniDialer.md    this call, kept
    Calls/latest.md                               always the newest call

"latest.md" is the fixed place to point Claude (or anything else) at: it is
the call in progress, or the last one finished, and its Status line says
which. Files are swapped in whole, so a reader never sees half a write.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

ME, THEM = "Me", "Them"
LATEST_NAME = "latest.md"
JOIN_GAP_S = 2.5   # lines by the same speaker this close together read as one


@dataclass
class Line:
    t: float          # seconds from the start of the call
    speaker: str
    text: str
    end: float = 0.0


def clock(t: float) -> str:
    t = max(0, int(t))
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def duration_words(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    m, s = divmod(s, 60)
    return f"{m} min {s} s" if m else f"{s} s"


def safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text or "")
    return " ".join(text.split())[:60] or "Call"


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def similar(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return SequenceMatcher(None, wa, wb).ratio()


class Transcript:
    def __init__(self, title: str, started: datetime, *, app: str = "",
                 windows: list[str] | None = None, engine: str = "") -> None:
        self.title = title
        self.started = started
        self.app = app
        self.windows = list(windows or [])
        self.engine = engine
        self.ended: datetime | None = None
        self.notes: list[str] = []
        self._lines: list[Line] = []
        self._lock = threading.Lock()

    # -- content -------------------------------------------------------------

    def add(self, line: Line) -> None:
        with self._lock:
            i = len(self._lines)
            while i > 0 and self._lines[i - 1].t > line.t:
                i -= 1
            self._lines.insert(i, line)

    @staticmethod
    def _echoes(heard: Line, original: Line, threshold: float) -> bool:
        """Is `heard` the microphone picking up `original` from the speakers?
        An echo is the same words at the same moment. The same words a moment
        later is someone reading them back, which is kept."""
        if len(_words(heard.text)) < 3:
            return False
        end = original.end or original.t
        if not (original.t - 1.0 <= heard.t < end - 0.2):
            return False
        return similar(heard.text, original.text) >= threshold

    def is_echo(self, line: Line, threshold: float = 0.75) -> bool:
        """On speakers the microphone also hears the other side, so their words
        come back a second time as "Me". True when `line` repeats something
        the other speaker was saying at that moment."""
        with self._lock:
            others = [o for o in self._lines if o.speaker != line.speaker]
        return any(self._echoes(line, o, threshold) for o in others)

    def echoes_of(self, line: Line, threshold: float = 0.75) -> list[Line]:
        """Lines already in the transcript that are an echo of `line` (for when
        the microphone's copy was transcribed first)."""
        with self._lock:
            others = [o for o in self._lines if o.speaker != line.speaker]
        return [o for o in others if self._echoes(o, line, threshold)]

    def remove(self, line: Line) -> bool:
        with self._lock:
            for i, existing in enumerate(self._lines):
                if existing is line:
                    del self._lines[i]
                    return True
        return False

    def lines(self) -> list[Line]:
        with self._lock:
            return list(self._lines)

    def turns(self) -> list[Line]:
        """Lines with back-to-back pieces from one speaker joined together."""
        out: list[Line] = []
        for ln in self.lines():
            prev = out[-1] if out else None
            if prev and prev.speaker == ln.speaker and ln.t - (prev.end or prev.t) <= JOIN_GAP_S:
                out[-1] = Line(prev.t, prev.speaker, f"{prev.text} {ln.text}", ln.end or ln.t)
            else:
                out.append(Line(ln.t, ln.speaker, ln.text, ln.end or ln.t))
        return out

    # -- rendering -----------------------------------------------------------

    def body(self) -> str:
        return "\n".join(f"[{clock(t.t)}] {t.speaker}: {t.text}" for t in self.turns())

    def render(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        head = [f"# Call: {self.title}, {self.started:%Y-%m-%d %H:%M}", ""]
        if self.ended:
            length = duration_words((self.ended - self.started).total_seconds())
            head.append(f"Status: ended {self.ended:%H:%M:%S} ({length})")
        else:
            head.append(f"Status: in progress (updated {now:%H:%M:%S})")
        if self.app:
            head.append(f"App: {self.app}")
        if self.windows:
            head.append("Window: " + " | ".join(self.windows))
        head.append('Speakers: "Me" is this computer\'s microphone, "Them" is the other side of the call.')
        for note in self.notes:
            head.append(f"Note: {note}")
        body = self.body() or "(nothing said yet)"
        return "\n".join(head) + "\n\n" + body + "\n"

    def file_name(self) -> str:
        return f"{self.started:%Y-%m-%d %H%M} {safe_name(self.title)}.md"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


_shared = threading.Lock()
_reserved: set[Path] = set()                    # names promised to calls with no file yet
_newest: dict[Path, "TranscriptFiles"] = {}     # per folder: whose text latest.md holds


class TranscriptFiles:
    """Keeps a call's own file and latest.md in step with the transcript.

    Nothing is written until write() is called, so a "call" in which nothing
    was said leaves no file behind and does not disturb latest.md. latest.md
    belongs to the newest call that has written anything: a slow transcript
    still finishing an older call can't take it back."""

    def __init__(self, folder: Path, transcript: Transcript) -> None:
        self.folder = Path(folder)
        self.transcript = transcript
        self.latest = self.folder / LATEST_NAME
        self.error = ""
        self._lock = threading.Lock()
        with _shared:
            self.path = self._free_path(self.folder / transcript.file_name())
            _reserved.add(self.path)

    @staticmethod
    def _free_path(path: Path) -> Path:
        candidate = path
        for n in range(2, 200):
            if not candidate.exists() and candidate not in _reserved:
                return candidate
            candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        return candidate

    def release(self) -> None:
        """The call ended with nothing to write: its file name is free again."""
        with _shared:
            _reserved.discard(self.path)

    def write(self) -> None:
        with self._lock:
            text = self.transcript.render()
            try:
                write_atomic(self.path, text)
                with _shared:
                    newest = _newest.get(self.folder)
                    mine = newest is None or newest is self \
                        or newest.transcript.started <= self.transcript.started
                    if mine:
                        _newest[self.folder] = self
                if mine:
                    write_atomic(self.latest, text)
                with _shared:
                    _reserved.discard(self.path)      # the file itself holds the name now
                self.error = ""
            except OSError as exc:
                self.error = str(exc)
                raise
