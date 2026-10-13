"""MiraBox StreamDock HID protocol (the family Streonor devices belong to).

Every host->device message is one HID output report: report id 0x00 followed by a
fixed-size payload. Commands start with b"CRT\\0\\0"; image data follows a BAT
command as raw payload-sized chunks.
"""
from __future__ import annotations

COMMAND_PREFIX = b"CRT\x00\x00"
REPORT_ID = b"\x00"


def frame(payload: bytes, packet_size: int) -> bytes:
    """Wrap a payload as one output report: report id + payload padded to packet_size."""
    if len(payload) > packet_size:
        raise ValueError(f"payload of {len(payload)} bytes exceeds packet size {packet_size}")
    return REPORT_ID + payload.ljust(packet_size, b"\x00")


def command(body: bytes, packet_size: int) -> bytes:
    return frame(COMMAND_PREFIX + body, packet_size)


def wake_screen(packet_size: int) -> bytes:
    return command(b"DIS", packet_size)


def screen_off(packet_size: int) -> bytes:
    return command(b"HAN", packet_size)


def heartbeat(packet_size: int) -> bytes:
    return command(b"CONNECT", packet_size)


def refresh(packet_size: int) -> bytes:
    """Commit uploaded images to the display."""
    return command(b"STP", packet_size)


def set_brightness(percent: int, packet_size: int) -> bytes:
    if not 0 <= percent <= 100:
        raise ValueError("brightness must be 0-100")
    return command(b"LIG\x00\x00" + bytes([percent]), packet_size)


def clear_key(key_number: int, packet_size: int) -> bytes:
    return command(b"CLE\x00\x00\x00" + bytes([key_number]), packet_size)


def clear_all_keys(packet_size: int) -> bytes:
    return clear_key(0xFF, packet_size)


def image_header(key_number: int, image_size: int, packet_size: int) -> bytes:
    """BAT command announcing an image of image_size bytes for key_number."""
    if image_size > 0xFFFF:
        raise ValueError(f"image of {image_size} bytes exceeds the 64 KiB protocol limit")
    return command(b"BAT\x00\x00" + image_size.to_bytes(2, "big") + bytes([key_number]), packet_size)


def image_chunks(image: bytes, packet_size: int) -> list[bytes]:
    return [frame(image[offset:offset + packet_size], packet_size)
            for offset in range(0, len(image), packet_size)]
