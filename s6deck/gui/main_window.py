"""Main window: profile/page management around the deck view and inspector."""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
                               QSpinBox, QSystemTrayIcon, QTabBar, QToolButton, QVBoxLayout,
                               QWidget)

from .. import config as config_store
from ..audio import DEFAULT_VIRTUAL_DEVICE, soundboard
from ..actions import run_host_action
from ..config import Binding, Config, Page, Profile
from ..controls import ControlEvent
from ..engine import Engine, EngineState
from ..models import STREONOR_S6, DeviceModel
from .deck_view import DeckView
from .inspector import TESTABLE_ACTION_TYPES, Inspector

SAVE_DELAY_MILLISECONDS = 400
ICON_SOURCE_URL = ("https://api.hub.jhu.edu/factory/sites/default/files/styles/soft_crop_2880/public/"
                   "2024-01/Mickey_Mouse.jpg")
DISCLAIMER_TITLE = "s6deck is an independent project."
DISCLAIMER_HTML = (
    "<p>It is not affiliated with, endorsed by, or sponsored by The Walt Disney Company.</p>"
    "<p>The app icon depicts Mickey Mouse as he appeared in Steamboat Willie (1928), "
    "which is in the public domain in the United States "
    f"(<a href='{ICON_SOURCE_URL}'>source</a>). Mickey Mouse is a trademark of Disney "
    "Enterprises, Inc.</p>"
    "<p>s6deck is also not affiliated with Streonor or MiraBox.</p>"
)


class EngineBridge(QObject):
    """Re-emits engine callbacks (engine thread) as Qt signals (delivered on the GUI thread)."""

    state_changed = Signal(object)
    control_used = Signal(object)
    brightness_changed = Signal(int)
    action_requested = Signal(str, str)


