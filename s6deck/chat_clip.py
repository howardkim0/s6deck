"""Chat clip that: keep the last minute of Discord's audio in memory, save the last N seconds on demand.

While any binding uses the "chat_clip" action, a background thread captures the audio Discord
plays (other people's voices, Discord's own sounds), and nothing else: no other apps, no
soundboard clips, not your microphone. Audio stays in a RAM ring buffer and is only written to
disk, as a WAV in CLIPS_DIR, when the action runs. The saved file can then be bound to a
"Play sound" key.

Windows only (10 2004+): it uses the per-process loopback capture of the Core Audio API
through ctypes, so it works whichever output device Discord plays on.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BUFFER_SECONDS = 60
DEFAULT_CLIP_SECONDS = 10
CLIP_SECONDS_CHOICES = ("10", "15", "20", "30", "60")
CLIPS_DIR = Path.home() / "Music" / "s6deck clips"
DISCORD_PROCESS_NAMES = ("discord.exe", "discordptb.exe", "discordcanary.exe")
FIND_DISCORD_INTERVAL_SECONDS = 3.0
# Process loopback delivers nothing while Discord is silent; gaps longer than this are filled
# with silence so the buffer stays in step with the wall clock.
GAP_TOLERANCE_SECONDS = 0.1


class ClipBuffer:
    """Ring buffer of the most recent int16 stereo frames."""

    def __init__(self, seconds: int = BUFFER_SECONDS, rate: int = SAMPLE_RATE):
        import numpy as np

        self._frames = np.zeros((seconds * rate, CHANNELS), dtype=np.int16)
        self._write = 0      # next frame index to write
        self._filled = 0     # valid frames, up to capacity
        self._lock = threading.Lock()

    def append(self, frames) -> None:
        capacity = len(self._frames)
        frames = frames[-capacity:]
        with self._lock:
            end = self._write + len(frames)
            if end <= capacity:
                self._frames[self._write:end] = frames
            else:
                split = capacity - self._write
                self._frames[self._write:] = frames[:split]
                self._frames[:end - capacity] = frames[split:]
            self._write = end % capacity
            self._filled = min(capacity, self._filled + len(frames))

    def append_silence(self, count: int) -> None:
        import numpy as np

        self.append(np.zeros((min(count, len(self._frames)), CHANNELS), dtype=np.int16))

    def last(self, count: int):
        """The most recent `count` frames (fewer if not that many were captured), oldest first."""
        import numpy as np

        with self._lock:
            count = min(count, self._filled)
            start = self._write - count
            if start >= 0:
                return self._frames[start:self._write].copy()
            return np.concatenate([self._frames[start:], self._frames[:self._write]])

    def clear(self) -> None:
        with self._lock:
            self._write = self._filled = 0


class ChatClipRecorder:
    def __init__(self) -> None:
        self._buffer: ClipBuffer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._capturing_pid: int | None = None
        self._listener: Callable[[str], None] = lambda message: None

    # -- public -----------------------------------------------------------

    def set_listener(self, listener: Callable[[str], None]) -> None:
        """listener(message) is told about saved clips and failures (on the caller's thread)."""
        self._listener = listener

    def set_enabled(self, enabled: bool) -> None:
        """Start/stop listening to Discord. A no-op off Windows (clip() explains why)."""
        if sys.platform != "win32" or enabled == (self._thread is not None):
            return
        if enabled:
            self._buffer = ClipBuffer()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="s6deck-chat-clip", daemon=True)
            self._thread.start()
            log.info("chat clip: listening for Discord audio")
        else:
            self._stop.set()
            self._thread.join(timeout=2)
            self._thread = None
            self._buffer = None   # drop the recorded audio
            log.info("chat clip: stopped")

    def clip(self, seconds: int = DEFAULT_CLIP_SECONDS) -> Path:
        """Save the last `seconds` of Discord audio as a WAV and return its path."""
        import soundfile

        if sys.platform != "win32":
            raise RuntimeError("Chat clip works on Windows only for now")
        buffer = self._buffer
        frames = buffer.last(max(1, min(seconds, BUFFER_SECONDS)) * SAMPLE_RATE) if buffer else None
        if frames is None or not frames.size:
            raise RuntimeError("Chat clip: Discord isn't running" if self._capturing_pid is None
                               else "Chat clip: no Discord audio captured yet")
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        stem = f"Chat clip {datetime.now():%Y-%m-%d %H.%M.%S}"
        path = CLIPS_DIR / f"{stem}.wav"
        number = 2
        while path.exists():  # two clips within the same second
            path = CLIPS_DIR / f"{stem} ({number}).wav"
            number += 1
        soundfile.write(str(path), frames, SAMPLE_RATE, subtype="PCM_16")
        log.info("chat clip: saved %.1f s to %s", len(frames) / SAMPLE_RATE, path)
        return path

    def run_action(self, value: str) -> None:
        """The "chat_clip" action: value is the clip length in seconds."""
        try:
            seconds = int(value) if value.strip().isdigit() else DEFAULT_CLIP_SECONDS
            path = self.clip(seconds)
        except RuntimeError as error:
            self._listener(str(error))
            return
        self._listener(f"Clipped {path.name} (in {CLIPS_DIR})")

    def close(self) -> None:
        self.set_enabled(False)

    # -- capture thread ---------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            pid = _find_discord_pid()
            if pid is None:
                self._stop.wait(FIND_DISCORD_INTERVAL_SECONDS)
                continue
            self._capturing_pid = pid
            log.info("chat clip: capturing Discord (pid %d)", pid)
            try:
                _capture_process_audio(pid, self._buffer, self._stop)
            except OSError as error:
                log.warning("chat clip: capture failed: %s", error)
                self._stop.wait(FIND_DISCORD_INTERVAL_SECONDS)
            finally:
                self._capturing_pid = None
                self._buffer.clear()  # don't hand out audio from a Discord session that ended


