# SPDX-License-Identifier: GPL-3.0-or-later
"""Collect original distribution notices without importing application code."""

import importlib.metadata
import json
import shutil
import sys
from pathlib import Path

destination = Path(sys.argv[1])
packages = []
for distribution in sorted(importlib.metadata.distributions(), key=lambda item: item.metadata["Name"]):
    name = distribution.metadata["Name"].lower().replace("_", "-")
    version = distribution.version
    target = destination / (name + "-" + version)
    target.mkdir(parents=True, exist_ok=True)
    (target / "METADATA").write_text(distribution.read_text("METADATA") or "", encoding="utf-8")
    notices = []
    for file in distribution.files or []:
        if any(part.lower().startswith(("license", "copying", "notice", "authors")) for part in file.parts):
            source = Path(str(distribution.locate_file(file)))
            if source.is_file():
                # Preserve nested third-party notices rather than flatten them.
                relative = Path(*[part for part in file.parts if part not in (".", "..")])
                output = target / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
                notices.append(str(relative))
    packages.append({"name": name, "version": version, "notices": notices})
print(json.dumps(packages))
