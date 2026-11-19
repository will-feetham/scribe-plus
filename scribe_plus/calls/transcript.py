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

    def is_echo(self, line: Line, window_s: float = 2.5, threshold: float = 0.75) -> bool:
        """On speakers the microphone also hears the other side, so their words
        come back a second time as "Me". True when `line` repeats something
        the other speaker said at the same moment."""
        if len(_words(line.text)) < 3:
            return False
        with self._lock:
            others = [o for o in self._lines if o.speaker != line.speaker
                      and o.t - window_s <= line.t <= (o.end or o.t) + window_s]
        return any(similar(line.text, o.text) >= threshold for o in others)

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
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class TranscriptFiles:
    """Keeps a call's own file and latest.md in step with the transcript."""

    def __init__(self, folder: Path, transcript: Transcript) -> None:
        self.folder = Path(folder)
        self.transcript = transcript
        self.path = self._free_path(self.folder / transcript.file_name())
        self.latest = self.folder / LATEST_NAME
        self._lock = threading.Lock()

    @staticmethod
    def _free_path(path: Path) -> Path:
        if not path.exists():
            return path
        for n in range(2, 100):
            alt = path.with_name(f"{path.stem} ({n}){path.suffix}")
            if not alt.exists():
                return alt
        return path

    def write(self) -> None:
        with self._lock:
            text = self.transcript.render()
            write_atomic(self.path, text)
            write_atomic(self.latest, text)
