"""Entry point: `python -m s6deck` (or the `s6deck` script)."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config as config_store

APP_NAME = "s6deck"
APP_ICON = Path(__file__).resolve().parent / "assets" / "icon.png"


def _set_macos_process_name(name: str) -> None:
    """Show `name` instead of "python3.12" in the menu bar when run unbundled.
    Must run before Qt creates the application."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
    except ImportError:
        return
    info = NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info["CFBundleName"] = name


def _log_file() -> str:
    """Where a packaged build logs (it has no terminal)."""
    if sys.platform == "darwin":
        directory = Path.home() / "Library" / "Logs"
    else:
        directory = config_store.default_config_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{APP_NAME}.log")


def _self_test() -> int:
    """Exercise every bundled native dependency without opening the device or playing sound.
    Used to check packaged builds after trimming unused libraries."""
    import io
    import tempfile

    checks = []

    def check(name, function):
        try:
            detail = function()
            checks.append((True, name, detail))
        except Exception as error:  # report every failure, not just the first
            checks.append((False, name, repr(error)))

    def render_titled_key():
        from .config import Action, Binding
        from .render import render_binding
        image = render_binding(Binding(title="Test", color="#336699", action=Action("hotkey", "a")))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=90)
        return f"{image.size}, JPEG {len(buffer.getvalue())} bytes"

    def decode_audio():
        import numpy as np
        import soundfile
        from .audio import _load_clip
        with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
            soundfile.write(wav.name, np.zeros((4410, 1), dtype="float32"), 44100)
            clip = _load_clip(wav.name, 0.0)
        return f"resampled to {clip.shape}"

    def list_audio_devices():
        import sounddevice
        return f"{len(sounddevice.query_devices())} devices"

    def enumerate_hid():
        import hid
        return f"{len(hid.enumerate())} HID interfaces"

    def app_icon_lookup():
        from .app_icons import app_icon
        icon = app_icon("Finder" if sys.platform == "darwin" else "python")
        return f"{icon.size if icon else None}"

    def qt_widgets():
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        icon = QIcon(str(APP_ICON))
        return f"Qt {app.applicationVersion() or 'ok'}, app icon null={icon.isNull()}"

    check("render key with title (Pillow, FreeType, JPEG)", render_titled_key)
    check("decode + resample audio (libsndfile, numpy)", decode_audio)
    check("audio devices (PortAudio)", list_audio_devices)
    check("USB HID (hidapi)", enumerate_hid)
    check("app icon lookup", app_icon_lookup)
    check("Qt widgets + icon", qt_widgets)
    for ok, name, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return 0 if all(ok for ok, _, _ in checks) else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="s6deck", description="Controller for Streonor/MiraBox decks")
    parser.add_argument("--config", type=Path, default=config_store.default_config_path(),
                        help="config file (default: %(default)s)")
    parser.add_argument("--hidden", action="store_true", help="start minimized to the tray")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true",
                        help="check bundled libraries work, then exit (no device access, no sound)")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        filename=_log_file() if getattr(sys, "frozen", False) else None)
    logging.getLogger("PIL").setLevel(logging.INFO)  # PIL's debug output drowns ours

    _set_macos_process_name(APP_NAME)

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .fonts import install_qt_font
    from .gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    install_qt_font(app)  # one font for the whole app, matching the key titles
    app.setWindowIcon(QIcon(str(APP_ICON)))  # window, Dock and tray
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(config_store.load(args.config), args.config)
    if not args.hidden:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
