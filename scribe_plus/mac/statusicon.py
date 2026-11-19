"""
pystray's Mac menu bar icon, drawn sharp on Retina screens and as a
"template" image when idle, so macOS tints it to match a light or dark
menu bar like its own icons. While recording, thinking or showing an error
it keeps its colour.

It can also pulse: set_pulse() shows a list of pictures in a loop, which is
how the icon breathes red while a call is being recorded.
"""
from __future__ import annotations

import io

import AppKit
import Foundation
from PIL import Image
from pystray import _darwin

ICON_PT = 18  # drawn size inside the ~22 pt menu bar


class _PulseTicker(Foundation.NSObject):
    def tick_(self, timer):
        icon = getattr(self, "sp_icon", None)
        if icon is not None:
            icon._pulse_tick()


class MacIcon(_darwin.Icon):
    _pulse_timer = None
    _pulse_ticker = None
    _pulse_images: tuple = ()
    _pulse_at = 0

    @staticmethod
    def _ns_image(icon: Image.Image):
        side = int(round(ICON_PT * 2))  # 2x pixels for Retina
        source = icon.resize((side, side), Image.LANCZOS)
        buf = io.BytesIO()
        source.save(buf, "png")
        image = AppKit.NSImage.alloc().initWithData_(Foundation.NSData(buf.getvalue()))
        image.setSize_((ICON_PT, ICON_PT))
        image.setTemplate_(bool(icon.info.get("template")))
        return image

    def _assert_image(self):
        self._icon_image = self._ns_image(self._icon)
        if not self._pulse_images:      # while pulsing, the pulse owns what is on screen
            self._status_item.button().setImage_(self._icon_image)

    # -- pulsing (main thread only) ---------------------------------------------

    @property
    def pulsing(self) -> bool:
        return bool(self._pulse_images)

    def set_pulse(self, frames: list | None, period_s: float = 1.6) -> None:
        """Show `frames` (PIL images) in a loop, one loop every `period_s`
        seconds. None or an empty list stops and puts the normal icon back."""
        if self._pulse_timer is not None:
            self._pulse_timer.invalidate()
            self._pulse_timer = None
        if not frames:
            self._pulse_images = ()
            if getattr(self, "_icon_image", None) is not None:
                self._status_item.button().setImage_(self._icon_image)
            return
        made: dict[int, object] = {}      # the fade down and the fade up share pictures
        for frame in frames:
            if id(frame) not in made:
                made[id(frame)] = self._ns_image(frame)
        self._pulse_images = tuple(made[id(frame)] for frame in frames)
        self._pulse_at = 0
        self._status_item.button().setImage_(self._pulse_images[0])
        if self._pulse_ticker is None:
            self._pulse_ticker = _PulseTicker.alloc().init()
            self._pulse_ticker.sp_icon = self
        timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            period_s / len(frames), self._pulse_ticker, "tick:", None, True)
        # Common modes: it keeps pulsing while a menu is open.
        Foundation.NSRunLoop.mainRunLoop().addTimer_forMode_(timer, Foundation.NSRunLoopCommonModes)
        self._pulse_timer = timer

    def _pulse_tick(self) -> None:
        if not self._pulse_images:
            return
        self._pulse_at = (self._pulse_at + 1) % len(self._pulse_images)
        self._status_item.button().setImage_(self._pulse_images[self._pulse_at])
