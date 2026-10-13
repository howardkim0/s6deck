"""Build app icons from s6deck/assets/mickey_source.png (Steamboat Willie-era Mickey).

Removes the flat background and the hand/arm, crops around the head and ears, and writes
(in s6deck/assets/): icon.png (1024, transparent), icon.ico, icon.icns (macOS only).
Run: .venv/bin/python tools/build_icons.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "s6deck" / "assets"
SOURCE = ASSETS / "mickey_source.png"

BACKGROUND = (240, 238, 239)    # flat backdrop color of the source image
BACKGROUND_TOLERANCE = 30       # summed RGB difference; pure white (255,255,255) is 48 away
INK_TOLERANCE = 600             # summed RGB difference from black that still counts as arm ink
# The arm merges into the chin outline along this vertical strip; cutting it there
# isolates the hand+arm as its own black shape.
ARM_CUT = (596, 1420, 600, 1640)  # x0, y0, x1, y1 in source pixels
HAND_SEED = (60, 1300)            # a point inside the hand
ARM_EDGE_GROWTH = 4               # px of anti-aliasing around the arm to remove too
POCKET_GRID = 20                  # spacing used to find enclosed background pockets
# Temporary fill color. Must differ from black by more than INK_TOLERANCE and from the
# backdrop by more than BACKGROUND_TOLERANCE (PIL skips a fill whose color is "already"
# within tolerance of the seed), and not occur in the grayscale source.
MARKER = (255, 255, 130)

ICON_SIZE = 1024
ICON_MARGIN = 48


def _marker_mask(image: Image.Image) -> Image.Image:
    """255 where the pixel is exactly MARKER, else 0."""
    difference = ImageChops.difference(image, Image.new("RGB", image.size, MARKER)).convert("L")
    return difference.point(lambda v: 255 if v == 0 else 0)


def _remove_hand_and_arm(image: Image.Image) -> None:
    ImageDraw.Draw(image).rectangle(ARM_CUT, fill=BACKGROUND)
    ImageDraw.floodfill(image, HAND_SEED, MARKER, thresh=INK_TOLERANCE)
    # Grow the removed area a few pixels to swallow the arm's anti-aliased edge, but only
    # left of the cut so the chin outline it touched stays intact.
    arm = _marker_mask(image).filter(ImageFilter.MaxFilter(ARM_EDGE_GROWTH * 2 + 1))
    ImageDraw.Draw(arm).rectangle((ARM_CUT[0], 0, image.width, image.height), fill=0)
    image.paste(BACKGROUND, mask=arm)


def _background_mask(image: Image.Image) -> Image.Image:
    """255 where the pixel is backdrop: reachable from the border, plus enclosed pockets."""
    work = image.copy()
    width, height = work.size
    seeds = [(x, y) for x in range(0, width, POCKET_GRID) for y in (0, height - 1)]
    seeds += [(x, y) for y in range(0, height, POCKET_GRID) for x in (0, width - 1)]
    # Enclosed pockets (e.g. between an ear and the head) only match the backdrop exactly.
    seeds += [(x, y) for x in range(0, width, POCKET_GRID) for y in range(0, height, POCKET_GRID)
              if work.getpixel((x, y)) == BACKGROUND]
    for seed in seeds:
        if work.getpixel(seed) != MARKER and _close(work.getpixel(seed), BACKGROUND, BACKGROUND_TOLERANCE):
            ImageDraw.floodfill(work, seed, MARKER, thresh=BACKGROUND_TOLERANCE)
    return _marker_mask(work)


def _close(color, target, tolerance: int) -> bool:
    return sum(abs(a - b) for a, b in zip(color, target)) <= tolerance


def _cut_out(image: Image.Image) -> Image.Image:
    """RGBA with the backdrop transparent. Anti-aliased ink at the outer edge becomes
    partially transparent black, so there's no light halo on dark backgrounds."""
    background = _background_mask(image)
    edge_band = ImageChops.subtract(background.filter(ImageFilter.MaxFilter(5)), background)
    luminance = image.convert("L")
    alpha = background.point(lambda v: 0 if v else 255)
    rgba = image.convert("RGBA")
    pixels, alpha_px, band_px, lum_px = rgba.load(), alpha.load(), edge_band.load(), luminance.load()
    for y in range(image.height):
        for x in range(image.width):
            if band_px[x, y] and lum_px[x, y] < BACKGROUND[0]:
                coverage = 1 - lum_px[x, y] / BACKGROUND[0]
                pixels[x, y] = (0, 0, 0, 255)
                alpha_px[x, y] = round(255 * coverage)
    rgba.putalpha(alpha)
    return rgba


def build_app_icon() -> Image.Image:
    source = Image.open(SOURCE).convert("RGB")
    _remove_hand_and_arm(source)
    head = _cut_out(source)
    head = head.crop(head.getchannel("A").getbbox())
    side = max(head.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(head, ((side - head.width) // 2, (side - head.height) // 2))
    body = ICON_SIZE - 2 * ICON_MARGIN
    icon = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    icon.alpha_composite(square.resize((body, body), Image.LANCZOS), (ICON_MARGIN, ICON_MARGIN))
    return icon


def write_icns(icon: Image.Image, destination: Path) -> None:
    if sys.platform != "darwin" or shutil.which("iconutil") is None:
        print("skipping icon.icns (needs macOS iconutil)")
        return
    with tempfile.TemporaryDirectory() as temp:
        iconset = Path(temp) / "icon.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            icon.resize((points, points), Image.LANCZOS).save(iconset / f"icon_{points}x{points}.png")
            icon.resize((points * 2, points * 2), Image.LANCZOS).save(iconset / f"icon_{points}x{points}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(destination)], check=True)


def main() -> None:
    icon = build_app_icon()
    icon.save(ASSETS / "icon.png")
    icon.save(ASSETS / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    write_icns(icon, ASSETS / "icon.icns")
    print("wrote icon.png, icon.ico, icon.icns")


if __name__ == "__main__":
    main()