class MainWindow(QMainWindow):
    def __init__(self, config: Config, config_path: Path, model: DeviceModel = STREONOR_S6):
        super().__init__()
        self.setWindowTitle("s6deck")
        self._config = config
        self._config_path = config_path
        self._model = model
        self._profile_name = config.profiles[0].name
        self._page_index = 0
        self._quitting = False

        self._bridge = EngineBridge()
        self._engine = Engine(
            config,
            on_state=self._bridge.state_changed.emit,
            on_event=self._bridge.control_used.emit,
            on_brightness=self._bridge.brightness_changed.emit,
            run_action=self._bridge.action_requested.emit,
        )
        # Queued to the GUI thread: macOS keyboard APIs (hotkeys) must run on the main thread.
        self._bridge.action_requested.connect(run_host_action)
        self._save_timer = QTimer(self, singleShot=True, interval=SAVE_DELAY_MILLISECONDS)
        self._save_timer.timeout.connect(self._save_now)

        self._build_ui()
        self._build_tray()
        self._bridge.state_changed.connect(self._on_engine_state)
        self._bridge.control_used.connect(self._on_control_used)
        self._bridge.brightness_changed.connect(self._on_device_brightness)
        self._refresh_profiles()
        soundboard().set_volume(self._config.sound_volume)
        if self._config.virtual_mic:
            self._apply_virtual_mic()
        self._engine.start()

    # -- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(140)
        add_profile = QToolButton(text="+", toolTip="New profile")
        rename_profile = QToolButton(text="Rename", toolTip="Rename profile")
        delete_profile = QToolButton(text="−", toolTip="Delete profile")
        self._apps = QLineEdit()
        self._apps.setToolTip("Apps that activate this profile, comma separated. "
                              "The first profile is the default and has no app list.")
        self._follow = QCheckBox("Auto-switch")
        self._follow.setToolTip("Switch profile when the frontmost app matches a profile's apps")
        self._follow.setChecked(self._config.follow_frontmost_app)
        self._virtual_mic = QCheckBox("Virtual mic")
        self._virtual_mic.setToolTip(
            f"Send your microphone plus soundboard sounds to “{DEFAULT_VIRTUAL_DEVICE}”. "
            "In Google Meet/Zoom, choose that device as the microphone.")
        self._virtual_mic.setChecked(self._config.virtual_mic)
        self._sound_volume = QSpinBox()
        self._sound_volume.setRange(0, 100)
        self._sound_volume.setSuffix("%")
        self._sound_volume.setValue(self._config.sound_volume)
        self._sound_volume.setToolTip("Soundboard volume (your voice is unaffected)")

        top_row = QHBoxLayout()
        top_row.setSpacing(4)
        top_row.addWidget(QLabel("Profile"))
        top_row.addWidget(self._profile_combo)
        for button in (add_profile, rename_profile, delete_profile):
            top_row.addWidget(button)
        top_row.addSpacing(12)
        top_row.addWidget(QLabel("Apps"))
        top_row.addWidget(self._apps, 1)
        top_row.addWidget(self._follow)
        top_row.addSpacing(12)
        top_row.addWidget(self._virtual_mic)
        top_row.addWidget(QLabel("Sounds"))
        top_row.addWidget(self._sound_volume)

        self._page_tabs = QTabBar()
        self._page_tabs.setExpanding(False)
        self._page_tabs.setDrawBase(False)
        add_page = QToolButton(text="+", toolTip="Add page")
        remove_page = QToolButton(text="−", toolTip="Remove current page (double-click a tab to rename)")
        page_row = QHBoxLayout()
        page_row.setSpacing(4)
        page_row.addWidget(self._page_tabs)
        page_row.addWidget(add_page)
        page_row.addWidget(remove_page)
        page_row.addStretch(1)

        self._deck = DeckView(self._model)
        self._inspector = Inspector()
        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(self._deck, 0, Qt.AlignTop)
        body.addWidget(self._inspector, 0, Qt.AlignTop)

        root = QVBoxLayout()
        root.setContentsMargins(10, 8, 10, 4)
        root.setSpacing(6)
        root.addLayout(top_row)
        root.addLayout(page_row)
        root.addLayout(body, 1)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)
        self.statusBar().showMessage("Starting…")
        disclaimer = QToolButton(text="Disclaimer", toolTip="Legal notice")
        disclaimer.setAutoRaise(True)
        disclaimer.setStyleSheet("font-size: 11px; color: palette(placeholder-text);")
        disclaimer.clicked.connect(self._show_disclaimer)
        self.statusBar().addPermanentWidget(disclaimer)  # permanent widgets sit at the bottom right

        self._profile_combo.activated.connect(self._on_profile_chosen)
        add_profile.clicked.connect(self._add_profile)
        rename_profile.clicked.connect(self._rename_profile)
        delete_profile.clicked.connect(self._delete_profile)
        self._apps.editingFinished.connect(self._on_apps_edited)
        self._follow.toggled.connect(self._on_follow_toggled)
        self._virtual_mic.toggled.connect(self._on_virtual_mic_toggled)
        self._sound_volume.valueChanged.connect(self._on_sound_volume_changed)
        self._page_tabs.currentChanged.connect(self._on_page_tab)
        self._page_tabs.tabBarDoubleClicked.connect(self._rename_page)
        add_page.clicked.connect(self._add_page)
        remove_page.clicked.connect(self._remove_page)
        self._deck.binding_selected.connect(self._on_binding_selected)
        self._inspector.binding_changed.connect(self._on_binding_changed)
        self._inspector.test_requested.connect(self._test_binding)

    def _show_disclaimer(self) -> None:
        # A plain dialog rather than QMessageBox: macOS renders a message box's text bold,
        # and the app uses one regular-weight font only.
        dialog = QDialog(self)
        dialog.setWindowTitle("Disclaimer")
        text = QLabel(f"<p>{DISCLAIMER_TITLE}</p>{DISCLAIMER_HTML}")
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)  # clickable source link
        text.setFixedWidth(360)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(dialog.accept)
        layout = QVBoxLayout(dialog)
        layout.addWidget(text)
        layout.addWidget(buttons)
        dialog.exec()

    def _build_tray(self) -> None:
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = QApplication.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)
        self._tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        show = QAction("Show s6deck", menu)
        quit_action = QAction("Quit", menu)
        show.triggered.connect(self._show_window)
        quit_action.triggered.connect(self.quit)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.setToolTip("s6deck")
        self._tray.activated.connect(lambda reason: self._show_window()
                                     if reason == QSystemTrayIcon.Trigger else None)
        self._tray.show()

    # -- model helpers ----------------------------------------------------

    def _profile(self) -> Profile:
        return self._config.profile_named(self._profile_name) or self._config.profiles[0]

    def _page(self) -> Page:
        pages = self._profile().pages
        return pages[min(self._page_index, len(pages) - 1)]

    def _changed(self) -> None:
        """Config edited: push to the device right away, save to disk shortly after."""
        self._engine.update_config(self._config)
        self._save_timer.start()

    def _save_now(self) -> None:
        config_store.save(self._config, self._config_path)

    # -- refreshing widgets from the model --------------------------------

    def _refresh_profiles(self) -> None:
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        self._profile_combo.addItems([p.name for p in self._config.profiles])
        self._profile_combo.setCurrentText(self._profile_name)
        self._profile_combo.blockSignals(False)
        profile = self._profile()
        self._apps.setText(", ".join(profile.apps))
        self._apps.setEnabled(profile is not self._config.profiles[0])
        if profile is self._config.profiles[0]:
            self._apps.setPlaceholderText("Default profile")
        else:
            self._apps.setPlaceholderText("Safari, Code")
        self._refresh_pages()

    def _refresh_pages(self) -> None:
        self._page_tabs.blockSignals(True)
        while self._page_tabs.count():
            self._page_tabs.removeTab(0)
        for page in self._profile().pages:
            self._page_tabs.addTab(page.name)
        self._page_index = min(self._page_index, self._page_tabs.count() - 1)
        self._page_tabs.setCurrentIndex(self._page_index)
        self._page_tabs.blockSignals(False)
        self._refresh_page()

    def _refresh_page(self) -> None:
        self._deck.set_page(self._page())
        self._inspector.set_context(len(self._profile().pages), [p.name for p in self._config.profiles])
        if self._deck.selected():
            self._load_inspector(self._deck.selected())

    def _show_on_device(self) -> None:
        self._engine.show(self._profile_name, self._page_index)

    # -- profile handlers -------------------------------------------------

    def _on_profile_chosen(self, _index: int) -> None:
        self._profile_name = self._profile_combo.currentText()
        self._page_index = 0
        self._refresh_profiles()
        self._show_on_device()

    def _add_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "New profile", "Profile name:")
        name = name.strip()
        if not ok or not name:
            return
        if self._config.profile_named(name):
            QMessageBox.warning(self, "New profile", f"A profile named “{name}” already exists.")
            return
        self._config.profiles.append(Profile(name=name))
        self._profile_name, self._page_index = name, 0
        self._refresh_profiles()
        self._changed()
        self._show_on_device()

    def _rename_profile(self) -> None:
        profile = self._profile()
        name, ok = QInputDialog.getText(self, "Rename profile", "Profile name:", text=profile.name)
        name = name.strip()
        if not ok or not name or name == profile.name:
            return
        if self._config.profile_named(name):
            QMessageBox.warning(self, "Rename profile", f"A profile named “{name}” already exists.")
            return
        for page in (pg for p in self._config.profiles for pg in p.pages):
            for binding in page.bindings.values():
                if binding.action.type == "profile" and binding.action.value == profile.name:
                    binding.action.value = name
        profile.name = name
        self._profile_name = name
        self._refresh_profiles()
        self._changed()
        self._show_on_device()

    def _delete_profile(self) -> None:
        if len(self._config.profiles) == 1:
            QMessageBox.information(self, "Delete profile", "The last profile can't be deleted.")
            return
        profile = self._profile()
        answer = QMessageBox.question(self, "Delete profile", f"Delete profile “{profile.name}”?")
        if answer != QMessageBox.Yes:
            return
        self._config.profiles.remove(profile)
        self._profile_name, self._page_index = self._config.profiles[0].name, 0
        self._refresh_profiles()
        self._changed()
        self._show_on_device()

    def _on_apps_edited(self) -> None:
        apps = [a.strip() for a in self._apps.text().split(",") if a.strip()]
        if apps != self._profile().apps:
            self._profile().apps = apps
            self._changed()

    def _on_follow_toggled(self, checked: bool) -> None:
        self._config.follow_frontmost_app = checked
        self._changed()

    def _on_virtual_mic_toggled(self, checked: bool) -> None:
        self._config.virtual_mic = checked
        self._apply_virtual_mic()
        self._changed()

    def _apply_virtual_mic(self) -> None:
        message = soundboard().set_virtual_mic(self._config.virtual_mic)
        if message:
            self.statusBar().showMessage(message, 6000)
        if self._config.virtual_mic and not soundboard().virtual_mic_active:
            # Couldn't start (e.g. no virtual device); reflect that without saving it as off.
            self._virtual_mic.blockSignals(True)
            self._virtual_mic.setChecked(False)
            self._virtual_mic.blockSignals(False)

    def _on_sound_volume_changed(self, value: int) -> None:
        self._config.sound_volume = value
        soundboard().set_volume(value)
        self._changed()

    def _on_device_brightness(self, value: int) -> None:
        """Brightness changed from a device control; persist it."""
        self._config.brightness = value
        self._save_timer.start()

    # -- page handlers ----------------------------------------------------

    def _on_page_tab(self, index: int) -> None:
        if index < 0:
            return
        self._page_index = index
        self._refresh_page()
        self._show_on_device()

    def _add_page(self) -> None:
        pages = self._profile().pages
        pages.append(Page(name=f"Page {len(pages) + 1}"))
        self._page_index = len(pages) - 1
        self._refresh_pages()
        self._changed()
        self._show_on_device()

    def _remove_page(self) -> None:
        pages = self._profile().pages
        if len(pages) == 1:
            QMessageBox.information(self, "Remove page", "A profile needs at least one page.")
            return
        page = self._page()
        if page.bindings:
            answer = QMessageBox.question(self, "Remove page", f"Remove “{page.name}” and its bindings?")
            if answer != QMessageBox.Yes:
                return
        pages.remove(page)
        self._page_index = max(0, self._page_index - 1)
        self._refresh_pages()
        self._changed()
        self._show_on_device()

    def _rename_page(self, index: int) -> None:
        if index < 0:
            return
        page = self._profile().pages[index]
        name, ok = QInputDialog.getText(self, "Rename page", "Page name:", text=page.name)
        if ok and name.strip():
            page.name = name.strip()
            self._page_tabs.setTabText(index, page.name)
            self._changed()

    # -- binding handlers -------------------------------------------------

    def _on_binding_selected(self, binding_id: str) -> None:
        self._load_inspector(binding_id)

    def _load_inspector(self, binding_id: str) -> None:
        self._inspector.edit(binding_id, _label_for(binding_id, self._model),
                             self._page().bindings.get(binding_id), binding_id.startswith("key:"))

    def _on_binding_changed(self, binding_id: str, binding: Binding) -> None:
        bindings = self._page().bindings
        if binding.is_empty():
            bindings.pop(binding_id, None)
        else:
            bindings[binding_id] = binding
        self._deck.set_page(self._page())
        self._changed()

    def _test_binding(self, binding: Binding) -> None:
        if binding.action.type not in TESTABLE_ACTION_TYPES:
            self.statusBar().showMessage("Page, profile and button-brightness actions run from the device.", 4000)
            return
        run_host_action(binding.action.type, binding.action.value)

    # -- engine events ----------------------------------------------------

    def _on_engine_state(self, state: EngineState) -> None:
        if state.connected:
            status = f"Connected: {state.device_name}"
        else:
            status = state.message or "Waiting for device…"
        self.statusBar().showMessage(status)
        if (state.profile_name, state.page_index) != (self._profile_name, self._page_index):
            # Device-side switch (page key, profile action, frontmost app).
            self._profile_name, self._page_index = state.profile_name, state.page_index
            self._refresh_profiles()

    def _on_control_used(self, event: ControlEvent) -> None:
        self._deck.flash(event.binding_id)

    # -- window lifecycle -------------------------------------------------

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._tray is not None and not self._quitting:
            # Keep running in the tray so the device keeps working. No "still running"
            # notification: on macOS that would make the app request notification permission.
            event.ignore()
            self.hide()
            return
        self._shutdown()
        event.accept()

    def quit(self) -> None:
        self._quitting = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_now()
        self._engine.stop()
        soundboard().close()


def _label_for(binding_id: str, model: DeviceModel) -> str:
    parts = binding_id.split(":")
    kind, index = parts[0], int(parts[1])
    if kind == "key":
        return f"Screen key {index + 1}"
    if kind == "button":
        return f"Button {index + 1}"
    name = model.knob_names[index] if index < len(model.knob_names) else f"Knob {index + 1}"
    gesture = {"left": "turn left", "right": "turn right", "press": "press"}[parts[2]]
    return f"{name} — {gesture}"
