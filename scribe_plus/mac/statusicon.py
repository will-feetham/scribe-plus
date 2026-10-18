"""
pystray's Mac menu bar icon, drawn sharp on Retina screens and as a
"template" image when idle, so macOS tints it to match a light or dark
menu bar like its own icons. While recording, thinking or showing an error
it keeps its colour.
"""
from __future__ import annotations

import io

import AppKit
import Foundation
from PIL import Image
from pystray import _darwin

ICON_PT = 18  # drawn size inside the ~22 pt menu bar


class MacIcon(_darwin.Icon):
    def _assert_image(self):
        side = int(round(ICON_PT * 2))  # 2x pixels for Retina
        source = self._icon.resize((side, side), Image.LANCZOS)
        buf = io.BytesIO()
        source.save(buf, "png")
        image = AppKit.NSImage.alloc().initWithData_(Foundation.NSData(buf.getvalue()))
        image.setSize_((ICON_PT, ICON_PT))
        image.setTemplate_(bool(self._icon.info.get("template")))
        self._icon_image = image
        self._status_item.button().setImage_(image)
