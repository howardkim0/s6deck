"""The app's single font: Aileron Regular, embedded in Pillow (no font files to ship).

Key titles (Pillow) and the window (Qt) both use it, regular weight only.
"""
from __future__ import annotations

from functools import lru_cache

from PIL import ImageFont

FONT_FAMILY = "Aileron"


@lru_cache(maxsize=8)
def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def font_bytes() -> bytes:
    """Raw TrueType data of the font, for registering it with Qt."""
    return font(12).font_bytes


def install_qt_font(app) -> None:
    """Register the font with Qt and make it the application-wide default."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QFont, QFontDatabase

    font_id = QFontDatabase.addApplicationFontFromData(QByteArray(font_bytes()))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    if not families:
        return
    app_font = QFont(families[0], app.font().pointSize())
    app_font.setWeight(QFont.Normal)
    app_font.setItalic(False)
    app.setFont(app_font)
