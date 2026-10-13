# PyInstaller spec. Build from the repo root:  .venv/bin/pyinstaller --noconfirm packaging/s6deck.spec
# Output: dist/s6deck.app (macOS), dist/s6deck/ (Windows/Linux). Build each OS on that OS.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
ASSETS = ROOT / "s6deck" / "assets"
APP_NAME = "s6deck"
BUNDLE_ID = "io.github.s6deck"
VERSION = "0.1.0"

hidden_imports = [
    *collect_submodules("pynput"),          # backends are chosen at runtime per OS
    "PySide6.QtSvg",                        # Linux SVG app icons
    "_sounddevice_data", "_soundfile_data",
]
if sys.platform == "darwin":
    hidden_imports += ["AppKit", "Foundation", "Quartz", "objc"]

binaries = [*collect_dynamic_libs("_sounddevice_data"), *collect_dynamic_libs("_soundfile_data")]
# Runtime icons only; the *_source images are build inputs for tools/build_icons.py.
datas = [(str(path), "s6deck/assets") for path in ASSETS.iterdir()
         if path.suffix in (".png", ".ico", ".icns") and "_source" not in path.stem]
# s6deck's license and third-party notices ship with every build (MIT/BSD/LGPL require it).
# Regenerate the notices with tools/collect_licenses.py when dependencies change.
datas += [(str(ROOT / name), ".") for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt")]

# Bundled files s6deck never uses, matched against their destination path in the bundle.
UNUSED_FILES = (
    "PySide6/Qt/translations/",          # Qt's own UI translations; s6deck is English-only
    "PySide6/Qt/plugins/imageformats/",  # PNG (all s6deck uses) is built into QtGui
    "PySide6/Qt/plugins/iconengines/",   # SVG icon engine; no SVG icons in the UI
    "PySide6/Qt/plugins/generic/",       # TUIO touch input; also the only user of QtNetwork
    "PySide6/Qt/plugins/platforms/libqminimal",
    "PySide6/Qt/plugins/platforms/libqoffscreen",
    "PySide6/Qt/lib/QtNetwork.framework/",
    "PySide6/QtNetwork.abi3.so",
    "PySide6/QtDBus.abi3.so",            # Python binding only; QtGui itself still links QtDBus
    # Pillow's optional codecs: AVIF, WebP, color management (ICC), Tk. Pillow imports
    # these lazily and treats them as unsupported when absent. Its core module links
    # TIFF/JPEG 2000/xcb directly, so those stay; FreeType/HarfBuzz render key titles.
    "PIL/_avif", "PIL/.dylibs/libavif",
    "PIL/_webp", "PIL/.dylibs/libwebp", "PIL/.dylibs/libsharpyuv",
    "PIL/_imagingcms", "PIL/.dylibs/liblcms2",
    "PIL/_imagingtk",
)
if sys.platform == "darwin":
    UNUSED_FILES += ("_sounddevice_data/portaudio-binaries/libportaudio32bit",
                     "_sounddevice_data/portaudio-binaries/libportaudio64bit",
                     "_sounddevice_data/portaudio-binaries/libportaudioarm64")  # Windows DLLs


def _used(entry) -> bool:
    destination = entry[0].replace("\\", "/")
    return not any(destination.startswith(prefix) for prefix in UNUSED_FILES)

# Qt modules s6deck never uses; leaving them out cuts the bundle size a lot.
excludes = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
            "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtNetwork", "PySide6.QtPdf",
            "PySide6.QtCharts", "PySide6.QtDataVisualization", "tkinter", "pytest"]

analysis = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    excludes=excludes,
)
analysis.binaries = [entry for entry in analysis.binaries if _used(entry)]
analysis.datas = [entry for entry in analysis.datas if _used(entry)]
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=str(ASSETS / ("icon.icns" if sys.platform == "darwin" else "icon.ico")),
)
collected = COLLECT(exe, analysis.binaries, analysis.datas, name=APP_NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        collected,
        name=f"{APP_NAME}.app",
        icon=str(ASSETS / "icon.icns"),
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            # Required for microphone access in a bundled app (Virtual mic forwards your mic).
            "NSMicrophoneUsageDescription":
                "s6deck forwards your microphone into the virtual mic so call apps hear "
                "your voice together with soundboard sounds.",
        },
    )
