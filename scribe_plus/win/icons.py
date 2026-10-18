"""
Scribe Plus icons: five rounded level bars, echoing the on-screen pill.

Drawn at 256 px and left to Windows to scale down (pystray packs the image
into a multi-size .ico), so edges stay smooth at the 16-32 px a tray icon
actually gets.

Idle bars are white on a dark taskbar and near-black on a light one; while
recording, thinking or showing an error they take the state's colour, so
the state reads at a glance even when the pill is off screen.
"""
from __future__ import annotations

import sys
from functools import lru_cache

from PIL import Image, ImageDraw

S = 256  # drawing canvas

STATE_COLORS = {
    "recording": (225, 29, 72),   # rose red
    "processing": (37, 99, 235),  # blue
    "error": (217, 119, 6),       # amber
    "paused": (148, 163, 184),    # light slate
}
LIGHT_INK = (245, 245, 247)
DARK_INK = (24, 24, 27)
TILE = (24, 24, 27)


def taskbar_is_dark() -> bool:
    """Windows taskbar theme (dark is the Windows 11 default)."""
    if sys.platform != "win32":
        return True
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return int(value) == 0
    except Exception:
        return True


def _bars(d: ImageDraw.ImageDraw, colour, box: tuple[float, float, float, float], flat: bool = False) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    bw = w * 34 / 228
    gap = (w - 5 * bw) / 4
    heights = [0.18] * 5 if flat else [0.43, 0.75, 1.0, 0.75, 0.43]
    mid = (y0 + y1) / 2
    x = x0
    for f in heights:
        bh = max(bw, h * f)
        d.rounded_rectangle((x, mid - bh / 2, x + bw, mid + bh / 2), radius=bw / 2, fill=colour)
        x += bw + gap


def make_icon(state: str = "idle", dark: bool | None = None) -> Image.Image:
    """Tray icon for a state: idle, recording, processing, error or paused."""
    return _draw(state, taskbar_is_dark() if dark is None else dark)


@lru_cache(maxsize=16)
def _draw(state: str, dark: bool) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ink = STATE_COLORS.get(state) or (LIGHT_INK if dark else DARK_INK)
    _bars(ImageDraw.Draw(img), ink + (255,), (14, 16, 242, 240), flat=state == "paused")
    return img


def app_icon() -> Image.Image:
    """White bars on a dark rounded tile: for shortcuts and the README."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), radius=60, fill=TILE + (255,))
    _bars(d, LIGHT_INK + (255,), (52, 58, 204, 198))
    return img
