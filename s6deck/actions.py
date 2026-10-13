"""Host-side actions a binding can run. Page/profile/brightness actions live in the engine."""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

MODIFIER_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "win": "cmd", "super": "cmd", "meta": "cmd",
}
KEY_ALIASES = {
    "return": "enter", "escape": "esc", "del": "delete", "pgup": "page_up", "pgdn": "page_down",
    "volup": "media_volume_up", "voldown": "media_volume_down", "mute": "media_volume_mute",
    "playpause": "media_play_pause", "next": "media_next", "prev": "media_previous",
}


def parse_hotkey(spec: str) -> tuple[list[str], str]:
    """Split "cmd+shift+4" into (["cmd", "shift"], "4"). Raises ValueError if malformed."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *modifiers, key = parts
    unknown = [m for m in modifiers if m not in MODIFIER_ALIASES]
    if unknown:
        raise ValueError(f"unknown modifier(s): {', '.join(unknown)}")
    return [MODIFIER_ALIASES[m] for m in modifiers], KEY_ALIASES.get(key, key)


def send_hotkey(spec: str) -> None:
    # pynput is imported lazily: on Linux it needs a display server at import time.
    from pynput.keyboard import Controller, Key, KeyCode

    modifiers, key_name = parse_hotkey(spec)
    if len(key_name) == 1:
        key = KeyCode.from_char(key_name)
    elif hasattr(Key, key_name):
        key = getattr(Key, key_name)
    else:
        raise ValueError(f"unknown key: {key_name}")

    keyboard = Controller()
    pressed = [getattr(Key, m) for m in modifiers]
    for modifier in pressed:
        keyboard.press(modifier)
    try:
        keyboard.press(key)
        keyboard.release(key)
    finally:
        for modifier in reversed(pressed):
            keyboard.release(modifier)


def type_text(text: str) -> None:
    """Type `text` into the focused app, as if typed on the keyboard. "\\n" presses Enter."""
    if sys.platform == "darwin":
        _require_macos_accessibility()
    from pynput.keyboard import Controller

    Controller().type(text.replace("\\n", "\n"))


def launch(target: str) -> None:
    """Open an app by name or path, or any file/folder with its default handler."""
    path = Path(target).expanduser()
    if sys.platform == "darwin":
        args = ["open", str(path)] if path.exists() else ["open", "-a", target]
        _spawn(args)
    elif sys.platform == "win32":
        if path.exists():
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            _spawn(target, shell=True)
    else:
        if path.is_file() and os.access(path, os.X_OK):
            _spawn([str(path)])
        elif path.exists():
            _spawn(["xdg-open", str(path)])
        else:
            _spawn(shlex.split(target))


def open_url(url: str) -> None:
    webbrowser.open(url)


def run_shell(command: str) -> None:
    _spawn(command, shell=True)


VOLUME_STEP_PERCENT = 5  # Linux only; macOS and Windows use the system's own volume-key step
VOLUME_COMMANDS = ("up", "down", "mute")

# NX_KEYTYPE_SOUND_UP / SOUND_DOWN / MUTE from IOKit's ev_keymap.h.
_MACOS_MEDIA_KEYS = {"up": 0, "down": 1, "mute": 7}
# NX_KEYTYPE_BRIGHTNESS_UP / BRIGHTNESS_DOWN.
_MACOS_BRIGHTNESS_KEYS = {"up": 2, "down": 3}
_accessibility_prompted = False


class AccessibilityPermissionMissing(RuntimeError):
    pass


def _macos_accessibility_trusted(prompt: bool) -> bool:
    """Whether this process may post input events. With prompt=True, macOS shows its
    'grant Accessibility access' dialog if it isn't trusted yet."""
    import ctypes

    import objc
    from Foundation import NSDictionary

    services = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    options = NSDictionary.dictionaryWithObject_forKey_(prompt, "AXTrustedCheckOptionPrompt")
    return services.AXIsProcessTrustedWithOptions(objc.pyobjc_id(options))


