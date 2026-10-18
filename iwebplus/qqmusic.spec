# SPDX-License-Identifier: GPL-3.0-or-later
"""PyInstaller entry for the iWebPlus private QQMusicApi module."""

import json
import os
from pathlib import Path

module = Path(SPECPATH)
upstream = module.parent
# These packages are not installed in the build venv. Enumerate the source
# tree; collect_submodules before Analysis would silently miss adapters.
hidden = [
    ".".join(path.relative_to(upstream).with_suffix("").parts)
    for package in ("web/src", "qqmusic_api")
    for path in sorted((upstream / package).rglob("*.py"))
    if path.name != "__init__.py"
]
a = Analysis(
    [str(module / "launcher.py")],
    pathex=[str(upstream)],
    binaries=[],
    datas=[(str(upstream / "LICENSE"), "licenses")],
    hiddenimports=hidden + ["uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "unittest",
        "setuptools",
        "pip",
        "wheel",
        "readline",
        "_curses",
        "_curses_panel",
        "zstandard.backend_cffi",
        "zstandard._cffi",
    ],
    noarchive=False,
    optimize=0,
)
Path(os.environ["IWEBPLUS_LIBRARIES"]).write_text(json.dumps(a.binaries))
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="qqmusic",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    console=True,
)
