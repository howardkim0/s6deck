"""Probe a MiraBox-family device: list HID interfaces, verify writes, log key reports.

Usage: .venv/bin/python tools/probe.py [seconds]
"""
import sys
import time

import hid

VENDOR_ID = 0x1500
PRODUCT_ID = 0x3006
VENDOR_USAGE_PAGE = 0xFFA0
PACKET_SIZE = 1024


def command(*parts: bytes) -> bytes:
    """Build a 'CRT\\0\\0' command padded to PACKET_SIZE, prefixed with report id 0."""
    payload = b"CRT\x00\x00" + b"".join(parts)
    return b"\x00" + payload.ljust(PACKET_SIZE, b"\x00")


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

    interfaces = hid.enumerate(VENDOR_ID, PRODUCT_ID)
    for info in interfaces:
        print(f"interface={info['interface_number']} usage_page=0x{info['usage_page']:04x} "
              f"usage=0x{info['usage']:02x} path={info['path']!r}")

    vendor = next(i for i in interfaces if i["usage_page"] == VENDOR_USAGE_PAGE)
    device = hid.device()
    device.open_path(vendor["path"])
    print("opened:", device.get_manufacturer_string(), device.get_product_string(),
          device.get_serial_number_string(), flush=True)

    device.write(command(b"DIS"))                 # wake screen
    for level in (10, 100, 50):                   # visible brightness change confirms writes
        device.write(command(b"LIG\x00\x00", bytes([level])))
        print("brightness", level, flush=True)
        time.sleep(0.7)

    print(f"listening {seconds:.0f}s - press each key/knob in order", flush=True)
    deadline = time.time() + seconds
    last_heartbeat = 0.0
    while time.time() < deadline:
        if time.time() - last_heartbeat > 10:
            device.write(command(b"CONNECT"))
            last_heartbeat = time.time()
        report = device.read(512, timeout_ms=200)
        if report:
            data = bytes(report)
            print(f"{time.time():.2f} len={len(data)} head={data[:16].hex(' ')} "
                  f"ascii={data[:12]!r}", flush=True)
    device.close()


if __name__ == "__main__":
    main()
