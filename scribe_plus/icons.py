"""
Scribe Plus icons: five rounded level bars, echoing the on-screen pill.
Used for the Windows tray, the Mac menu bar, shortcuts and the README.

Drawn at 256 px and left to Windows to scale down (pystray packs the image
into a multi-size .ico), so edges stay smooth at the 16-32 px a tray icon
actually gets.

Idle bars are white on a dark taskbar and near-black on a light one; while
recording, thinking or showing an error they take the state's colour, so
the state reads at a glance even when the pill is off screen.

While a call is being recorded the bars are red and pulse: they fade down
and back about once every second and a half, for as long as the recording
runs. When the call's transcript is saved, the icon is a green check mark
for a few seconds.
"""
from __future__ import annotations

import math
import sys
from functools import lru_cache

from PIL import Image, ImageDraw

S = 256  # drawing canvas

STATE_COLORS = {
    "recording": (225, 29, 72),   # rose red
    "processing": (37, 99, 235),  # blue
    "error": (217, 119, 6),       # amber
    "paused": (148, 163, 184),    # light slate
    "call": (220, 38, 38),        # red: a call is being recorded
    "ready": (22, 163, 74),       # green: a call's transcript is ready
}
PULSE_PERIOD_S = 1.6   # one fade down and back
PULSE_LOW = 0.25       # how faint the bars get, as a share of full strength
PULSE_FRAMES = 20      # pictures per pulse (12.5 a second)
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


def _check(d: ImageDraw.ImageDraw, colour) -> None:
    """A filled disc with a white check mark: "done"."""
    d.ellipse((30, 30, 226, 226), fill=colour)
    pts = [(86, 132), (116, 162), (172, 98)]
    width = 26
    d.line(pts, fill=(255, 255, 255, 255), width=width, joint="curve")
    for x, y in pts:        # round ends and a round corner
        d.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=(255, 255, 255, 255))


def make_icon(state: str = "idle", dark: bool | None = None) -> Image.Image:
    """Tray icon for a state: idle, recording, processing, error, paused,
    call (a call is being recorded) or ready (its transcript is saved)."""
    return _draw(state, taskbar_is_dark() if dark is None else dark)


@lru_cache(maxsize=16)
def _draw(state: str, dark: bool) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ink = STATE_COLORS.get(state) or (LIGHT_INK if dark else DARK_INK)
    if state == "ready":
        _check(ImageDraw.Draw(img), ink + (255,))
    else:
        _bars(ImageDraw.Draw(img), ink + (255,), (14, 16, 242, 240), flat=state == "paused")
    return img


def app_icon(size: int = 256, margin: float = 8) -> Image.Image:
    """White bars on a dark rounded tile: for shortcuts, the Mac app and the
    README. `margin` is in 256ths of the canvas (macOS icons use about 25)."""
    k = size / S
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = margin * k
    d.rounded_rectangle((m, m, size - m, size - m), radius=(60 - margin + 8) * k * (240 / (256 - 2 * margin)),
                        fill=TILE + (255,))
    inner = (size - 2 * m) / (240 * k)
    cx, cy = size / 2, size / 2
    w, h = 152 * k * inner, 140 * k * inner
    _bars(d, LIGHT_INK + (255,), (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
    return img


def make_menubar_icon(state: str = "idle") -> Image.Image:
    """Mac menu bar: idle and paused are template images (macOS tints them
    to suit the menu bar); active states keep their colour."""
    img = _draw(state, False).copy()
    if state in ("idle", "paused"):
        img.info["template"] = True
    return img


def pulse_level(t: float, period: float = PULSE_PERIOD_S, low: float = PULSE_LOW) -> float:
    """How strong the pulsing icon is `t` seconds into a pulse: 1.0 at the
    start, easing down to `low` halfway through and back up."""
    phase = (t % period) / period
    return low + (1.0 - low) * (0.5 + 0.5 * math.cos(2.0 * math.pi * phase))


@lru_cache(maxsize=64)
def _faded(state: str, step: int, steps: int) -> Image.Image:
    img = _draw(state, False).copy()
    level = step / steps
    img.putalpha(img.getchannel("A").point(lambda a: int(round(a * level))))
    return img


def pulse_frames(state: str = "call", frames: int = PULSE_FRAMES) -> list[Image.Image]:
    """The pictures of one pulse, in order. Shown in a loop they make the
    icon breathe; the first one is the icon at full strength."""
    steps = 32      # distinct strengths (the fade down and the fade up share pictures)
    return [_faded(state, max(1, round(pulse_level(i * PULSE_PERIOD_S / frames) * steps)), steps)
            for i in range(frames)]

