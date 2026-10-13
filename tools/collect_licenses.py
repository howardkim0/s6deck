"""Generate THIRD_PARTY_NOTICES.md (summary) and THIRD_PARTY_LICENSES.txt (full texts).

Covers everything shipped inside a built s6deck app: the runtime Python packages
(s6deck's dependency closure, read from installed metadata), plus components that
aren't separate pip packages (CPython, Qt, PortAudio, libsndfile, the font, the
PyInstaller bootloader). Run after changing dependencies:

    .venv/bin/python tools/collect_licenses.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
LICENSES = ROOT / "licenses"
SITE = Path(distribution("pillow").locate_file(""))

LGPL3 = [LICENSES / "lgpl-3.0.txt", LICENSES / "gpl-3.0.txt"]  # LGPLv3 incorporates GPLv3 by reference
PYOBJC_MIT = SITE / "pyobjc_framework_cocoa-{v}.dist-info/licenses/LICENSE.txt"


@dataclass
class Component:
    name: str
    version: str
    license: str
    url: str
    note: str = ""
    texts: list[Path] = field(default_factory=list)


# Packages whose wheels ship no license file, or whose terms need a note.
PACKAGE_OVERRIDES = {
    "pyside6_essentials": dict(license="LGPL-3.0-only (Qt for Python)", texts=LGPL3,
                               url="https://wiki.qt.io/Qt_for_Python"),
    "shiboken6": dict(license="LGPL-3.0-only", texts=LGPL3, url="https://wiki.qt.io/Qt_for_Python"),
    "pyobjc-core": dict(license="MIT", url="https://github.com/ronaldoussoren/pyobjc"),
    "pyobjc-framework-applicationservices": dict(license="MIT", url="https://github.com/ronaldoussoren/pyobjc"),
    "pyobjc-framework-coretext": dict(license="MIT", url="https://github.com/ronaldoussoren/pyobjc"),
    "hidapi": dict(license="BSD-3-Clause (chosen from BSD / GPL-3.0 / original HIDAPI license)",
                   url="https://github.com/trezor/cython-hidapi"),
    "pynput": dict(license="LGPL-3.0-only", texts=[*LGPL3]),
}


def runtime_distributions() -> list[str]:
    """s6deck's installed runtime dependency closure (no dev/build extras)."""
    seen: dict[str, str] = {}
    pending = ["s6deck"]
    while pending:
        try:
            dist = distribution(pending.pop())
        except PackageNotFoundError:
            continue
        key = dist.metadata["Name"].lower()
        if key in seen:
            continue
        seen[key] = dist.version
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    seen.pop("s6deck")
    return sorted(seen)


def _preferred_project_url(entries: list[str]) -> str:
    """Pick the homepage/source link from "Label, URL" Project-URL entries."""
    urls = {label.strip().lower(): url.strip() for label, _, url in (e.partition(",") for e in entries)}
    for label in ("homepage", "home", "source", "source code", "repository", "github", "documentation"):
        if label in urls:
            return urls[label]
    return ""


def package_component(name: str) -> Component:
    dist = distribution(name)
    meta = dist.metadata
    license_name = meta.get("License-Expression") or ""
    if not license_name or len(license_name) > 80:
        classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
        license_name = "; ".join(classifiers) or (meta.get("License") or "")[:80]
    project_url = meta.get("Home-page") or _preferred_project_url(meta.get_all("Project-URL") or []) \
        or f"https://pypi.org/project/{name}/"
    texts = [Path(dist.locate_file(f)) for f in dist.files or []
             if any(k in Path(str(f)).name.upper() for k in ("LICENSE", "COPYING", "NOTICE"))
             and ".dist-info" in str(f)]
    component = Component(meta["Name"], dist.version, license_name, project_url, texts=texts)
    override = PACKAGE_OVERRIDES.get(name, {})
    for attribute, value in override.items():
        setattr(component, attribute, value)
    if name.startswith("pyobjc") and not component.texts:
        component.texts = [Path(str(PYOBJC_MIT).format(v=distribution("pyobjc-framework-cocoa").version))]
    return component


