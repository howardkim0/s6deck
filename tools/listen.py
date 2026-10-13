"""Hold the device open (with heartbeat), show numbered keys, and log raw input reports."""
import sys
import time

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, ".")
from s6deck.device import Device, connected_devices


def numbered(n: int) -> Image.Image:
    img = Image.new("RGB", (64, 64), (20, 60, 160))
    ImageDraw.Draw(img).text((20, 14), str(n), fill="white", font=ImageFont.load_default(size=32))
    return img


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 90
    model, info = connected_devices()[0]
    device = Device(model, info)
    device.set_brightness(80)
    for position in range(model.key_count):
        device.set_key_image(position, numbered(position + 1), refresh=False)
    device.refresh()
    print(f"{model.name}: listening {seconds:.0f}s", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        event = device.read_event()
        if event:
            print(f"{time.strftime('%H:%M:%S')} code=0x{event.code:02x} state=0x{event.state:02x} "
                  f"head={event.raw[:14].hex(' ')}", flush=True)
    device.close()


if __name__ == "__main__":
    main()
