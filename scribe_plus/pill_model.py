"""
The status pill's look and motion, independent of any GUI toolkit.

PillAnimator holds the pill's state and, once per frame, produces a Frame:
the pill's size, opacity, sideways shake and a short list of shapes to draw
(in pill-local pixels, origin top-left). The Windows pill (tkinter) and the
Mac pill (AppKit) both draw these same frames, so the two look and move
alike, and all of the animation is unit-tested without a display.

Motion, all eased so nothing snaps:
  * appears by growing out of a dot and fading in
  * width morphs smoothly between states (listening, thinking, done)
  * level bars follow the microphone, with a gentle idle wave so the pill
    never looks frozen while you pause between words
  * thinking: three dots in a travelling wave
  * done: the checkmark draws itself, then the pill shrinks away
  * error: a short shake, then it fades out
"""
from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field
from typing import Callable

BG = "#18181B"
BORDER = "#3F3F46"
FG = "#F4F4F5"
DIM = "#A1A1AA"
COMMAND = "#C4B5FD"
HANDSFREE = "#F87171"
OK = "#4ADE80"
ERROR = "#FCA5A5"

HEIGHT = 34            # points / 96-dpi pixels before scaling
BARS = 9
SHOW_ALPHA = 0.95
FONT_PX = 13           # label size before scaling


@dataclass
class Frame:
    width: int
    height: int
    alpha: float
    shake: int = 0
    # Shapes, drawn in order:
    #   ("capsule", x1, y1, x2, y2, fill, outline)
    #   ("dot", cx, cy, r, fill)
    #   ("bar", x, y1, y2, width, colour)          round-capped vertical line
    #   ("polyline", [x0, y0, x1, y1, ...], colour, width)
    #   ("text", x, cy, text, colour, anchor)      anchor "w" or "center"
    items: list = field(default_factory=list)


def _hex_grey(v: float) -> str:
    shade = int(0x71 + (0xF4 - 0x71) * max(0.0, min(1.0, v)))
    return f"#{shade:02x}{shade:02x}{min(0xF5, shade + 1):02x}"