def _require_macos_accessibility() -> None:
    global _accessibility_prompted
    prompt = not _accessibility_prompted  # ask once per run; repeated dialogs would be noise
    _accessibility_prompted = True
    if not _macos_accessibility_trusted(prompt):
        raise AccessibilityPermissionMissing(
            "s6deck needs Accessibility permission (System Settings → Privacy & Security → "
            "Accessibility) to type text and send volume and brightness keys")


def _post_macos_media_key(key_type: int) -> None:
    """Press and release a media key the way the keyboard's volume keys do."""
    import Quartz
    from AppKit import NSEvent, NSEventTypeSystemDefined

    system_defined_aux_control_buttons = 8
    for key_state in (0xA, 0xB):  # key down, key up
        event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            NSEventTypeSystemDefined, (0, 0), key_state << 8, 0, 0, None,
            system_defined_aux_control_buttons, (key_type << 16) | (key_state << 8), -1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event.CGEvent())


_WINDOWS_VOLUME_KEYS = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}  # VK_VOLUME_UP / DOWN / MUTE
_LINUX_VOLUME_COMMANDS = (  # first tool found on PATH wins
    ("wpctl", {"up": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", f"{VOLUME_STEP_PERCENT}%+"],
               "down": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{VOLUME_STEP_PERCENT}%-"],
               "mute": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"]}),
    ("pactl", {"up": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"+{VOLUME_STEP_PERCENT}%"],
               "down": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"-{VOLUME_STEP_PERCENT}%"],
               "mute": ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]}),
    ("amixer", {"up": ["amixer", "-q", "sset", "Master", f"{VOLUME_STEP_PERCENT}%+"],
                "down": ["amixer", "-q", "sset", "Master", f"{VOLUME_STEP_PERCENT}%-"],
                "mute": ["amixer", "-q", "sset", "Master", "toggle"]}),
)


def change_volume(command: str) -> None:
    """System output volume: "up"/"down" one step, or "mute" to toggle mute."""
    if command not in VOLUME_COMMANDS:
        raise ValueError(f"volume command must be one of {', '.join(VOLUME_COMMANDS)}")
    if sys.platform == "darwin":
        _require_macos_accessibility()
        _post_macos_media_key(_MACOS_MEDIA_KEYS[command])  # same as the keyboard keys: shows the overlay
    elif sys.platform == "win32":
        import ctypes

        key = _WINDOWS_VOLUME_KEYS[command]
        keyeventf_keyup = 0x2
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(key, 0, keyeventf_keyup, 0)
    else:
        import shutil

        for tool, commands in _LINUX_VOLUME_COMMANDS:
            if shutil.which(tool):
                _spawn(commands[command])
                return
        raise RuntimeError("no volume tool found (install wireplumber, pulseaudio-utils or alsa-utils)")


def _spawn(args, shell: bool = False) -> None:
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "shell": shell}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


SYSTEM_BRIGHTNESS_COMMANDS = ("up", "down")
SYSTEM_BRIGHTNESS_STEP_PERCENT = 10  # Windows/Linux; macOS uses its own brightness-key step


def change_system_brightness(command: str) -> None:
    """Brightness of the built-in display and external monitors: "up" or "down" one step.

    macOS: sends the keyboard brightness keys (built-in display, with the system overlay);
    external monitors follow when an app such as MonitorControl handles those keys.
    Windows: WMI for the laptop panel, DDC/CI for external monitors.
    Linux: brightnessctl for the laptop panel, ddcutil for external monitors.
    """
    if command not in SYSTEM_BRIGHTNESS_COMMANDS:
        raise ValueError(f"system brightness command must be one of {', '.join(SYSTEM_BRIGHTNESS_COMMANDS)}")
    if sys.platform == "darwin":
        _require_macos_accessibility()
        _post_macos_media_key(_MACOS_BRIGHTNESS_KEYS[command])
    elif sys.platform == "win32":
        _windows_change_brightness(command)
    else:
        _linux_change_brightness(command)


