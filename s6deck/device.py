"""Connection to one physical device: writes, heartbeat, image upload, input reading."""
from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass

import hid
from PIL import Image

from . import protocol
from .models import MODELS, VENDOR_USAGE_PAGE, DeviceModel

HEARTBEAT_INTERVAL_SECONDS = 10.0  # vendor firmware drops to its logo without this
INPUT_REPORT_SIZE = 512


@dataclass(frozen=True)
class InputEvent:
    code: int      # device control code (key number for screen keys)
    state: int     # raw state byte from the report
    raw: bytes


def connected_devices() -> list[tuple[DeviceModel, dict]]:
    """All attached supported devices, as (model, hid enumeration entry) pairs."""
    found = []
    for model in MODELS:
        for info in hid.enumerate(model.vendor_id, model.product_id):
            if info["usage_page"] == VENDOR_USAGE_PAGE and info["usage"] == 0x01:
                found.append((model, info))
    return found


class Device:
    def __init__(self, model: DeviceModel, hid_info: dict):
        self.model = model
        self.serial = hid_info.get("serial_number") or ""
        self._hid = hid.device()
        self._hid.open_path(hid_info["path"])
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._write(protocol.wake_screen(model.packet_size))
        self._heartbeat_thread.start()

    # -- public API -------------------------------------------------------

    def set_brightness(self, percent: int) -> None:
        self._write(protocol.set_brightness(percent, self.model.packet_size))

    def screen_off(self) -> None:
        self._write(protocol.screen_off(self.model.packet_size))

    def wake(self) -> None:
        self._write(protocol.wake_screen(self.model.packet_size))

    def clear_all(self) -> None:
        self._write(protocol.clear_all_keys(self.model.packet_size))
        self.refresh()

    def clear_key(self, position: int) -> None:
        self._write(protocol.clear_key(self.model.key_number(position), self.model.packet_size))
        self.refresh()

    def set_key_image(self, position: int, image: Image.Image, refresh: bool = True) -> None:
        encoded = self._encode_key_image(image)
        size = self.model.packet_size
        reports = [protocol.image_header(self.model.key_number(position), len(encoded), size),
                   *protocol.image_chunks(encoded, size)]
        with self._write_lock:  # header and chunks must not interleave with heartbeats
            for report in reports:
                self._write_unlocked(report)
        if refresh:
            self.refresh()

    def refresh(self) -> None:
        self._write(protocol.refresh(self.model.packet_size))

    def read_event(self, timeout_ms: int = 200) -> InputEvent | None:
        report = self._hid.read(INPUT_REPORT_SIZE, timeout_ms=timeout_ms)
        if not report:
            return None
        raw = bytes(report)
        # Reports look like b"ACK\0\0OK\0\0" + code + state.
        return InputEvent(code=raw[9], state=raw[10], raw=raw)

    def close(self) -> None:
        self._stop.set()
        self._heartbeat_thread.join(timeout=2)
        self._hid.close()

    # -- internals --------------------------------------------------------

    def _encode_key_image(self, image: Image.Image) -> bytes:
        fitted = image.convert("RGB").resize(self.model.key_image_size, Image.LANCZOS)
        if self.model.image_rotation:
            fitted = fitted.rotate(-self.model.image_rotation, expand=True)  # PIL rotates counter-clockwise
        buffer = io.BytesIO()
        fitted.save(buffer, self.model.image_format, quality=95)
        return buffer.getvalue()

    def _write(self, report: bytes) -> None:
        with self._write_lock:
            self._write_unlocked(report)

    def _write_unlocked(self, report: bytes) -> None:
        if self._hid.write(report) < 0:
            raise OSError(f"HID write failed: {self._hid.error()}")

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                self._write(protocol.heartbeat(self.model.packet_size))
            except OSError:
                return
