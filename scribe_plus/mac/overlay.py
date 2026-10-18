"""
The floating status pill on a Mac: frames from scribe_plus.pill_model,
drawn with AppKit.

The pill is a borderless, non-activating panel that ignores the mouse and
floats above other windows on every Space (full-screen apps included), so
it never takes keyboard focus from the app you're dictating into. It sits
just above the Dock on the screen the pointer is on.

All methods run on the main thread; other threads go through MainBridge.
"""
from __future__ import annotations

import time

from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSAttributedString, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

from scribe_plus.pill_model import PillAnimator

_ROUND = 1  # NSLineCapStyleRound / NSLineJoinStyleRound
_colours: dict[str, object] = {}


def _colour(hex_str: str):
    c = _colours.get(hex_str)
    if c is None:
        h = hex_str.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        c = NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)
        _colours[hex_str] = c
    return c


class _PillView(NSView):
    def isFlipped(self):  # top-left origin, like the frame coordinates
        return True

    def drawRect_(self, rect):
        frame = getattr(self, "sp_frame", None)
        font = getattr(self, "sp_font", None)
        if frame is None or font is None:
            return
        for item in frame.items:
            kind = item[0]
            if kind == "capsule":
                _k, x1, y1, x2, y2, fill, outline = item
                r = (y2 - y1) / 2
                path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x1, y1, x2 - x1, y2 - y1), r, r)
                _colour(fill).setFill()
                path.fill()
                _colour(outline).setStroke()
                path.setLineWidth_(1.0)
                path.stroke()
            elif kind == "dot":
                _k, cx, cy, r, fill = item
                _colour(fill).setFill()
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - r, cy - r, 2 * r, 2 * r)).fill()
            elif kind == "bar":
                _k, x, y1, y2, width, colour = item
                cap = width / 2
                path = NSBezierPath.bezierPath()
                path.moveToPoint_((x, y1 + cap))
                path.lineToPoint_((x, max(y1 + cap, y2 - cap)))
                path.setLineWidth_(width)
                path.setLineCapStyle_(_ROUND)
                _colour(colour).setStroke()
                path.stroke()
            elif kind == "polyline":
                _k, pts, colour, width = item
                path = NSBezierPath.bezierPath()
                path.moveToPoint_((pts[0], pts[1]))
                for i in range(2, len(pts), 2):
                    path.lineToPoint_((pts[i], pts[i + 1]))
                path.setLineWidth_(width)
                path.setLineCapStyle_(_ROUND)
                path.setLineJoinStyle_(_ROUND)
                _colour(colour).setStroke()
                path.stroke()
            elif kind == "text":
                _k, x, cy, text, colour, anchor = item
                s = NSAttributedString.alloc().initWithString_attributes_(
                    text, {NSFontAttributeName: font,
                           NSForegroundColorAttributeName: _colour(colour)})
                size = s.size()
                left = x - size.width / 2 if anchor == "center" else x
                s.drawAtPoint_((left, cy - size.height / 2))


class _Ticker(NSObject):
    def tick_(self, timer):
        pill = getattr(self, "sp_pill", None)
        if pill is not None:
            pill._tick()


class Pill:
    """AppKit panel that shows PillAnimator frames at about 60 per second."""

    def __init__(self) -> None:
        self.font = NSFont.systemFontOfSize_weight_(13.0, 0.0)
        self.anim = PillAnimator(1.0, measure=self._text_width)
        self.h = self.anim.h
        self._timer = None
        self.clock = time.monotonic  # replaceable, so the smoke test can step time
        self._last = self.clock()
        self._visible_frame = None
        self._geom = None

        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 60, self.h), style, NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorIgnoresCycle)
        panel.setAlphaValue_(0.0)
        view = _PillView.alloc().initWithFrame_(NSMakeRect(0, 0, 60, self.h))
        view.sp_font = self.font
        view.sp_frame = None
        panel.setContentView_(view)
        self.panel, self.view = panel, view
        self._ticker = _Ticker.alloc().init()
        self._ticker.sp_pill = self

    @property
    def state(self) -> str:
        return self.anim.state

    # -- public, main thread only --------------------------------------------

    def set_state(self, state: str, **kw) -> None:
        appearing = self.anim.set_state(state, self.clock(), **kw)
        if state == "level":
            return
        if appearing:
            self._visible_frame = self._screen_frame()
            self.panel.orderFrontRegardless()
        if self.anim.visible:
            self._ensure_running()

    def snapshot_png(self, path: str) -> bool:
        """Write the current pill to a PNG (used by the smoke test; no screen
        recording permission needed)."""
        bounds = self.view.bounds()
        rep = self.view.bitmapImageRepForCachingDisplayInRect_(bounds)
        if rep is None:
            return False
        self.view.cacheDisplayInRect_toBitmapImageRep_(bounds, rep)
        data = rep.representationUsingType_properties_(4, {})  # NSBitmapImageFileTypePNG
        return bool(data and data.writeToFile_atomically_(path, True))

    # -- animation loop --------------------------------------------------------

    def _ensure_running(self) -> None:
        if self._timer is None:
            self._last = self.clock()
            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / 60.0, self._ticker, "tick:", None, True)
            NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
            self._timer = timer

    def _stop(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _tick(self) -> None:
        now = self.clock()
        dt, self._last = now - self._last, now
        frame = self.anim.step(now, dt)
        if frame is None:
            self.panel.setAlphaValue_(0.0)
            self.panel.orderOut_(None)
            self._stop()
            return
        self._apply_geometry(frame.width, frame.shake)
        self.panel.setAlphaValue_(frame.alpha)
        self.view.sp_frame = frame
        self.view.setNeedsDisplay_(True)

    def step_once(self) -> None:
        """Advance one frame synchronously (smoke test)."""
        self._tick()
        self.view.displayIfNeeded()

    # -- layout --------------------------------------------------------------------

    def _text_width(self, text: str) -> float:
        s = NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: self.font})
        return float(s.size().width)

    def _screen_frame(self):
        mouse = NSEvent.mouseLocation()
        for screen in NSScreen.screens() or []:
            f = screen.frame()
            if (f.origin.x <= mouse.x <= f.origin.x + f.size.width
                    and f.origin.y <= mouse.y <= f.origin.y + f.size.height):
                return screen.visibleFrame()
        main = NSScreen.mainScreen()
        return main.visibleFrame() if main is not None else NSMakeRect(0, 0, 1440, 900)

    def _apply_geometry(self, w: int, shake: int) -> None:
        vf = self._visible_frame or self._screen_frame()
        x = vf.origin.x + (vf.size.width - w) / 2 + shake
        y = vf.origin.y + 28
        geom = (w, round(x), round(y))
        if geom != self._geom:
            self._geom = geom
            self.panel.setFrame_display_(NSMakeRect(geom[1], geom[2], w, self.h), False)
            self.view.setFrame_(NSMakeRect(0, 0, w, self.h))
