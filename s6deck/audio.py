"""Soundboard: play clips locally and into a virtual microphone for calls.

Video-call apps read one input device, so to send both your voice and clips, s6deck
forwards your real microphone into a virtual loopback device (BlackHole on macOS,
VB-CABLE on Windows, a PulseAudio/PipeWire null sink on Linux) and mixes clips in.
Pick that virtual device as the microphone in the call app.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
DEFAULT_VOLUME_PERCENT = 30  # meme clips are mastered loud; full scale is painful
# Clips louder than this (roughly speech level) are turned down to it, so one volume
# setting works for clips mastered at wildly different levels.
TARGET_RMS_DBFS = -20
# Enough buffering to ride out UI work (key rendering) without dropping mic audio,
# small enough that call participants don't notice the delay.
STREAM_LATENCY_SECONDS = 0.03
# Input devices that are loopbacks themselves; forwarding them into the virtual mic
# would feed it back into itself.
VIRTUAL_DEVICE_HINTS = ("blackhole", "cable", "loopback", "null", "monitor of", "camo")
DEFAULT_VIRTUAL_DEVICE = {"darwin": "BlackHole 2ch", "win32": "CABLE Input"}.get(sys.platform, "s6deck_mic")


def _normalization_gain(samples) -> float:
    """Gain that brings a clip down to TARGET_RMS_DBFS; never boosts quiet clips."""
    import numpy as np

    rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0
    if rms == 0.0:
        return 1.0
    return min(1.0, 10 ** (TARGET_RMS_DBFS / 20) / rms)


@lru_cache(maxsize=32)
def _load_clip(path: str, mtime: float):
    """Decode an audio file to float32 stereo at SAMPLE_RATE, loudness-normalized.

    Returns (samples, gain) where gain is the normalization already applied.
    """
    import numpy as np
    import soundfile

    data, rate = soundfile.read(path, dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    data = data[:, :2]
    if rate != SAMPLE_RATE:
        frames = round(len(data) * SAMPLE_RATE / rate)
        source_times = np.arange(len(data)) / rate
        target_times = np.arange(frames) / SAMPLE_RATE
        data = np.stack([np.interp(target_times, source_times, data[:, c]) for c in range(2)], axis=1)
    gain = _normalization_gain(data)
    return (data * gain).astype("float32"), gain


def _clip(path: str):
    file = Path(path).expanduser()
    if not file.is_file():
        raise FileNotFoundError(f"sound file not found: {file}")
    return _load_clip(str(file), file.stat().st_mtime)


class SoundBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playing: list[list] = []   # [samples, position] being mixed into the virtual mic
        self._local_players: list[subprocess.Popen] = []
        self._stream = None
        self._virtual_device = DEFAULT_VIRTUAL_DEVICE
        self._volume = DEFAULT_VOLUME_PERCENT / 100

    # -- public -----------------------------------------------------------

    def set_volume(self, percent: int) -> None:
        """Clip volume for both local playback and the virtual mic (your voice is unaffected)."""
        self._volume = max(0, min(100, percent)) / 100

    @property
    def virtual_mic_active(self) -> bool:
        return self._stream is not None

    def set_virtual_mic(self, enabled: bool, virtual_device: str = DEFAULT_VIRTUAL_DEVICE) -> str:
        """Start/stop forwarding mic + clips into the virtual device. Returns a status message."""
        self._virtual_device = virtual_device
        self._stop_stream()
        if not enabled:
            return ""
        try:
            return self._start_stream()
        except Exception as error:
            log.warning("virtual mic unavailable: %s", error)
            return f"Virtual mic unavailable: {error}"

    def play(self, path: str) -> None:
        samples, gain = _clip(path)
        clip = samples * self._volume
        self._play_locally(Path(path).expanduser(), clip, gain * self._volume)
        if self._stream is not None:
            with self._lock:
                self._playing.append([clip, 0])

    def stop_all(self) -> None:
        with self._lock:
            self._playing.clear()
            local, self._local_players = self._local_players, []
        for player in local:
            player.terminate()
        if sys.platform != "darwin":
            import sounddevice

            sounddevice.stop()

    def close(self) -> None:
        self._stop_stream()

    # -- internals --------------------------------------------------------

    def _play_locally(self, path: Path, scaled_clip, file_gain: float) -> None:
        """Play on the current default output (e.g. headphones).

        file_gain is the volume to apply when playing the original file directly.
        """
        if sys.platform == "darwin":
            # afplay always uses the current default output, so it follows headphone changes.
            player = subprocess.Popen(["afplay", "--volume", f"{file_gain:.3f}", str(path)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            with self._lock:
                self._local_players = [p for p in self._local_players if p.poll() is None] + [player]
            return
        import sounddevice

        sounddevice.play(scaled_clip, SAMPLE_RATE, blocking=False, device=_default_output_follower())

    def _start_stream(self) -> str:
        import numpy as np
        import sounddevice

        sounddevice._terminate()   # re-scan devices so newly installed/plugged ones appear
        sounddevice._initialize()
        devices = sounddevice.query_devices()
        output = _find_device(devices, self._virtual_device, want_output=True)
        if output is None:
            raise RuntimeError(f"“{self._virtual_device}” not found; install a virtual audio device")
        microphone = _pick_microphone(devices, sounddevice.default.device[0])
        if microphone is None:
            raise RuntimeError("no physical microphone found")

        def callback(indata, outdata, frames, _time, status):
            if status:
                log.debug("virtual mic stream status: %s", status)
            voice = indata[:, :1]
            outdata[:] = np.repeat(voice, 2, axis=1)
            with self._lock:
                still_playing = []
                for item in self._playing:
                    samples, position = item
                    chunk = samples[position:position + frames]
                    outdata[:len(chunk)] += chunk
                    item[1] = position + frames
                    if item[1] < len(samples):
                        still_playing.append(item)
                self._playing = still_playing
            np.clip(outdata, -1.0, 1.0, out=outdata)

        stream = sounddevice.Stream(device=(microphone, output), samplerate=SAMPLE_RATE,
                                    channels=(1, 2), dtype="float32", latency=STREAM_LATENCY_SECONDS,
                                    callback=callback)
        stream.start()
        self._stream = stream
        mic_name = devices[microphone]["name"]
        out_name = devices[output]["name"]
        log.info("virtual mic: %s + sounds -> %s", mic_name, out_name)
        return f"Virtual mic on: {mic_name} + sounds → {out_name}"

    def _stop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        with self._lock:
            self._playing.clear()


def _default_output_follower() -> int | None:
    """Output device that tracks the system default at play time, or None for PortAudio's default.

    PortAudio resolves its default device once, when it initializes, so after the default
    output changes (e.g. the audio output action switches to headphones) clips would keep
    going to the old device. On Windows, MME's Sound Mapper is routed by Windows to the
    current default each time a stream opens."""
    if sys.platform != "win32":
        return None
    import sounddevice

    for index, device in enumerate(sounddevice.query_devices()):
        if device["max_output_channels"] > 0 and device["name"].startswith("Microsoft Sound Mapper"):
            return index
    return None


def _find_device(devices, name: str, want_output: bool) -> int | None:
    wanted = name.casefold()
    for index, device in enumerate(devices):
        channels = device["max_output_channels"] if want_output else device["max_input_channels"]
        if channels > 0 and wanted in device["name"].casefold():
            return index
    return None


def _is_virtual(name: str) -> bool:
    return any(hint in name.casefold() for hint in VIRTUAL_DEVICE_HINTS)


def _pick_microphone(devices, default_input: int) -> int | None:
    """The default input, unless it's a virtual device; then the first physical input."""
    if default_input is not None and default_input >= 0 and not _is_virtual(devices[default_input]["name"]):
        return default_input
    for index, device in enumerate(devices):
        if device["max_input_channels"] > 0 and not _is_virtual(device["name"]):
            return index
    return None


_soundboard: SoundBoard | None = None


def soundboard() -> SoundBoard:
    global _soundboard
    if _soundboard is None:
        _soundboard = SoundBoard()
    return _soundboard
