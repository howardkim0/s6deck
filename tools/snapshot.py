"""Render the main window offscreen to a PNG (device engine disabled).

Usage: QT_QPA_PLATFORM=offscreen .venv/bin/python tools/snapshot.py OUT.png
           [--config FILE] [--page N] [--select BINDING_ID]
"""
import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from s6deck import config as config_store
from s6deck.engine import Engine
from s6deck.fonts import install_qt_font
from s6deck.gui.main_window import MainWindow

Engine.start = lambda self: None  # don't touch the real device
Engine.stop = lambda self: None

parser = argparse.ArgumentParser()
parser.add_argument("out")
parser.add_argument("--config", type=Path, default=config_store.default_config_path())
parser.add_argument("--page", type=int, default=1, help="1-based page of the first profile")
parser.add_argument("--select", default="", help="binding id to select, e.g. key:0 or knob:0:left")
args = parser.parse_args()

app = QApplication(sys.argv[:1])
install_qt_font(app)
window = MainWindow(config_store.load(args.config), Path("/tmp/snapshot-config.json"))
if window._tray:
    window._tray.hide()
window.show()
window._page_tabs.setCurrentIndex(args.page - 1)
if args.select:
    window._deck.select(args.select)
window.statusBar().clearMessage()
app.processEvents()
window.resize(window.sizeHint())
app.processEvents()
window.grab().save(args.out)
print("wrote", args.out, window.size().toTuple())
