"""The frontmost application, per OS. Returns None when it can't be determined."""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrontmostApp:
    name: str
    pid: int


def frontmost_app() -> FrontmostApp | None:
    try:
        if sys.platform == "darwin":
            return _macos()
        if sys.platform == "win32":
            return _windows()
        return _linux_x11()
    except Exception:
        log.debug("frontmost app lookup failed", exc_info=True)
        return None


def _macos() -> FrontmostApp | None:
    from AppKit import NSWorkspace  # pyobjc-framework-Cocoa

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    return FrontmostApp(name=str(app.localizedName()), pid=int(app.processIdentifier()))


def _windows() -> FrontmostApp | None:
    """Name is the executable without extension, e.g. "chrome"."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    window = user32.GetForegroundWindow()
    if not window:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid.value)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return FrontmostApp(name=Path(buffer.value).stem, pid=int(pid.value))
    finally:
        kernel32.CloseHandle(handle)


def _linux_x11() -> FrontmostApp | None:
    """WM_CLASS class name of the active window via xprop. Not available on pure Wayland."""
    active = _xprop("-root", "_NET_ACTIVE_WINDOW")
    window_id = active.split()[-1] if active else ""
    if not window_id.startswith("0x") or window_id == "0x0":
        return None
    # Output lines look like: WM_CLASS(STRING) = "instance", "Class"  /  _NET_WM_PID(CARDINAL) = 1234
    properties = _xprop("-id", window_id, "WM_CLASS", "_NET_WM_PID")
    name, pid = "", 0
    for line in properties.splitlines():
        value = line.split("=", 1)[-1].strip()
        if line.startswith("WM_CLASS"):
            name = value.split(",")[-1].strip().strip('"')
        elif line.startswith("_NET_WM_PID") and value.isdigit():
            pid = int(value)
    return FrontmostApp(name=name, pid=pid) if name else None


def _xprop(*args: str) -> str:
    result = subprocess.run(["xprop", *args], capture_output=True, text=True, timeout=1)
    return result.stdout.strip()