class PillAnimator:
    def __init__(self, scale: float = 1.0, measure: Callable[[str], float] | None = None) -> None:
        self.s = max(1.0, float(scale))
        self.h = int(HEIGHT * self.s)
        self.font_px = int(round(FONT_PX * self.s))
        self._measure = measure or (lambda text: 0.55 * self.font_px * len(text))
        self.state = "hidden"
        self.mode = "dictate"
        self.message = ""
        self.levels: collections.deque = collections.deque([0.0] * BARS, maxlen=BARS)
        self.bars = [0.0] * BARS
        self.w = float(self.h)
        self.w_target = float(self.h)
        self.alpha = 0.0
        self.alpha_target = 0.0
        self.closing = False
        self.hide_at: float | None = None
        self.t_state = 0.0

    # -- input ---------------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self.state != "hidden"

    def set_state(self, state: str, now: float, **kw) -> bool:
        """Apply a state change. True means the pill is (re)appearing, so the
        renderer should place it on the active screen and bring it to front."""
        if state == "level":
            self.levels.append(float(kw.get("value", 0.0)))
            return False
        if state in ("idle", "hidden"):
            self.begin_close()
            return False
        appearing = self.state == "hidden" or self.closing
        if state == "recording":
            mode = kw.get("mode", "dictate")
            if self.state != "recording" or mode != "handsfree":
                self.levels.extend([0.0] * BARS)
            self.state, self.mode, self.hide_at = "recording", mode, None
        elif state == "processing":
            self.state, self.hide_at = "processing", None
        elif state == "done":
            self.state, self.hide_at = "done", now + 0.75
        elif state in ("error", "info"):
            self.state, self.message = state, str(kw.get("message", ""))
            self.hide_at = now + 2.4
        elif state == "cancelled":
            self.state, self.message, self.hide_at = "info", "Cancelled", now + 0.9
        else:
            return False
        self.t_state = now
        self.closing = False
        self.w_target = float(self.target_width())
        self.alpha_target = SHOW_ALPHA
        if appearing and self.alpha < 0.05:
            self.w = float(self.h)  # grow out of a dot
        return appearing

    def begin_close(self) -> None:
        if self.state == "hidden":
            return
        self.closing = True
        self.hide_at = None
        self.alpha_target = 0.0
        self.w_target = float(self.h)

    # -- layout ----------------------------------------------------------------

    def label(self) -> str:
        if self.state == "recording":
            return {"command": "Command", "handsfree": "Hands-free"}.get(self.mode, "")
        if self.state in ("error", "info"):
            return self.message
        return ""

    def text_width(self, text: str) -> int:
        try:
            return int(self._measure(text))
        except Exception:
            return int(0.55 * self.font_px * len(text))

    def target_width(self) -> int:
        s = self.s
        pad = int(14 * s)
        if self.state == "recording":
            bars_w = int(BARS * 3 * s + (BARS - 1) * 3 * s)
            label = self.label()
            w = pad * 2 + bars_w + (self.text_width(label) + int(10 * s) if label else 0)
            if self.mode == "handsfree":
                w += int(14 * s)
        elif self.state == "processing":
            w = int(72 * s)
        elif self.state == "done":
            w = int(56 * s)
        else:
            w = pad * 2 + self.text_width(self.label())
        return max(w, int(56 * s))

    # -- animation -----------------------------------------------------------

    def step(self, now: float, dt: float) -> Frame | None:
        """Advance one frame. None means the pill has just finished hiding."""
        dt = min(0.1, max(0.0, dt))
        if self.hide_at is not None and now >= self.hide_at:
            self.begin_close()
        ease_w = 1.0 - math.exp(-dt / 0.07)
        ease_a = 1.0 - math.exp(-dt / (0.06 if self.closing else 0.045))
        self.w += (self.w_target - self.w) * ease_w
        self.alpha += (self.alpha_target - self.alpha) * ease_a
        if self.closing and self.alpha < 0.02:
            self.alpha = 0.0
            self.state = "hidden"
            self.closing = False
            return None
        return self._frame(now, dt)

    def _shake(self, now: float) -> int:
        if self.state != "error" or self.closing:
            return 0
        t = now - self.t_state
        if t >= 0.4:
            return 0
        return int(6 * self.s * math.sin(t * 48.0) * (1.0 - t / 0.4))

    def _frame(self, now: float, dt: float) -> Frame:
        s, h = self.s, self.h
        w = max(h, int(round(self.w)))
        f = Frame(width=w, height=h, alpha=self.alpha, shake=self._shake(now))
        f.items.append(("capsule", 1, 1, w - 2, h - 2, BG, BORDER))
        # Content appears once the pill is wide enough, and not while closing.
        room = (self.w - h) / max(1.0, self.w_target - h) if self.w_target > h else 1.0
        if self.closing or room < 0.6:
            return f
        mid = h / 2
        if self.state == "recording":
            self._recording(f, s, mid, now, dt)
        elif self.state == "processing":
            for i in range(3):
                phase = (math.sin(now * 7.0 - i * 0.95) + 1) / 2
                r = (2.2 + 1.8 * phase) * s
                cx = w / 2 + (i - 1) * 12 * s
                cy = mid - 2.5 * s * phase
                f.items.append(("dot", cx, cy, r, _hex_grey(phase)))
        elif self.state == "done":
            p = min(1.0, (now - self.t_state) / 0.24)
            p = 1 - (1 - p) ** 3  # ease out
            pts = [(w / 2 - 7 * s, mid), (w / 2 - 2 * s, mid + 5 * s), (w / 2 + 8 * s, mid - 6 * s)]
            seg1 = math.dist(pts[0], pts[1])
            seg2 = math.dist(pts[1], pts[2])
            run = p * (seg1 + seg2)
            line = [pts[0]]
            if run <= seg1:
                k = run / seg1
                line.append((pts[0][0] + (pts[1][0] - pts[0][0]) * k,
                             pts[0][1] + (pts[1][1] - pts[0][1]) * k))
            else:
                k = (run - seg1) / seg2
                line += [pts[1], (pts[1][0] + (pts[2][0] - pts[1][0]) * k,
                                  pts[1][1] + (pts[2][1] - pts[1][1]) * k)]
            if len(line) >= 2 and run > 0:
                f.items.append(("polyline", [v for pt in line for v in pt], OK,
                                max(2.0, 2.6 * s)))
        else:
            colour = ERROR if self.state == "error" else FG
            f.items.append(("text", w / 2, mid, self.label(), colour, "center"))
        return f

    def _recording(self, f: Frame, s: float, mid: float, now: float, dt: float) -> None:
        x = 14 * s
        if self.mode == "handsfree":
            pulse = 0.5 + 0.5 * math.sin(now * 4.0)
            f.items.append(("dot", x + 4 * s, mid, (3.4 + 1.2 * pulse) * s, HANDSFREE))
            x += 14 * s
        label = self.label()
        colour = COMMAND if self.mode == "command" else FG
        if label:
            f.items.append(("text", x, mid, label, colour, "w"))
            x += self.text_width(label) + 10 * s
        levels = list(self.levels)
        ease = 1.0 - math.exp(-max(dt, 0.001) / 0.06)
        for i in range(BARS):
            raw = levels[-1 - abs(i - BARS // 2)] if levels else 0.0
            idle_wave = 0.12 + 0.10 * math.sin(now * 5.0 - i * 0.8)  # alive while you pause
            target = max(raw, idle_wave)
            self.bars[i] += (target - self.bars[i]) * ease
            bh = (4 + 16 * min(1.0, self.bars[i])) * s
            bx = x + i * 6 * s + 1.5 * s
            f.items.append(("bar", bx, mid - bh / 2, mid + bh / 2, 3 * s, colour))
