"""
The floating status pill on Windows: frames from scribe_plus.pill_model,
drawn with tkinter.

It must never take keyboard focus, or the paste would land in the pill
instead of the user's app. On Windows the window gets WS_EX_NOACTIVATE,
WS_EX_TOOLWINDOW (no taskbar/Alt+Tab entry) and WS_EX_TRANSPARENT (clicks
pass through), and it is hidden by dropping its opacity to 0 rather than
withdrawing it, since re-showing a withdrawn Tk window can activate it.

All methods run on the Tk thread; other threads go through UiBridge.
"""
from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk

from scribe_plus.pill_model import PillAnimator

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
    """Tk window that shows PillAnimator frames, about 60 per second while visible."""

    FRAME_MS = 16

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.anim = PillAnimator(self.s, measure=self._text_width)
        self.h = self.anim.h
        self._running = False
        self._last = time.monotonic()
        self._area = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        self._geom = None
        # Negative size = pixels, so text scales with the pill on any DPI.
        self._font = ("Segoe UI" if WIN else "DejaVu Sans", -self.anim.font_px)

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

    @property
    def state(self) -> str:
        return self.anim.state

    # -- public, Tk thread only -------------------------------------------

    def set_state(self, state: str, **kw) -> None:
        appearing = self.anim.set_state(state, time.monotonic(), **kw)
        if state == "level":
            return
        if appearing:
            self._area = self._work_area()
            self._apply_win32_styles()
            self._raise()
        if self.anim.visible:
            self._ensure_running()

    # -- animation loop ------------------------------------------------------

    def _ensure_running(self) -> None:
        if not self._running:
            self._running = True
            self._last = time.monotonic()
            self.root.after(self.FRAME_MS, self._loop)

    def _loop(self) -> None:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        frame = self.anim.step(now, dt)
        if frame is None:
            self._set_alpha(0.0)
            self._running = False
            return
        self._apply_geometry(frame.width, frame.shake)
        self._set_alpha(frame.alpha)
        try:
            self._draw(frame)
        except tk.TclError:
            pass
        self.root.after(self.FRAME_MS, self._loop)

    # -- drawing ---------------------------------------------------------------

    def _text_width(self, text: str) -> int:
        try:
            item = self.canvas.create_text(0, -100, text=text, font=self._font, anchor="w")
            x1, _y1, x2, _y2 = self.canvas.bbox(item)
            self.canvas.delete(item)
            return x2 - x1
        except Exception:
            return int(0.55 * self.anim.font_px * len(text))

    def _draw(self, frame) -> None:
        c = self.canvas
        c.delete("all")
        for item in frame.items:
            kind = item[0]
            if kind == "capsule":
                _capsule(c, *item[1:5], fill=item[5], outline=item[6])
            elif kind == "dot":
                _k, cx, cy, r, fill = item
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline="")
            elif kind == "bar":
                _k, x, y1, y2, width, colour = item
                cap = width / 2  # round caps add this much at each end
                c.create_line(x, y1 + cap, x, max(y1 + cap, y2 - cap), fill=colour,
                              width=max(2, int(round(width))), capstyle="round")
            elif kind == "polyline":
                _k, pts, colour, width = item
                c.create_line(*pts, fill=colour, width=max(2, int(width)),
                              capstyle="round", joinstyle="round")
            elif kind == "text":
                _k, x, cy, text, colour, anchor = item
                c.create_text(x, cy, text=text, font=self._font, fill=colour, anchor=anchor)

    # -- window management ---------------------------------------------------

    def _apply_geometry(self, w: int, shake: int) -> None:
        left, _top, right, bottom = self._area
        x = left + (right - left - w) // 2 + shake
        y = bottom - self.h - int(28 * self.s)
        geom = (w, x, y)
        if geom != self._geom:
            self._geom = geom
            self.win.geometry(f"{w}x{self.h}+{x}+{y}")

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
