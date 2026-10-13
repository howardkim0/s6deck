"""Clickable picture of the device: screen keys, round buttons and knobs."""
from __future__ import annotations

import io

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..config import Page
from ..controls import Gesture, button_binding_id, key_binding_id, knob_binding_id
from ..models import DeviceModel
from ..render import render_binding

KEY_PREVIEW_SIZE = 96
SLOT_HEIGHT = 40
FLASH_MILLISECONDS = 180

IDLE_BORDER = "#555555"
SELECTED_BORDER = "#4da3ff"
FLASH_BORDER = "#ffd54d"
TEXT_COLOR = "#ffffff"


def _slot_style(border: str) -> str:
    # Every state uses the same border width and padding so highlighting never resizes a slot.
    return (f"border: 3px solid {border}; border-radius: 8px; padding: 4px; "
            f"background: #2b2b2b; color: {TEXT_COLOR};")


def pil_to_pixmap(image) -> QPixmap:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


class DeckView(QWidget):
    """Emits binding_selected(binding_id) when a control slot is clicked."""

    binding_selected = Signal(str)

    def __init__(self, model: DeviceModel, parent: QWidget | None = None):
        super().__init__(parent)
        self._model = model
        self._slots: dict[str, QWidget] = {}
        self._flashing: set[str] = set()
        self._flash_timers: dict[str, QTimer] = {}
        self._selected = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._build_keys())
        layout.addWidget(self._build_buttons())
        layout.addWidget(self._build_knobs())
        layout.addStretch(1)

    # -- construction -----------------------------------------------------

    def _build_keys(self) -> QWidget:
        box = QGroupBox("Screen keys")
        grid = QGridLayout(box)
        for position in range(self._model.key_count):
            button = QToolButton()
            button.setFixedSize(KEY_PREVIEW_SIZE + 14, KEY_PREVIEW_SIZE + 14)
            button.setIconSize(QSize(KEY_PREVIEW_SIZE, KEY_PREVIEW_SIZE))
            button.setToolTip(f"Key {position + 1}")
            self._register(key_binding_id(position), button)
            grid.addWidget(button, position // self._model.columns, position % self._model.columns)
        return box

    def _build_buttons(self) -> QWidget:
        box = QGroupBox("Buttons")
        row = QHBoxLayout(box)
        for index in range(len(self._model.button_codes)):
            button = QPushButton(f"Button {index + 1}")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setFixedHeight(SLOT_HEIGHT)
            self._register(button_binding_id(index), button)
            row.addWidget(button)
        return box

    def _build_knobs(self) -> QWidget:
        box = QGroupBox("Knobs")
        row = QHBoxLayout(box)
        for index, _ in enumerate(self._model.knobs):
            name = self._model.knob_names[index] if index < len(self._model.knob_names) else f"Knob {index + 1}"
            column = QVBoxLayout()
            label = QLabel(name)
            label.setAlignment(Qt.AlignCenter)
            column.addWidget(label)
            gestures = QHBoxLayout()
            for gesture, text in ((Gesture.ROTATE_LEFT, "⟲ Left"), (Gesture.PRESS, "● Press"),
                                  (Gesture.ROTATE_RIGHT, "Right ⟳")):
                slot = QPushButton(text)
                slot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                slot.setFixedHeight(SLOT_HEIGHT)
                self._register(knob_binding_id(index, gesture), slot)
                gestures.addWidget(slot)
            column.addLayout(gestures)
            row.addLayout(column)
        return box

    def _register(self, binding_id: str, widget) -> None:
        widget.clicked.connect(lambda _checked=False, b=binding_id: self.select(b))
        self._slots[binding_id] = widget
        self._restyle(binding_id)

    # -- state ------------------------------------------------------------

    def set_page(self, page: Page) -> None:
        for position in range(self._model.key_count):
            binding_id = key_binding_id(position)
            image = render_binding(page.bindings.get(binding_id))
            image = image.resize((KEY_PREVIEW_SIZE, KEY_PREVIEW_SIZE))
            self._slots[binding_id].setIcon(pil_to_pixmap(image))
        for binding_id, widget in self._slots.items():
            if binding_id.startswith("key:"):
                continue
            binding = page.bindings.get(binding_id)
            assigned = bool(binding and binding.action.type != "none")
            widget.setToolTip(f"{binding.action.type}: {binding.action.value}" if assigned else "Unassigned")

    def select(self, binding_id: str) -> None:
        previous, self._selected = self._selected, binding_id
        if previous in self._slots:
            self._restyle(previous)
        self._restyle(binding_id)
        self.binding_selected.emit(binding_id)

    def selected(self) -> str:
        return self._selected

    def flash(self, binding_id: str) -> None:
        """Briefly highlight a slot when its physical control is used."""
        if binding_id not in self._slots:
            return
        if binding_id not in self._flashing:
            self._flashing.add(binding_id)
            self._restyle(binding_id)
        # One restartable timer per slot keeps the highlight steady while a knob keeps turning.
        timer = self._flash_timers.get(binding_id)
        if timer is None:
            timer = QTimer(self, singleShot=True, interval=FLASH_MILLISECONDS)
            timer.timeout.connect(lambda b=binding_id: self._end_flash(b))
            self._flash_timers[binding_id] = timer
        timer.start()

    def _end_flash(self, binding_id: str) -> None:
        self._flashing.discard(binding_id)
        self._restyle(binding_id)

    def _restyle(self, binding_id: str) -> None:
        if binding_id in self._flashing:
            border = FLASH_BORDER
        elif binding_id == self._selected:
            border = SELECTED_BORDER
        else:
            border = IDLE_BORDER
        self._slots[binding_id].setStyleSheet(_slot_style(border))
