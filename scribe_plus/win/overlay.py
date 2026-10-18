"""
The floating status pill, drawn and animated with tkinter.

It must never take keyboard focus, or the paste would land in the pill
instead of the user's app. On Windows the window gets WS_EX_NOACTIVATE,
WS_EX_TOOLWINDOW (no taskbar/Alt+Tab entry) and WS_EX_TRANSPARENT (clicks
pass through), and it is hidden by dropping its opacity to 0 rather than
withdrawing it, since re-showing a withdrawn Tk window can activate it.

All methods run on the Tk thread; other threads go through UiBridge.
"""
from __future__ import annotations

import collections
import ctypes
import math
import sys
import time
import tkinter as tk

BG = "#18181B"
BORDER = "#3F3F46"
FG = "#F4F4F5"
DIM = "#A1A1AA"
COMMAND = "#C4B5FD"
HANDSFREE = "#F87171"
OK = "#4ADE80"
ERROR = "#FCA5A5"
KEY = "#010203"  # colour keyed out to transparent on Windows (rounded corners)

WIN = sys.platform == "win32"

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
MONITOR_DEFAULTTONEAREST = 2


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


_user32 = None


def _u32():
    global _user32
    if _user32 is None:
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetParent.argtypes = [wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        get = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        put = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        get.argtypes = [wintypes.HWND, ctypes.c_int]
        get.restype = ctypes.c_ssize_t
        put.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        put.restype = ctypes.c_ssize_t
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = wintypes.BOOL
        u.GetForegroundWindow.restype = wintypes.HWND
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = wintypes.HANDLE
        u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        _user32 = (u, get, put)
    return _user32


def _capsule(c: tk.Canvas, x1, y1, x2, y2, fill: str, outline: str) -> None:
    """A pill shape: two half-circle caps joined by straight edges."""
    d = y2 - y1
    c.create_oval(x1, y1, x1 + d, y2, fill=fill, outline="")
    c.create_oval(x2 - d, y1, x2, y2, fill=fill, outline="")
    c.create_rectangle(x1 + d / 2, y1, x2 - d / 2, y2, fill=fill, outline="")
    c.create_arc(x1, y1, x1 + d, y2, start=90, extent=180, style="arc", outline=outline)
    c.create_arc(x2 - d, y1, x2, y2, start=-90, extent=180, style="arc", outline=outline)
    c.create_line(x1 + d / 2, y1, x2 - d / 2, y1, fill=outline)
    c.create_line(x1 + d / 2, y2, x2 - d / 2, y2, fill=outline)


class Pill:
    """Animated status pill.

    Motion, all eased so nothing snaps:
      * appears by growing out of a dot and fading in
      * width morphs smoothly between states (listening, thinking, done)
      * level bars follow the microphone, with a gentle idle wave so the
        pill never looks frozen while you pause between words
      * thinking: three dots in a travelling wave
      * done: the checkmark draws itself, then the pill shrinks away
      * error: a short shake, then it fades out
    """

    HEIGHT = 34
    BARS = 9
    FRAME_MS = 16          # ~60 fps while visible
    SHOW_ALPHA = 0.95

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.h = int(self.HEIGHT * self.s)
        self.state = "hidden"
        self.mode = "dictate"
        self.message = ""
        self.levels: collections.deque = collections.deque([0.0] * self.BARS, maxlen=self.BARS)
        self._bars = [0.0] * self.BARS
        self._w = float(self.h)
        self._w_target = float(self.h)
        self._alpha = 0.0
        self._alpha_target = 0.0
        self._closing = False
        self._hide_at: float | None = None
        self._t_state = time.monotonic()
        self._last = time.monotonic()
        self._running = False
        self._area = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        self._geom = None
        # Negative size = pixels, so text scales with the pill on any DPI.
        self._font = ("Segoe UI" if WIN else "DejaVu Sans", -int(round(13 * self.s)))

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.configure(bg=KEY)
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        if WIN:
            self.win.attributes("-transparentcolor", KEY)
        self._set_alpha(0.0)
        self.canvas = tk.Canvas(self.win, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.win.geometry("10x10+-200+-200")
        self.win.update_idletasks()
        self._hwnd = None
        self._apply_win32_styles()

    # -- public, Tk thread only -------------------------------------------

    def set_state(self, state: str, **kw) -> None:
        now = time.monotonic()
        if state == "level":
            self.levels.append(float(kw.get("value", 0.0)))
            return
        if state in ("idle", "hidden"):
            self._begin_close()
            return
        appearing = self.state == "hidden" or self._closing
        if state == "recording":
            mode = kw.get("mode", "dictate")
            if self.state != "recording" or mode != "handsfree":
                self.levels.extend([0.0] * self.BARS)
            self.state, self.mode, self._hide_at = "recording", mode, None
        elif state == "processing":
            self.state, self._hide_at = "processing", None
        elif state == "done":
            self.state, self._hide_at = "done", now + 0.75
        elif state in ("error", "info"):
            self.state, self.message = state, str(kw.get("message", ""))
            self._hide_at = now + 2.4
        elif state == "cancelled":
            self.state, self.message, self._hide_at = "info", "Cancelled", now + 0.9
        else:
            return
        self._t_state = now
        self._closing = False
        self._w_target = float(self._target_width())
        self._alpha_target = self.SHOW_ALPHA
        if appearing:
            if self._alpha < 0.05:
                self._w = float(self.h)          # grow out of a dot
            self._area = self._work_area()
            self._apply_win32_styles()
            self._raise()
        self._ensure_running()

    # -- animation loop ------------------------------------------------------

    def _ensure_running(self) -> None:
        if not self._running:
            self._running = True
            self._last = time.monotonic()
            self.root.after(self.FRAME_MS, self._loop)

    def _begin_close(self) -> None:
        if self.state == "hidden":
            return
        self._closing = True
        self._hide_at = None
        self._alpha_target = 0.0
        self._w_target = float(self.h)
        self._ensure_running()

    def _loop(self) -> None:
        now = time.monotonic()
        dt = min(0.1, max(0.0, now - self._last))
        self._last = now
        if self._hide_at is not None and now >= self._hide_at:
            self._begin_close()
        ease_w = 1.0 - math.exp(-dt / 0.07)
        ease_a = 1.0 - math.exp(-dt / (0.06 if self._closing else 0.045))
        self._w += (self._w_target - self._w) * ease_w
        self._alpha += (self._alpha_target - self._alpha) * ease_a
        if self._closing and self._alpha < 0.02:
            self._alpha = 0.0
            self._set_alpha(0.0)
            self.state = "hidden"
            self._closing = False
            self._running = False
            return
        self._apply_geometry(now)
        self._set_alpha(self._alpha)
        try:
            self._draw(now, dt)
        except tk.TclError:
            pass
        self.root.after(self.FRAME_MS, self._loop)

    # -- layout --------------------------------------------------------------

    def _text_width(self, text: str) -> int:
        try:
            item = self.canvas.create_text(0, -100, text=text, font=self._font, anchor="w")
            x1, _y1, x2, _y2 = self.canvas.bbox(item)
            self.canvas.delete(item)
            return x2 - x1
        except Exception:
            return int(7 * self.s * len(text))

    def _label(self) -> str:
        if self.state == "recording":
            return {"command": "Command", "handsfree": "Hands-free"}.get(self.mode, "")
        if self.state in ("error", "info"):
            return self.message
        return ""

    def _target_width(self) -> int:
        s = self.s
        pad = int(14 * s)
        if self.state == "recording":
            bars_w = int(self.BARS * 3 * s + (self.BARS - 1) * 3 * s)
            label = self._label()
            w = pad * 2 + bars_w + (self._text_width(label) + int(10 * s) if label else 0)
            if self.mode == "handsfree":
                w += int(14 * s)
        elif self.state == "processing":
            w = int(72 * s)
        elif self.state == "done":
            w = int(56 * s)
        else:
            w = pad * 2 + self._text_width(self._label())
        return max(w, int(56 * s))

    def _apply_geometry(self, now: float) -> None:
        left, _top, right, bottom = self._area
        w = max(self.h, int(round(self._w)))
        shake = 0
        if self.state == "error" and not self._closing:
            t = now - self._t_state
            if t < 0.4:
                shake = int(6 * self.s * math.sin(t * 48.0) * (1.0 - t / 0.4))
        x = left + (right - left - w) // 2 + shake
        y = bottom - self.h - int(28 * self.s)
        geom = (w, x, y)
        if geom != self._geom:
            self._geom = geom
            self.win.geometry(f"{w}x{self.h}+{x}+{y}")

    # -- drawing ---------------------------------------------------------------

    def _draw(self, now: float, dt: float) -> None:
        c, s, h = self.canvas, self.s, self.h
        w = max(h, int(round(self._w)))
        c.delete("all")
        _capsule(c, 1, 1, w - 2, h - 2, fill=BG, outline=BORDER)
        # Content appears once the pill is wide enough, and not while closing.
        room = (self._w - h) / max(1.0, self._w_target - h) if self._w_target > h else 1.0
        if self._closing or room < 0.6:
            return
        mid = h / 2
        if self.state == "recording":
            self._draw_recording(c, s, mid, now, dt)
        elif self.state == "processing":
            for i in range(3):
                phase = (math.sin(now * 7.0 - i * 0.95) + 1) / 2
                r = (2.2 + 1.8 * phase) * s
                cx = w / 2 + (i - 1) * 12 * s
                cy = mid - 2.5 * s * phase
                shade = int(0x71 + (0xF4 - 0x71) * phase)
                c.create_oval(cx - r, cy - r, cx + r, cy + r,
                              fill=f"#{shade:02x}{shade:02x}{min(0xF5, shade + 1):02x}", outline="")
        elif self.state == "done":
            p = min(1.0, (now - self._t_state) / 0.24)
            p = 1 - (1 - p) ** 3  # ease out
            pts = [(w / 2 - 7 * s, mid), (w / 2 - 2 * s, mid + 5 * s), (w / 2 + 8 * s, mid - 6 * s)]
            seg1 = math.dist(pts[0], pts[1])
            seg2 = math.dist(pts[1], pts[2])
            run = p * (seg1 + seg2)
            line = [pts[0]]
            if run <= seg1:
                f = run / seg1
                line.append((pts[0][0] + (pts[1][0] - pts[0][0]) * f, pts[0][1] + (pts[1][1] - pts[0][1]) * f))
            else:
                f = (run - seg1) / seg2
                line += [pts[1], (pts[1][0] + (pts[2][0] - pts[1][0]) * f,
                                  pts[1][1] + (pts[2][1] - pts[1][1]) * f)]
            if len(line) >= 2:
                flat = [v for pt in line for v in pt]
                c.create_line(*flat, fill=OK, width=max(2, int(2.6 * s)),
                              capstyle="round", joinstyle="round")
        else:
            colour = ERROR if self.state == "error" else FG
            c.create_text(w / 2, mid, text=self._label(), font=self._font, fill=colour)

    def _draw_recording(self, c: tk.Canvas, s: float, mid: float, now: float, dt: float) -> None:
        x = int(14 * s)
        if self.mode == "handsfree":
            pulse = 0.5 + 0.5 * math.sin(now * 4.0)
            r = (3.4 + 1.2 * pulse) * s
            c.create_oval(x + 4 * s - r, mid - r, x + 4 * s + r, mid + r, fill=HANDSFREE, outline="")
            x += int(14 * s)
        label = self._label()
        colour = COMMAND if self.mode == "command" else FG
        if label:
            c.create_text(x, mid, text=label, font=self._font, fill=colour, anchor="w")
            x += self._text_width(label) + int(10 * s)
        levels = list(self.levels)
        n = self.BARS
        ease = 1.0 - math.exp(-max(dt, 0.001) / 0.06)
        for i in range(n):
            raw = levels[-1 - abs(i - n // 2)] if levels else 0.0
            idle_wave = 0.12 + 0.10 * math.sin(now * 5.0 - i * 0.8)   # alive while you pause
            target = max(raw, idle_wave)
            self._bars[i] += (target - self._bars[i]) * ease
            bh = (4 + 16 * min(1.0, self._bars[i])) * s
            bx = x + i * 6 * s + 1.5 * s
            cap = 1.5 * s                       # round caps add this much at each end
            c.create_line(bx, mid - bh / 2 + cap, bx, mid + bh / 2 - cap, fill=colour,
                          width=max(2, int(round(3 * s))), capstyle="round")

    # -- window management ---------------------------------------------------

    def _set_alpha(self, a: float) -> None:
        try:
            self.win.attributes("-alpha", a)
        except tk.TclError:
            pass

    def _work_area(self) -> tuple[int, int, int, int]:
        if WIN:
            try:
                u, _g, _p = _u32()
                mon = u.MonitorFromWindow(u.GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
                info = _MONITORINFO()
                info.cbSize = ctypes.sizeof(_MONITORINFO)
                if mon and u.GetMonitorInfoW(mon, ctypes.byref(info)):
                    r = info.rcWork
                    return r.left, r.top, r.right, r.bottom
            except Exception:
                pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _raise(self) -> None:
        if WIN and self._hwnd:
            try:
                u, _g, _p = _u32()
                u.SetWindowPos(self._hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0,
                               SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            except Exception:
                pass

    def _apply_win32_styles(self) -> None:
        if not WIN:
            return
        try:
            u, get, put = _u32()
            hwnd = u.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            self._hwnd = hwnd
            ex = get(hwnd, GWL_EXSTYLE)
            want = (ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT
                    | WS_EX_LAYERED | WS_EX_TOPMOST) & ~WS_EX_APPWINDOW
            if want != ex:
                put(hwnd, GWL_EXSTYLE, want)
        except Exception:
            pass
