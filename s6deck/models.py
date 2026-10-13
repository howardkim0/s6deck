"""Per-model device specs. Add a DeviceModel entry to support another device."""
from __future__ import annotations

from dataclasses import dataclass, field

VENDOR_USAGE_PAGE = 0xFFA0


@dataclass(frozen=True)
class KnobCodes:
    rotate_left: int
    rotate_right: int
    press: int


@dataclass(frozen=True)
class DeviceModel:
    name: str
    vendor_id: int
    product_id: int
    packet_size: int
    columns: int
    rows: int
    key_image_size: tuple[int, int]
    image_rotation: int  # degrees clockwise applied before upload
    image_format: str = "JPEG"
    # Input code of each screen key, in row-major position order. Also used as the
    # key number in image/clear commands.
    key_codes: tuple[int, ...] = ()
    # Input codes of screenless round buttons, in physical left-to-right order.
    button_codes: tuple[int, ...] = ()
    # Knobs in physical left-to-right order.
    knobs: tuple[KnobCodes, ...] = field(default=())
    knob_names: tuple[str, ...] = ()

    @property
    def key_count(self) -> int:
        return self.columns * self.rows

    def key_number(self, position: int) -> int:
        return self.key_codes[position] if self.key_codes else position + 1


STREONOR_S6 = DeviceModel(
    name="Streonor S6",
    vendor_id=0x1500,
    product_id=0x3006,
    packet_size=1024,
    columns=3,
    rows=2,
    key_image_size=(64, 64),
    image_rotation=90,
    key_codes=(0x01, 0x02, 0x03, 0x04, 0x05, 0x06),
    button_codes=(0x25, 0x30, 0x31),
    knobs=(
        KnobCodes(rotate_left=0x90, rotate_right=0x91, press=0x33),
        KnobCodes(rotate_left=0x60, rotate_right=0x61, press=0x34),
        KnobCodes(rotate_left=0x50, rotate_right=0x51, press=0x35),
    ),
    knob_names=("Small left knob", "Small right knob", "Big knob"),
)

MODELS: tuple[DeviceModel, ...] = (STREONOR_S6,)


def find_model(vendor_id: int, product_id: int) -> DeviceModel | None:
    return next((m for m in MODELS if (m.vendor_id, m.product_id) == (vendor_id, product_id)), None)
