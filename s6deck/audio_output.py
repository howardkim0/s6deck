"""Switch the system's default audio output device: cycle to the next or previous one.

Virtual devices (VB-CABLE, null sinks, ...) are skipped: switching to one silences your
speakers/headphones. Windows uses the Core Audio COM API (no extra packages); Linux uses
pactl. macOS is not supported yet.
"""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

AUDIO_OUTPUT_COMMANDS = ("next", "prev")
# Substrings (lowercase) of device names treated as virtual and skipped when cycling.
VIRTUAL_OUTPUT_HINTS = ("cable", "blackhole", "loopback", "null", "virtual", "steam streaming", "s6deck_mic")


def cycle_audio_output(command: str) -> None:
    if command not in AUDIO_OUTPUT_COMMANDS:
        raise ValueError(f"audio output command must be one of {', '.join(AUDIO_OUTPUT_COMMANDS)}")
    step = 1 if command == "next" else -1
    if sys.platform == "win32":
        _windows_cycle(step)
    elif sys.platform == "darwin":
        raise RuntimeError("switching the audio output is not supported on macOS yet")
    else:
        _linux_cycle(step)


def _next_device(devices: list[tuple[str, str]], current_id: str | None, step: int) -> tuple[str, str] | None:
    """devices: (id, name) pairs. Returns the device `step` places after the current one,
    skipping virtual devices; None if there is nothing to switch to."""
    real = [device for device in devices if not any(hint in device[1].lower() for hint in VIRTUAL_OUTPUT_HINTS)]
    if not real:
        return None
    ids = [device_id for device_id, _ in real]
    index = ids.index(current_id) if current_id in ids else (-1 if step > 0 else 0)
    candidate = real[(index + step) % len(real)]
    return None if candidate[0] == current_id else candidate


# -- Windows --------------------------------------------------------------

def _windows_cycle(step: int) -> None:
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.WinDLL("ole32")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class PropertyKey(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PropVariant(ctypes.Structure):
        _fields_ = [("vt", wintypes.WORD), ("reserved", wintypes.WORD * 3), ("pwszVal", ctypes.c_wchar_p),
                    ("padding", ctypes.c_void_p)]

    def guid(text: str) -> GUID:
        value = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value))
        return value

    def call(interface, index, *args, argtypes=()):
        """Call vtable method `index` of a COM interface pointer; raises OSError on failure."""
        vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        return prototype(vtable[index])(interface, *args)

    def release(interface):
        if interface:
            call(interface, 2)  # IUnknown::Release

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.PropVariantClear.argtypes = [ctypes.c_void_p]
    initialized = ole32.CoInitializeEx(None, 0x2) in (0, 1)  # S_OK / S_FALSE; else already set up
    clsctx_all = 0x17
    e_render, device_state_active = 0, 1
    friendly_name = PropertyKey(guid("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)

    enumerator, collection, policy = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")), None, clsctx_all,
                               ctypes.byref(guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")),
                               ctypes.byref(enumerator))
        if not enumerator:
            raise RuntimeError("Core Audio device enumerator unavailable")

        def device_id(device) -> str:
            text = ctypes.c_wchar_p()
            call(device, 5, ctypes.byref(text), argtypes=(ctypes.POINTER(ctypes.c_wchar_p),))  # GetId
            try:
                return text.value
            finally:
                ole32.CoTaskMemFree(text)

        def device_name(device) -> str:
            store, value = ctypes.c_void_p(), PropVariant()
            call(device, 4, 0, ctypes.byref(store), argtypes=(wintypes.DWORD, ctypes.c_void_p))  # OpenPropertyStore
            try:
                call(store, 5, ctypes.byref(friendly_name), ctypes.byref(value),
                     argtypes=(ctypes.c_void_p, ctypes.c_void_p))  # IPropertyStore::GetValue
                return (value.pwszVal or "") if value.vt == 31 else ""  # VT_LPWSTR
            finally:
                ole32.PropVariantClear(ctypes.byref(value))
                release(store)

        # IMMDeviceEnumerator::EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)
        call(enumerator, 3, e_render, device_state_active, ctypes.byref(collection),
             argtypes=(ctypes.c_int, wintypes.DWORD, ctypes.c_void_p))
        count = wintypes.UINT()
        call(collection, 3, ctypes.byref(count), argtypes=(ctypes.c_void_p,))  # GetCount
        devices = []
        for index in range(count.value):
            device = ctypes.c_void_p()
            call(collection, 4, index, ctypes.byref(device), argtypes=(wintypes.UINT, ctypes.c_void_p))  # Item
            try:
                devices.append((device_id(device), device_name(device)))
            finally:
                release(device)

        current_id = None
        default = ctypes.c_void_p()
        try:  # GetDefaultAudioEndpoint(eRender, eMultimedia); fails when there is no output at all
            call(enumerator, 4, e_render, 1, ctypes.byref(default), argtypes=(ctypes.c_int, ctypes.c_int, ctypes.c_void_p))
            current_id = device_id(default)
        except OSError:
            pass
        finally:
            release(default)

        target = _next_device(devices, current_id, step)
        if target is None:
            return
        # IPolicyConfig (undocumented, stable since Windows 7) is what Sound settings uses.
        ole32.CoCreateInstance(ctypes.byref(guid("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")), None, clsctx_all,
                               ctypes.byref(guid("{F8679F50-850A-41CF-9C72-430F290290C8}")), ctypes.byref(policy))
        if not policy:
            raise RuntimeError("audio policy interface unavailable")
        for role in (0, 1):  # eConsole, eMultimedia: what "Set as Default Device" changes
            call(policy, 13, target[0], role, argtypes=(ctypes.c_wchar_p, ctypes.c_int))  # SetDefaultEndpoint
        log.info("audio output: %s", target[1])
    finally:
        release(policy)
        release(collection)
        release(enumerator)
        if initialized:
            ole32.CoUninitialize()


# -- Linux ----------------------------------------------------------------

def _linux_cycle(step: int) -> None:
    def pactl(*args: str) -> str:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, check=True, timeout=5).stdout

    try:
        sinks = [line.split("\t")[1] for line in pactl("list", "short", "sinks").splitlines() if "\t" in line]
        current = pactl("get-default-sink").strip()
    except FileNotFoundError:
        raise RuntimeError("pactl not found (install pulseaudio-utils)") from None
    target = _next_device([(sink, sink) for sink in sinks], current, step)
    if target is not None:
        pactl("set-default-sink", target[0])
        log.info("audio output: %s", target[1])
