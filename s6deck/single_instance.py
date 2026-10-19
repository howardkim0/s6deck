"""Run one s6deck at a time; launching it again shows the running one's window.

Two instances fight over the device and both forward the microphone into the virtual mic,
so call participants hear you twice. Windows: a named mutex marks the running instance and a
named event asks it to show its window. macOS: the app bundle is already single-instance;
for source runs and Linux, a lock file in the config directory (no window activation).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable

_MUTEX_NAME = "Local\\s6deck-instance"
_SHOW_EVENT_NAME = "Local\\s6deck-show-window"
_held = []  # handles/files kept open for the life of the process


def acquire(lock_dir: Path) -> bool:
    """True if this is the only running instance (and it now holds the lock)."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        already_running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        _held.append(mutex)
        return not already_running
    import fcntl

    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(lock_dir / "s6deck.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _held.append(handle)
    return True


def show_running_instance() -> None:
    """Ask the running instance to show its window (Windows; elsewhere a no-op)."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    event = kernel32.OpenEventW(0x0002, False, _SHOW_EVENT_NAME)  # EVENT_MODIFY_STATE
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)


def on_show_requested(callback: Callable[[], None]) -> None:
    """Call callback (on a background thread) whenever another launch asks to show the window."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    event = kernel32.CreateEventW(None, False, False, _SHOW_EVENT_NAME)  # auto-reset
    _held.append(event)

    def wait() -> None:
        infinite = 0xFFFFFFFF
        while kernel32.WaitForSingleObject(event, infinite) == 0:
            callback()

    threading.Thread(target=wait, name="s6deck-show-requests", daemon=True).start()