# -- Windows ----------------------------------------------------------------

def _find_discord_pid() -> int | None:
    """The top Discord process: capturing its process tree includes the child that plays audio."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return None
    processes = {}  # pid -> (name, parent pid)
    try:
        entry = ProcessEntry(dwSize=ctypes.sizeof(ProcessEntry))
        more = kernel32.Process32FirstW(wintypes.HANDLE(snapshot), ctypes.byref(entry))
        while more:
            processes[entry.th32ProcessID] = (entry.szExeFile.lower(), entry.th32ParentProcessID)
            more = kernel32.Process32NextW(wintypes.HANDLE(snapshot), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return _top_process(processes, DISCORD_PROCESS_NAMES)


def _top_process(processes: dict[int, tuple[str, int]], names: tuple[str, ...]) -> int | None:
    """A process named one of `names` whose parent isn't also one of them."""
    for pid, (name, parent) in processes.items():
        if name in names and processes.get(parent, ("", 0))[0] not in names:
            return pid
    return None


def _capture_process_audio(pid: int, buffer: ClipBuffer, stop: threading.Event) -> None:
    """Append process `pid`'s (and its children's) audio to `buffer` until it exits or `stop` is set."""
    import ctypes
    from ctypes import wintypes

    import numpy as np

    ole32 = ctypes.WinDLL("ole32")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mmdevapi = ctypes.WinDLL("mmdevapi")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class ActivationParams(ctypes.Structure):  # AUDIOCLIENT_ACTIVATION_PARAMS
        _fields_ = [("ActivationType", ctypes.c_int), ("TargetProcessId", wintypes.DWORD),
                    ("ProcessLoopbackMode", ctypes.c_int)]

    class PropVariantBlob(ctypes.Structure):  # PROPVARIANT holding a VT_BLOB
        _fields_ = [("vt", wintypes.WORD), ("reserved", wintypes.WORD * 3),
                    ("cbSize", wintypes.ULONG), ("pBlobData", ctypes.c_void_p)]

    class WaveFormat(ctypes.Structure):  # WAVEFORMATEX
        _pack_ = 1
        _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                    ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                    ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                    ("cbSize", wintypes.WORD)]

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
            vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(interface)  # IUnknown::Release

    iid_audio_client = guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    iid_capture_client = guid("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    agile_iids = {bytes(guid(text)) for text in (
        "{00000000-0000-0000-C000-000000000046}",   # IUnknown
        "{41D949AB-9862-444A-80F6-C261334DA5EB}",   # IActivateAudioInterfaceCompletionHandler
        "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}",   # IAgileObject: the handler must be agile
    )}

    # A minimal COM object implementing IActivateAudioInterfaceCompletionHandler.
    activated = threading.Event()
    query_interface = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID),
                                         ctypes.POINTER(ctypes.c_void_p))
    add_ref_release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
    activate_completed = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

    def on_query_interface(this, riid, out):
        if bytes(riid.contents) in agile_iids:
            out[0] = this
            return 0
        out[0] = None
        return ctypes.c_long(0x80004002).value  # E_NOINTERFACE

    def on_activate_completed(_this, _operation):
        activated.set()
        return 0

    vtable = (ctypes.c_void_p * 5)()
    callbacks = (query_interface(on_query_interface), add_ref_release(lambda _this: 1),
                 add_ref_release(lambda _this: 1), activate_completed(on_activate_completed))
    for index, callback in enumerate(callbacks):
        vtable[index] = ctypes.cast(callback, ctypes.c_void_p)
    handler = ctypes.c_void_p(ctypes.addressof(vtable))  # the object: a pointer to its vtable
    # Windows may still call the handler after a timed-out activation; never free it.
    _handler_keepalive.append((vtable, callbacks, handler))

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    mmdevapi.ActivateAudioInterfaceAsync.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID), ctypes.c_void_p,
                                                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    mmdevapi.ActivateAudioInterfaceAsync.restype = ctypes.HRESULT
    initialized = ole32.CoInitializeEx(None, 0x0) in (0, 1)  # COINIT_MULTITHREADED

    operation, unknown, client, capture = (ctypes.c_void_p() for _ in range(4))
    event = process = None
    try:
        # INCLUDE_TARGET_PROCESS_TREE (mode 0) of AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK (1).
        params = ActivationParams(1, pid, 0)
        blob = PropVariantBlob(vt=65, cbSize=ctypes.sizeof(params),  # VT_BLOB
                               pBlobData=ctypes.cast(ctypes.byref(params), ctypes.c_void_p))
        mmdevapi.ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ctypes.byref(iid_audio_client),
                                             ctypes.byref(blob), ctypes.byref(handler), ctypes.byref(operation))
        if not activated.wait(5):
            raise OSError("audio capture activation timed out")
        result = ctypes.c_long()
        call(operation, 3, ctypes.byref(result), ctypes.byref(unknown),
             argtypes=(ctypes.c_void_p, ctypes.c_void_p))  # GetActivateResult
        if result.value < 0:
            raise OSError(f"audio capture activation failed (0x{result.value & 0xFFFFFFFF:08X})")
        call(unknown, 0, ctypes.byref(iid_audio_client), ctypes.byref(client),
             argtypes=(ctypes.c_void_p, ctypes.c_void_p))  # QueryInterface(IAudioClient)

        # Process loopback has no mix format to query; it converts to whatever we ask for.
        block = CHANNELS * 2
        wave_format = WaveFormat(1, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * block, block, 16, 0)  # 16-bit PCM
        loopback, event_callback, auto_convert = 0x20000, 0x40000, 0x80000000
        call(client, 3, 0, loopback | event_callback | auto_convert, 2_000_000, 0, ctypes.byref(wave_format), None,
             argtypes=(ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                       ctypes.c_void_p, ctypes.c_void_p))  # Initialize (200 ms buffer)
        event = kernel32.CreateEventW(None, False, False, None)
        call(client, 13, event, argtypes=(wintypes.HANDLE,))  # SetEventHandle
        call(client, 14, ctypes.byref(iid_capture_client), ctypes.byref(capture),
             argtypes=(ctypes.c_void_p, ctypes.c_void_p))  # GetService(IAudioCaptureClient)
        process = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        call(client, 10)  # Start

        started = time.monotonic()
        written = 0
        packet = wintypes.UINT()
        data, frames, flags = ctypes.c_void_p(), wintypes.UINT(), wintypes.DWORD()
        while not stop.is_set():
            kernel32.WaitForSingleObject(event, 100)
            while True:
                call(capture, 5, ctypes.byref(packet), argtypes=(ctypes.c_void_p,))  # GetNextPacketSize
                if not packet.value:
                    break
                call(capture, 3, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None,
                     argtypes=(ctypes.c_void_p,) * 5)  # GetBuffer
                count = frames.value
                if flags.value & 0x2 or not data:  # AUDCLNT_BUFFERFLAGS_SILENT
                    buffer.append_silence(count)
                else:
                    raw = ctypes.string_at(data, count * block)
                    buffer.append(np.frombuffer(raw, dtype=np.int16).reshape(-1, CHANNELS))
                written += count
                call(capture, 4, count, argtypes=(wintypes.UINT,))  # ReleaseBuffer
            behind = round((time.monotonic() - started) * SAMPLE_RATE) - written
            if behind > GAP_TOLERANCE_SECONDS * SAMPLE_RATE:
                buffer.append_silence(behind)
                written += behind
            if process and kernel32.WaitForSingleObject(process, 0) == 0:
                log.info("chat clip: Discord exited")
                return
        call(client, 11)  # Stop
    finally:
        for interface in (capture, client, unknown, operation):
            release(interface)
        for handle in (event, process):
            if handle:
                kernel32.CloseHandle(handle)
        if initialized:
            ole32.CoUninitialize()


_handler_keepalive: list = []
_recorder: ChatClipRecorder | None = None


def recorder() -> ChatClipRecorder:
    global _recorder
    if _recorder is None:
        _recorder = ChatClipRecorder()
    return _recorder
