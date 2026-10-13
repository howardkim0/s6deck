"""Device-independent control identifiers and input-event decoding."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import DeviceModel


class ControlKind(str, Enum):
    KEY = "key"
    BUTTON = "button"
    KNOB = "knob"


class Gesture(str, Enum):
    PRESS = "press"
    RELEASE = "release"
    ROTATE_LEFT = "left"
    ROTATE_RIGHT = "right"


@dataclass(frozen=True)
class ControlEvent:
    kind: ControlKind
    index: int
    gesture: Gesture

    @property
    def binding_id(self) -> str:
        """Config key of the binding this event triggers."""
        if self.kind is ControlKind.KNOB:
            return knob_binding_id(self.index, self.gesture)
        return f"{self.kind.value}:{self.index}"


def key_binding_id(position: int) -> str:
    return f"{ControlKind.KEY.value}:{position}"


def button_binding_id(index: int) -> str:
    return f"{ControlKind.BUTTON.value}:{index}"


def knob_binding_id(index: int, gesture: Gesture) -> str:
    return f"{ControlKind.KNOB.value}:{index}:{gesture.value}"


def decode(model: DeviceModel, code: int, state: int) -> ControlEvent | None:
    """Translate a raw (code, state) input report into a control event."""
    pressed = Gesture.PRESS if state else Gesture.RELEASE
    if code in model.key_codes:
        return ControlEvent(ControlKind.KEY, model.key_codes.index(code), pressed)
    if code in model.button_codes:
        return ControlEvent(ControlKind.BUTTON, model.button_codes.index(code), pressed)
    for index, knob in enumerate(model.knobs):
        if code == knob.rotate_left:
            return ControlEvent(ControlKind.KNOB, index, Gesture.ROTATE_LEFT)
        if code == knob.rotate_right:
            return ControlEvent(ControlKind.KNOB, index, Gesture.ROTATE_RIGHT)
        if code == knob.press:
            return ControlEvent(ControlKind.KNOB, index, pressed)
    return None
