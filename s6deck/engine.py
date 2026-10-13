"""Background loop tying config, device and actions together.

Runs on its own thread: (re)connects to the device, renders the active page,
decodes input, runs bindings, and follows the frontmost app. GUI-facing
callbacks are invoked from this thread; the GUI must marshal them to its own.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from . import actions, controls
from .config import Binding, Config, Page, Profile
from .controls import ControlEvent, ControlKind, Gesture
from .device import Device, connected_devices
from .frontmost import frontmost_app
from .render import render_binding

log = logging.getLogger(__name__)

RECONNECT_INTERVAL_SECONDS = 2.0
FRONTMOST_POLL_SECONDS = 1.0
BRIGHTNESS_STEP = 10


@dataclass(frozen=True)
class EngineState:
    connected: bool
    device_name: str
    profile_name: str
    page_index: int
    message: str = ""


class Engine:
    def __init__(self, config: Config,
                 on_state: Callable[[EngineState], None] = lambda state: None,
                 on_event: Callable[[ControlEvent], None] = lambda event: None,
                 on_brightness: Callable[[int], None] = lambda value: None,
                 run_action: Callable[[str, str], None] | None = None):
        """run_action(type, value) executes host actions. GUIs should pass a function that
        hands the action to their main thread: macOS keyboard APIs used for hotkeys crash
        when called off the main thread. Default: a dedicated worker thread."""
        self._config = copy.deepcopy(config)
        self._on_state = on_state
        self._on_event = on_event
        self._on_brightness = on_brightness
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="s6deck-engine", daemon=True)
        self._actions = ThreadPoolExecutor(max_workers=1, thread_name_prefix="s6deck-action")
        self._run_action = run_action or (
            lambda action_type, value: self._actions.submit(actions.run_host_action, action_type, value))
        self._device: Device | None = None
        self._profile_name = self._config.profiles[0].name
        self._page_index = 0
        self._needs_render = True
        self._needs_brightness = True
        self._sent_image_digests: dict[int, bytes] = {}
        self._last_app: str | None = None
        self._message = ""

    # -- control from the GUI (any thread) --------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._actions.shutdown(wait=False, cancel_futures=True)

    def update_config(self, config: Config) -> None:
        with self._lock:
            self._config = copy.deepcopy(config)
            if self._config.profile_named(self._profile_name) is None:
                self._profile_name = self._config.profiles[0].name
            self._page_index = min(self._page_index, len(self._current_profile().pages) - 1)
            self._needs_render = True
            self._needs_brightness = True
        self._publish_state()

    def show(self, profile_name: str, page_index: int) -> None:
        with self._lock:
            profile = self._config.profile_named(profile_name)
            if profile is None:
                return
            self._profile_name = profile_name
            self._page_index = max(0, min(page_index, len(profile.pages) - 1))
            self._needs_render = True
        self._publish_state()

    # -- loop -------------------------------------------------------------

    def _run(self) -> None:
        last_connect_attempt = 0.0
        last_frontmost_poll = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if self._device is None:
                if now - last_connect_attempt >= RECONNECT_INTERVAL_SECONDS:
                    last_connect_attempt = now
                    self._try_connect()
                if self._device is None:
                    self._stop.wait(0.2)
                    continue

            if now - last_frontmost_poll >= FRONTMOST_POLL_SECONDS:
                last_frontmost_poll = now
                self._follow_frontmost_app()

            try:
                self._sync_display()
                event = self._device.read_event(timeout_ms=100)
                if event is not None:
                    self._handle_input(event.code, event.state)
            except OSError as error:
                log.warning("device connection lost: %s", error)
                self._disconnect(f"Disconnected: {error}")

        if self._device is not None:
            self._device.close()
            self._device = None

    def _try_connect(self) -> None:
        devices = connected_devices()
        if not devices:
            self._set_message("No supported device connected")
            return
        model, info = devices[0]
        try:
            self._device = Device(model, info)
        except OSError as error:
            # Typically another app (e.g. the vendor software) holds the device.
            self._set_message(f"{model.name} found but could not be opened ({error}). "
                              "Quit any other software using it.")
            return
        with self._lock:
            self._needs_render = True
            self._needs_brightness = True
            self._sent_image_digests.clear()
        log.info("connected to %s (serial %s)", model.name, self._device.serial)
        self._message = ""
        self._publish_state()

    def _disconnect(self, message: str) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except OSError:
                pass
        self._device = None
        self._set_message(message)

    # -- display ----------------------------------------------------------

    def _sync_display(self) -> None:
        with self._lock:
            needs_brightness, self._needs_brightness = self._needs_brightness, False
            needs_render, self._needs_render = self._needs_render, False
            brightness = self._config.brightness
            page = copy.deepcopy(self._current_page())
        device = self._device
        if needs_brightness:
            device.set_brightness(brightness)
        if needs_render:
            changed = False
            for position in range(device.model.key_count):
                image = render_binding(page.bindings.get(controls.key_binding_id(position)))
                digest = hashlib.sha1(image.tobytes()).digest()
                if self._sent_image_digests.get(position) == digest:
                    continue
                device.set_key_image(position, image, refresh=False)
                self._sent_image_digests[position] = digest
                changed = True
            if changed:
                device.refresh()

    # -- input ------------------------------------------------------------

    def _handle_input(self, code: int, state: int) -> None:
        event = controls.decode(self._device.model, code, state)
        if event is None:
            log.debug("unmapped input code 0x%02x state %d", code, state)
            return
        log.debug("input %s", event.binding_id)
        self._on_event(event)
        if event.gesture is Gesture.RELEASE:
            return
        with self._lock:
            binding = self._current_page().bindings.get(event.binding_id)
            binding = copy.deepcopy(binding) if binding else None
        if binding is not None:
            self._run_binding(binding)

    def _run_binding(self, binding: Binding) -> None:
        action = binding.action
        if action.type == "page":
            self._change_page(action.value)
        elif action.type == "profile":
            with self._lock:
                profile = self._config.profile_named(action.value)
            if profile is not None:
                self.show(profile.name, 0)
        elif action.type == "brightness":
            self._change_brightness(action.value)
        else:
            self._run_action(action.type, action.value)

    def _change_page(self, value: str) -> None:
        with self._lock:
            page_count = len(self._current_profile().pages)
            if value == "next":
                target = (self._page_index + 1) % page_count
            elif value == "prev":
                target = (self._page_index - 1) % page_count
            elif value.isdigit():
                target = int(value) - 1
            else:
                return
            profile_name = self._profile_name
        self.show(profile_name, target)

    def _change_brightness(self, value: str) -> None:
        with self._lock:
            current = self._config.brightness
            if value == "up":
                target = current + BRIGHTNESS_STEP
            elif value == "down":
                target = current - BRIGHTNESS_STEP
            elif value.isdigit():
                target = int(value)
            else:
                return
            self._config.brightness = max(0, min(100, target))
            self._needs_brightness = True
            brightness = self._config.brightness
        self._on_brightness(brightness)

    # -- frontmost app ----------------------------------------------------

    def _follow_frontmost_app(self) -> None:
        with self._lock:
            enabled = self._config.follow_frontmost_app and len(self._config.profiles) > 1
        if not enabled:
            return
        app = frontmost_app()
        # Ignore our own window so editing a profile in the GUI doesn't switch away from it.
        if app is None or app.pid == os.getpid() or app.name == self._last_app:
            return
        self._last_app = app.name
        with self._lock:
            profile = self._config.profile_for_app(app.name)
            switch = profile.name != self._profile_name
        if switch:
            self.show(profile.name, 0)

    # -- helpers ----------------------------------------------------------

    def _current_profile(self) -> Profile:
        return self._config.profile_named(self._profile_name) or self._config.profiles[0]

    def _current_page(self) -> Page:
        pages = self._current_profile().pages
        return pages[min(self._page_index, len(pages) - 1)]

    def _set_message(self, message: str) -> None:
        if message != self._message:
            self._message = message
            self._publish_state()

    def _publish_state(self) -> None:
        with self._lock:
            state = EngineState(
                connected=self._device is not None,
                device_name=self._device.model.name if self._device else "",
                profile_name=self._profile_name,
                page_index=self._page_index,
                message=self._message,
            )
        self._on_state(state)
