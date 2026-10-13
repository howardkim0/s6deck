"""User configuration: profiles -> pages -> control bindings, stored as JSON.

A binding maps a control (see controls.binding ids, e.g. "key:0", "button:1",
"knob:2:left") to an optional look (title/icon/color, screen keys only) and an action.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1


@dataclass
class Action:
    # none | hotkey | launch | url | shell | page | profile | brightness
    type: str = "none"
    value: str = ""


@dataclass
class Binding:
    title: str = ""
    icon: str = ""            # path to an image file
    color: str = "#1e1e1e"    # key background
    action: Action = field(default_factory=Action)

    def is_empty(self) -> bool:
        return not self.title and not self.icon and self.action.type == "none"


@dataclass
class Page:
    name: str = "Page 1"
    bindings: dict[str, Binding] = field(default_factory=dict)


@dataclass
class Profile:
    name: str = "Default"
    # Frontmost-app names (case-insensitive) that activate this profile.
    apps: list[str] = field(default_factory=list)
    pages: list[Page] = field(default_factory=lambda: [Page()])


@dataclass
class Config:
    brightness: int = 80
    follow_frontmost_app: bool = True
    # Forward the microphone + soundboard clips into a virtual audio device for calls.
    virtual_mic: bool = False
    sound_volume: int = 30  # percent, soundboard clips only
    profiles: list[Profile] = field(default_factory=lambda: [Profile()])

    def profile_named(self, name: str) -> Profile | None:
        return next((p for p in self.profiles if p.name == name), None)

    def profile_for_app(self, app_name: str | None) -> Profile:
        """The profile listing app_name, else the first (default) profile."""
        if app_name:
            wanted = app_name.casefold()
            for profile in self.profiles:
                if any(app.casefold() == wanted for app in profile.apps):
                    return profile
        return self.profiles[0]


def default_config_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "s6deck" / "config.json"


def to_dict(config: Config) -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "brightness": config.brightness,
        "follow_frontmost_app": config.follow_frontmost_app,
        "virtual_mic": config.virtual_mic,
        "sound_volume": config.sound_volume,
        "profiles": [
            {
                "name": profile.name,
                "apps": profile.apps,
                "pages": [
                    {
                        "name": page.name,
                        "bindings": {control: asdict(binding)
                                     for control, binding in page.bindings.items()
                                     if not binding.is_empty()},
                    }
                    for page in profile.pages
                ],
            }
            for profile in config.profiles
        ],
    }


def from_dict(data: dict[str, Any]) -> Config:
    profiles = []
    for raw_profile in data.get("profiles") or []:
        pages = []
        for raw_page in raw_profile.get("pages") or []:
            bindings = {
                control: Binding(
                    title=raw.get("title", ""),
                    icon=raw.get("icon", ""),
                    color=raw.get("color", "#1e1e1e"),
                    action=Action(**raw.get("action", {})),
                )
                for control, raw in (raw_page.get("bindings") or {}).items()
            }
            pages.append(Page(name=raw_page.get("name", f"Page {len(pages) + 1}"), bindings=bindings))
        profiles.append(Profile(
            name=raw_profile.get("name", "Default"),
            apps=list(raw_profile.get("apps") or []),
            pages=pages or [Page()],
        ))
    return Config(
        brightness=int(data.get("brightness", 80)),
        follow_frontmost_app=bool(data.get("follow_frontmost_app", True)),
        virtual_mic=bool(data.get("virtual_mic", False)),
        sound_volume=int(data.get("sound_volume", 30)),
        profiles=profiles or [Profile()],
    )


def load(path: Path) -> Config:
    if not path.exists():
        return Config()
    return from_dict(json.loads(path.read_text(encoding="utf-8")))


def save(config: Config, path: Path) -> None:
    """Write atomically so a crash mid-save never leaves a truncated config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(to_dict(config), handle, indent=2)
    os.replace(temp_name, path)
