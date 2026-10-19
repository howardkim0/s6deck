"""Editor for the selected control's binding."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QToolButton, QWidget)

from ..actions import SYSTEM_BRIGHTNESS_COMMANDS, VOLUME_COMMANDS, parse_hotkey
from ..audio_output import AUDIO_OUTPUT_COMMANDS
from ..chat_clip import CLIP_SECONDS_CHOICES, CLIPS_DIR
from ..config import Action, Binding

PANEL_WIDTH = 300

ACTION_TYPES = (
    ("none", "Nothing"),
    ("hotkey", "Keyboard shortcut"),
    ("type_text", "Type text"),
    ("launch", "Open app / file"),
    ("url", "Open URL"),
    ("shell", "Run shell command"),
    ("page", "Switch page"),
    ("profile", "Switch profile"),
    ("volume", "System volume"),
    ("system_brightness", "System brightness"),
    ("audio_output", "Audio output device"),
    ("brightness", "Button brightness"),  # type id kept as "brightness" for existing configs
    ("sound", "Play sound"),
    ("stop_sounds", "Stop sounds"),
    ("chat_clip", "Chat clip that"),
)
# Action types whose value is picked from a list rather than typed.
CHOICE_ACTION_TYPES = ("page", "profile", "volume", "system_brightness", "audio_output", "brightness",
                       "chat_clip")
# Action types without a value field.
VALUELESS_ACTION_TYPES = ("none", "stop_sounds")
# Action types whose value is a file chosen with a Browse button, and its file filter.
BROWSE_FILTERS = {"launch": "", "sound": "Audio (*.mp3 *.wav *.m4a *.aac *.aiff *.flac *.ogg)"}
# Action types the Test button can run from the GUI (the rest act on the device).
TESTABLE_ACTION_TYPES = ("hotkey", "launch", "url", "shell", "volume", "system_brightness", "audio_output",
                         "sound", "stop_sounds", "chat_clip")

# Short placeholder inside the field, longer help shown under it.
VALUE_PLACEHOLDERS = {"hotkey": "cmd+shift+4", "type_text": "Yeet", "launch": "Safari", "url": "https://",
                      "shell": "command",
                      "sound": "Audio file"}
VALUE_HELP = {
    "type_text": "Typed into the app that has focus. \\n presses Enter.",
    "sound": "Plays on your output (e.g. headphones). With Virtual mic on, call apps using "
             "the virtual mic hear it too.",
    "stop_sounds": "Stops all sounds that are playing.",
    "chat_clip": "Saves the last N seconds of Discord audio (what others say) as a WAV in "
                 f"{CLIPS_DIR}, ready for a Play sound key. While this key exists, s6deck keeps "
                 "the last minute of Discord audio in memory only. Windows.",
    "hotkey": "Modifiers: cmd, ctrl, alt, shift. Keys: a–z, f1–f20, enter, space, "
              "playpause, volup, voldown, mute, next, prev",
    "launch": "App name, or path to an app or file. The key shows its icon unless you set one.",
    "shell": "Runs with the system shell",
    "page": "next, prev, or a page number",
    "volume": "up / down one step, or mute to toggle. Bind up and down to a knob's "
              "left and right. macOS shows its volume overlay when s6deck has "
              "Accessibility permission.",
    "system_brightness": "Laptop screen and external monitors, up / down one step. On macOS, "
                         "external monitors need MonitorControl (or similar) handling the "
                         "brightness keys.",
    "audio_output": "Switches the default output (speakers, headset, ...) to the next or previous "
                    "device. Bind next and prev to a knob's right and left. Virtual devices "
                    "(VB-CABLE, ...) are skipped. Windows and Linux.",
    "brightness": "Brightness of the deck's key screens: up / down by 10, or a level 0–100",
}
PAGE_CHOICES = ("next", "prev")
BRIGHTNESS_CHOICES = ("up", "down", "0", "25", "50", "75", "100")

SECTION_STYLE = "color: palette(placeholder-text); margin-top: 6px;"
HELP_STYLE = "color: palette(placeholder-text); font-size: 11px;"
ERROR_STYLE = "color: #e57373; font-size: 11px;"


def _section(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setStyleSheet(SECTION_STYLE)
    return label


class Inspector(QGroupBox):
    """Emits binding_changed(binding_id, Binding) on every edit; test_requested(Binding) on Test."""

    binding_changed = Signal(str, object)
    test_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("No control selected", parent)
        self.setFixedWidth(PANEL_WIDTH)
        self._binding_id = ""
        self._loading = False
        self._page_count = 1
        self._profile_names: list[str] = []

        self._title = QLineEdit()
        self._title.setPlaceholderText("Label (\\n = new line)")
        self._icon = QLineEdit()
        self._icon.setPlaceholderText("Image file")
        self._icon.setClearButtonEnabled(True)
        browse_icon = QToolButton(text="…", toolTip="Choose image")
        icon_row = QHBoxLayout()
        icon_row.setSpacing(4)
        icon_row.addWidget(self._icon, 1)
        icon_row.addWidget(browse_icon)
        self._color = QPushButton()
        self._color_value = "#1e1e1e"

        self._action_type = QComboBox()
        for value, label in ACTION_TYPES:
            self._action_type.addItem(label, value)
        self._value_text = QLineEdit()
        self._value_choice = QComboBox()
        self._value_choice.setEditable(True)
        self._browse_target = QToolButton(text="…", toolTip="Choose app or file")
        # A widget, not a bare layout: hiding a layout row hides its widgets one by one, and
        # the per-type setVisible calls below would show them again outside the layout.
        value_row = QWidget()
        value_layout = QHBoxLayout(value_row)
        value_layout.setContentsMargins(0, 0, 0, 0)
        value_layout.setSpacing(4)
        value_layout.addWidget(self._value_text, 1)
        value_layout.addWidget(self._value_choice, 1)
        value_layout.addWidget(self._browse_target)
        self._help = QLabel(wordWrap=True)
        self._help.setStyleSheet(HELP_STYLE)
        self._error = QLabel(wordWrap=True)
        self._error.setStyleSheet(ERROR_STYLE)
        self._test = QPushButton("Test")

        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(6)
        self._look_header = _section("Look")
        form.addRow(self._look_header)
        form.addRow("Title", self._title)
        form.addRow("Icon", icon_row)
        form.addRow("Color", self._color)
        form.addRow(_section("Action"))
        form.addRow("Type", self._action_type)
        form.addRow("Value", value_row)
        form.addRow("", self._help)
        form.addRow("", self._error)
        test_row = QHBoxLayout()
        test_row.addStretch(1)
        test_row.addWidget(self._test)
        form.addRow(test_row)
        self._form = form
        self._look_rows = (self._look_header, self._title, icon_row, self._color)
        self._value_row = value_row

        self._title.textEdited.connect(self._emit)
        self._icon.textChanged.connect(self._emit)  # also fires on the built-in clear button
        browse_icon.clicked.connect(self._browse_icon)
        self._color.clicked.connect(self._pick_color)
        self._action_type.currentIndexChanged.connect(self._on_action_type_changed)
        self._value_text.textEdited.connect(self._emit)
        self._value_choice.currentTextChanged.connect(self._emit)
        self._browse_target.clicked.connect(self._browse_launch_target)
        self._test.clicked.connect(lambda: self.test_requested.emit(self._current_binding()))
        self.setEnabled(False)
        self._set_row_visible(self._help, False)
        self._set_row_visible(self._error, False)

    # -- public -----------------------------------------------------------

    def set_context(self, page_count: int, profile_names: list[str]) -> None:
        self._page_count = page_count
        self._profile_names = profile_names

    def edit(self, binding_id: str, label: str, binding: Binding | None, has_screen: bool) -> None:
        self._loading = True
        self._binding_id = binding_id
        binding = binding or Binding()
        self.setEnabled(True)
        self.setTitle(label)
        for row in self._look_rows:
            self._set_row_visible(row, has_screen)
        self._title.setText(binding.title.replace("\n", "\\n"))
        self._icon.setText(binding.icon)
        self._set_color(binding.color)
        self._action_type.setCurrentIndex(max(0, self._action_type.findData(binding.action.type)))
        self._configure_value_widgets(binding.action.type, binding.action.value)
        self._loading = False
        self._validate()

    # -- internals --------------------------------------------------------

    def _set_row_visible(self, field, visible: bool) -> None:
        self._form.setRowVisible(field, visible)

    def _current_binding(self) -> Binding:
        action_type = self._action_type.currentData()
        if action_type in CHOICE_ACTION_TYPES:
            value = self._value_choice.currentText().strip()
        elif action_type == "type_text":
            value = self._value_text.text()  # spaces are part of what gets typed
        else:
            value = self._value_text.text().strip()
        return Binding(
            title=self._title.text().replace("\\n", "\n"),
            icon=self._icon.text().strip(),
            color=self._color_value,
            action=Action(type=action_type, value="" if action_type in VALUELESS_ACTION_TYPES else value),
        )

    def _emit(self, *_args) -> None:
        if self._loading or not self._binding_id:
            return
        self._validate()
        self.binding_changed.emit(self._binding_id, self._current_binding())

    def _on_action_type_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._configure_value_widgets(self._action_type.currentData(), "")
        self._emit()

    def _configure_value_widgets(self, action_type: str, value: str) -> None:
        uses_choice = action_type in CHOICE_ACTION_TYPES
        self._set_row_visible(self._value_row, action_type not in VALUELESS_ACTION_TYPES)
        self._value_text.setVisible(not uses_choice)
        self._value_choice.setVisible(uses_choice)
        self._value_choice.setEditable(action_type in ("page", "brightness", "chat_clip"))  # free-form numbers
        self._browse_target.setVisible(action_type in BROWSE_FILTERS)
        self._icon.setPlaceholderText("App's icon (default)" if action_type == "launch" else "Image file")
        self._test.setVisible(action_type in TESTABLE_ACTION_TYPES)
        self._value_text.setPlaceholderText(VALUE_PLACEHOLDERS.get(action_type, ""))
        help_text = VALUE_HELP.get(action_type, "")
        self._help.setText(help_text)
        self._set_row_visible(self._help, bool(help_text))
        if uses_choice:
            if action_type == "page":
                choices = [*PAGE_CHOICES, *(str(n) for n in range(1, self._page_count + 1))]
            elif action_type == "profile":
                choices = self._profile_names
            elif action_type == "volume":
                choices = list(VOLUME_COMMANDS)
            elif action_type == "system_brightness":
                choices = list(SYSTEM_BRIGHTNESS_COMMANDS)
            elif action_type == "audio_output":
                choices = list(AUDIO_OUTPUT_COMMANDS)
            elif action_type == "chat_clip":
                choices = list(CLIP_SECONDS_CHOICES)
            else:
                choices = list(BRIGHTNESS_CHOICES)
            was_loading, self._loading = self._loading, True
            self._value_choice.clear()
            self._value_choice.addItems(choices)
            self._value_choice.setCurrentText(value or (choices[0] if choices else ""))
            self._loading = was_loading
        else:
            self._value_text.setText(value)

    def _validate(self) -> None:
        binding = self._current_binding()
        message = ""
        if binding.action.type == "hotkey" and binding.action.value:
            try:
                parse_hotkey(binding.action.value)
            except ValueError as error:
                message = str(error)
        self._error.setText(message)
        self._set_row_visible(self._error, bool(message))

    def _set_color(self, value: str) -> None:
        self._color_value = value if QColor(value).isValid() else "#1e1e1e"
        self._color.setText(self._color_value)
        self._color.setStyleSheet(f"background: {self._color_value}; color: white; "
                                  "border-radius: 4px; padding: 3px;")

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._color_value), self, "Key background")
        if color.isValid():
            self._set_color(color.name())
            self._emit()

    def _browse_icon(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Key icon", "",
                                              "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)")
        if path:
            self._icon.setText(path)

    def _browse_launch_target(self) -> None:
        action_type = self._action_type.currentData()
        title = "Sound to play" if action_type == "sound" else "App or file to open"
        start = str(CLIPS_DIR) if action_type == "sound" and CLIPS_DIR.is_dir() else ""  # saved chat clips
        path, _ = QFileDialog.getOpenFileName(self, title, start, BROWSE_FILTERS.get(action_type, ""))
        if path:
            self._value_text.setText(path)
            self._emit()
