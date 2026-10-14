"""Look up the icon of an app (or file) a "launch" binding opens, per OS.

Returns a PIL image or None. Lookups are cached; callers may use any thread.
"""
from __future__ import annotations

import configparser
import io
import logging
import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

ICON_PIXELS = 256


@lru_cache(maxsize=64)
def app_icon(target: str) -> Image.Image | None:
    target = target.strip()
    if not target:
        return None
    try:
        if sys.platform == "darwin":
            return _macos_icon(target)
        if sys.platform == "win32":
            return _windows_icon(target)
        return _linux_icon(target)
    except Exception:
        log.debug("icon lookup failed for %r", target, exc_info=True)
        return None


# -- macOS ----------------------------------------------------------------

def _macos_icon(target: str) -> Image.Image | None:
    from AppKit import (NSBitmapImageFileTypePNG, NSBitmapImageRep, NSCompositingOperationCopy,
                        NSDeviceRGBColorSpace, NSGraphicsContext, NSWorkspace, NSZeroRect)

    workspace = NSWorkspace.sharedWorkspace()
    path = Path(target).expanduser()
    if path.exists():
        resolved = str(path)
    else:
        resolved = workspace.fullPathForApplication_(target)  # app name, e.g. "Safari"
        if not resolved:
            url = workspace.URLForApplicationWithBundleIdentifier_(target)  # e.g. "com.apple.Safari"
            resolved = url.path() if url else None
    if not resolved:
        return None

    icon = workspace.iconForFile_(resolved)
    bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, ICON_PIXELS, ICON_PIXELS, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(context)
        icon.drawInRect_fromRect_operation_fraction_(((0, 0), (ICON_PIXELS, ICON_PIXELS)), NSZeroRect,
                                                     NSCompositingOperationCopy, 1.0)
        context.flushGraphics()
    finally:
        NSGraphicsContext.restoreGraphicsState()
    png = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


# -- Windows --------------------------------------------------------------

@lru_cache(maxsize=1)
def _win32_api():
    """user32/gdi32 with declared signatures. Without them ctypes passes handles as 32-bit
    ints, and 64-bit handles above 2**31 raise OverflowError. Private WinDLL instances so the
    prototypes don't leak into other ctypes users (pynput shares ctypes.windll)."""
    import ctypes
    from ctypes import wintypes

    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    user32.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(wintypes.HICON), ctypes.c_void_p,
                                            wintypes.UINT, wintypes.UINT]
    user32.PrivateExtractIconsW.restype = wintypes.UINT
    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
    user32.GetIconInfo.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    return user32, gdi32


def _windows_icon(target: str) -> Image.Image | None:
    """Largest icon embedded in an .exe/.dll/.ico, found by path or on PATH."""
    import ctypes
    from ctypes import wintypes

    path = Path(target).expanduser()
    if not path.exists():
        found = shutil.which(target) or shutil.which(target + ".exe")
        if not found:
            return None
        path = Path(found)

    user32, gdi32 = _win32_api()
    icon = wintypes.HICON()
    extracted = user32.PrivateExtractIconsW(str(path), 0, ICON_PIXELS, ICON_PIXELS,
                                            ctypes.byref(icon), None, 1, 0)
    if extracted < 1 or not icon:
        return None
    try:
        class IconInfo(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                        ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

        class BitmapInfoHeader(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        info = IconInfo()
        if not user32.GetIconInfo(icon, ctypes.byref(info)):
            return None
        try:
            header = BitmapInfoHeader(biSize=ctypes.sizeof(BitmapInfoHeader), biWidth=ICON_PIXELS,
                                      biHeight=-ICON_PIXELS, biPlanes=1, biBitCount=32)  # top-down BGRA
            buffer = ctypes.create_string_buffer(ICON_PIXELS * ICON_PIXELS * 4)
            device_context = user32.GetDC(None)
            try:
                rows = gdi32.GetDIBits(device_context, info.hbmColor, 0, ICON_PIXELS, buffer,
                                       ctypes.byref(header), 0)
            finally:
                user32.ReleaseDC(None, device_context)
            if rows != ICON_PIXELS:
                return None
            image = Image.frombuffer("RGBA", (ICON_PIXELS, ICON_PIXELS), buffer.raw, "raw", "BGRA", 0, 1)
            if image.getchannel("A").getextrema() == (0, 0):  # legacy icon without alpha
                image.putalpha(255)
            return image.copy()
        finally:
            gdi32.DeleteObject(info.hbmColor)
            gdi32.DeleteObject(info.hbmMask)
    finally:
        user32.DestroyIcon(icon)


# -- Linux ----------------------------------------------------------------

_ICON_SIZES = ("512x512", "256x256", "192x192", "128x128", "96x96", "64x64", "48x48", "scalable")


def _data_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))]
    dirs += [Path(d) for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if d]
    return dirs


def _linux_icon(target: str) -> Image.Image | None:
    """Icon named by the app's .desktop entry, resolved through the hicolor theme/pixmaps."""
    path = Path(target).expanduser()
    if path.suffix in (".png", ".svg", ".xpm") and path.is_file():
        return _load_icon_file(path)
    icon_name = _desktop_icon_name(path.stem if path.exists() else target)
    if not icon_name:
        return None
    if Path(icon_name).is_absolute():
        return _load_icon_file(Path(icon_name))
    for data_dir in _data_dirs():
        for size in _ICON_SIZES:
            for suffix in (".png", ".svg"):
                candidate = data_dir / "icons" / "hicolor" / size / "apps" / f"{icon_name}{suffix}"
                if candidate.is_file():
                    return _load_icon_file(candidate)
        for suffix in (".png", ".svg", ".xpm"):
            candidate = data_dir / "pixmaps" / f"{icon_name}{suffix}"
            if candidate.is_file():
                return _load_icon_file(candidate)
    return None


def _desktop_icon_name(app: str) -> str | None:
    wanted = app.casefold()
    for data_dir in _data_dirs():
        applications = data_dir / "applications"
        if not applications.is_dir():
            continue
        for desktop_file in applications.glob("*.desktop"):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(desktop_file, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            names = {desktop_file.stem.casefold(), entry.get("Name", "").casefold(),
                     Path(entry.get("Exec", "").split(" ")[0]).name.casefold()}
            if wanted in names and entry.get("Icon"):
                return entry["Icon"]
    return None


def _load_icon_file(path: Path) -> Image.Image | None:
    if path.suffix == ".svg":
        return _render_svg(path)
    return Image.open(path).convert("RGBA")


def _render_svg(path: Path) -> Image.Image | None:
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        return None
    image = QImage(ICON_PIXELS, ICON_PIXELS, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA")
