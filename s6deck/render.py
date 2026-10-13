"""Compose key images (background color + icon + title)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

from .app_icons import app_icon
from .config import Binding
from .fonts import font

RENDER_SIZE = 192  # draw large, then let the device layer downscale for clean text
TITLE_FONT_SIZE = 34
APP_ICON_MARGIN = 6


def _title_font():
    return font(TITLE_FONT_SIZE)


@lru_cache(maxsize=128)
def _load_icon(path: str, mtime: float) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGBA")
    except OSError:
        return None


def _icon(path: str) -> Image.Image | None:
    file = Path(path).expanduser()
    if not path or not file.is_file():
        return None
    return _load_icon(str(file), file.stat().st_mtime)


def _render_app_icon(icon: Image.Image) -> Image.Image:
    """An app's own icon, alone and nearly full-size: no background color or title.
    Black behind it because the key screens can't show transparency."""
    canvas = Image.new("RGB", (RENDER_SIZE, RENDER_SIZE), (0, 0, 0))
    visible = icon.getchannel("A").getbbox()  # trim the transparent padding app icons ship with
    if visible:
        icon = icon.crop(visible)
    fitted = icon.copy()
    fitted.thumbnail((RENDER_SIZE - 2 * APP_ICON_MARGIN,) * 2, Image.LANCZOS)
    offset = ((RENDER_SIZE - fitted.width) // 2, (RENDER_SIZE - fitted.height) // 2)
    canvas.paste(fitted, offset, fitted)
    return canvas


def render_binding(binding: Binding | None) -> Image.Image:
    """Key image: a launch key without a custom icon shows only the app's icon; otherwise
    background color + optional icon + optional title."""
    if binding is not None and not binding.icon and binding.action.type == "launch":
        default_icon = app_icon(binding.action.value)
        if default_icon is not None:
            return _render_app_icon(default_icon)

    canvas = Image.new("RGB", (RENDER_SIZE, RENDER_SIZE), _parse_color(binding.color if binding else ""))
    if binding is None:
        return canvas

    icon = _icon(binding.icon)
    title = binding.title.strip()
    if icon is not None:
        icon_area = RENDER_SIZE - (TITLE_FONT_SIZE + 16 if title else 0)
        fitted = icon.copy()
        fitted.thumbnail((icon_area - 12, icon_area - 12), Image.LANCZOS)
        offset = ((RENDER_SIZE - fitted.width) // 2, (icon_area - fitted.height) // 2)
        canvas.paste(fitted, offset, fitted)

    if title:
        draw = ImageDraw.Draw(canvas)
        font = _title_font()
        lines = title.splitlines()[:3]
        line_height = TITLE_FONT_SIZE + 4
        y = RENDER_SIZE - 8 - line_height * len(lines) if icon is not None else \
            (RENDER_SIZE - line_height * len(lines)) // 2
        for line in lines:
            width = draw.textlength(line, font=font)
            draw.text(((RENDER_SIZE - width) / 2, y), line, fill="white", font=font,
                      stroke_width=2, stroke_fill="black")
            y += line_height
    return canvas


def _parse_color(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 6:
        try:
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            pass
    return (30, 30, 30)