def _windows_change_brightness(command: str) -> None:
    step = SYSTEM_BRIGHTNESS_STEP_PERCENT if command == "up" else -SYSTEM_BRIGHTNESS_STEP_PERCENT
    # Laptop panel (no-op on desktops without WMI brightness support).
    script = ("$b = (Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
              "-ErrorAction SilentlyContinue | Select-Object -First 1).CurrentBrightness; "
              "if ($b -ne $null) { $m = Get-CimInstance -Namespace root/WMI "
              "-ClassName WmiMonitorBrightnessMethods | Select-Object -First 1; "
              "Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
              f"-Arguments @{{Timeout=0; Brightness=[math]::Max(0,[math]::Min(100,$b+({step})))}} }}")
    _spawn(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    _windows_ddc_change_brightness(step)


def _windows_ddc_change_brightness(step_percent: int) -> None:
    """External monitors over DDC/CI via Dxva2."""
    import ctypes
    from ctypes import wintypes

    class PhysicalMonitor(ctypes.Structure):
        _fields_ = [("handle", wintypes.HANDLE), ("description", wintypes.WCHAR * 128)]

    user32, dxva2 = ctypes.windll.user32, ctypes.windll.dxva2
    monitor_enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                           ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def adjust(hmonitor, _hdc, _rect, _data):
        count = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)) or not count.value:
            return True
        monitors = (PhysicalMonitor * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, monitors):
            return True
        try:
            for monitor in monitors:
                low, current, high = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
                if dxva2.GetMonitorBrightness(monitor.handle, ctypes.byref(low), ctypes.byref(current),
                                              ctypes.byref(high)):
                    span = high.value - low.value
                    target = current.value + round(span * step_percent / 100)
                    dxva2.SetMonitorBrightness(monitor.handle, max(low.value, min(high.value, target)))
        finally:
            dxva2.DestroyPhysicalMonitors(count.value, monitors)
        return True

    user32.EnumDisplayMonitors(None, None, monitor_enum_proc(adjust), 0)


def _linux_change_brightness(command: str) -> None:
    import shutil

    sign = "+" if command == "up" else "-"
    step = SYSTEM_BRIGHTNESS_STEP_PERCENT
    found = False
    if shutil.which("brightnessctl"):  # laptop backlight
        _spawn(["brightnessctl", "-q", "set", f"{step}%{sign}"])
        found = True
    if shutil.which("ddcutil"):  # external monitors; VCP 0x10 is brightness
        detected = subprocess.run(["ddcutil", "detect", "--terse"], capture_output=True, text=True, timeout=10)
        for line in detected.stdout.splitlines():
            if line.startswith("Display "):
                display = line.split()[1]
                _spawn(["ddcutil", "--display", display, "setvcp", "10", sign, str(step)])
                found = True
    if not found:
        raise RuntimeError("no brightness tool found (install brightnessctl and/or ddcutil)")


HOST_ACTIONS = {
    "hotkey": send_hotkey,
    "launch": launch,
    "url": open_url,
    "shell": run_shell,
    "volume": change_volume,
    "system_brightness": change_system_brightness,
    "type_text": type_text,
    "sound": lambda path: _soundboard().play(path),
    "stop_sounds": lambda _value: _soundboard().stop_all(),
}
# Actions that take no value.
VALUELESS_ACTIONS = ("stop_sounds",)


def _soundboard():
    from .audio import soundboard  # audio libraries load only when a sound is used

    return soundboard()


def run_host_action(action_type: str, value: str) -> None:
    handler = HOST_ACTIONS.get(action_type)
    if handler is None or (not value and action_type not in VALUELESS_ACTIONS):
        return
    log.debug("action %s(%r)", action_type, value)
    try:
        handler(value)
    except AccessibilityPermissionMissing as error:
        log.warning("%s", error)
    except Exception:  # an action failing must never take down the device loop
        log.exception("action %s(%r) failed", action_type, value)