def bundled_components() -> list[Component]:
    """Shipped in the app but not separate pip packages."""
    python_license = Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"
    pyinstaller = distribution("pyinstaller")
    return [
        Component("CPython", sys.version.split()[0], "PSF-2.0", "https://www.python.org/",
                  "The Python runtime embedded in the app.", [python_license]),
        Component("Qt", distribution("pyside6_essentials").version, "LGPL-3.0-only", "https://www.qt.io/",
                  "Qt libraries bundled with PySide6. Qt's own third-party components: "
                  "https://doc.qt.io/qt-6/licenses-used-in-qt.html", LGPL3),
        Component("PortAudio", "19", "MIT", "http://www.portaudio.com/",
                  "Audio I/O library bundled with sounddevice.", [LICENSES / "portaudio.txt"]),
        Component("libsndfile", "1.2", "LGPL-2.1-or-later", "https://libsndfile.github.io/libsndfile/",
                  "Audio decoding library bundled with soundfile (includes its codec libraries).",
                  [SITE / "_soundfile_data" / "COPYING"]),
        Component("Aileron Regular", "", "CC0-1.0", "https://dotcolon.net/fonts/aileron/",
                  "The app's font, embedded in Pillow.", [LICENSES / "cc0-1.0.txt"]),
        Component("PyInstaller bootloader", pyinstaller.version, "GPL-2.0-or-later WITH Bootloader-exception",
                  "https://pyinstaller.org/",
                  "The executable stub of packaged builds. The exception permits distributing apps "
                  "built with it under any license.",
                  [Path(pyinstaller.locate_file(f)) for f in pyinstaller.files or [] if "COPYING" in str(f)]),
    ]


NOT_BUNDLED = [
    ("BlackHole", "GPL-3.0", "https://github.com/ExistentialAudio/BlackHole",
     "Optional virtual audio device for the soundboard; installed separately by the user."),
    ("PyInstaller (build tool)", "GPL-2.0", "https://pyinstaller.org/", "Used to build the app; only its bootloader ships."),
    ("pytest", "MIT", "https://pytest.org/", "Tests only."),
]


def main() -> None:
    components = [*bundled_components(), *(package_component(name) for name in runtime_distributions())]

    notices = [
        "# Third-party notices",
        "",
        "s6deck is MIT-licensed (see [LICENSE](LICENSE)). Built apps include the components below; "
        "their full license texts are in [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt), "
        "which is generated by `tools/collect_licenses.py` and shipped inside the app.",
        "",
        "LGPL components (Qt/PySide6, pynput, libsndfile) are used unmodified as separate libraries; "
        "their source is available from the linked projects, and s6deck's source and build spec let "
        "you rebuild the app against modified versions.",
        "",
        "## Included in the app",
        "",
        "| Component | Version | License | Project |",
        "|---|---|---|---|",
        *(f"| {c.name} | {c.version or '—'} | {c.license} | {c.url} |" for c in components),
        "",
        "## Not included in the app",
        "",
        "| Component | License | Project | Notes |",
        "|---|---|---|---|",
        *(f"| {name} | {lic} | {url} | {note} |" for name, lic, url, note in NOT_BUNDLED),
        "",
    ]
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text("\n".join(notices), encoding="utf-8")

    missing = []
    sections = ["Third-party licenses for software included in s6deck.", ""]
    for component in components:
        header = f"{component.name} {component.version}".strip()
        sections += ["=" * 78, header, f"License: {component.license}", f"Project: {component.url}"]
        if component.note:
            sections.append(component.note)
        sections += ["=" * 78, ""]
        texts = [path for path in component.texts if path.is_file()]
        if not texts:
            missing.append(component.name)
        seen_texts = set()
        for path in texts:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text in seen_texts:
                continue
            seen_texts.add(text)
            sections += [f"--- {path.name} ---", "", text, ""]
    (ROOT / "THIRD_PARTY_LICENSES.txt").write_text("\n".join(sections) + "\n", encoding="utf-8")

    print(f"{len(components)} components written")
    if missing:
        print("WARNING: no license text found for:", ", ".join(missing))
        sys.exit(1)


if __name__ == "__main__":
    main()
