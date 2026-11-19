"""Collect installed build/runtime dependency notices without local config/data."""

import importlib.metadata as metadata
import json
import shutil
import sys
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def main():
    # PyInstaller's setuptools hooks also bundle these distribution helpers.
    pending = ["Flask", "pydantic", "filelock", "requests", "python-dotenv", "pyinstaller",
               "attrs", "importlib_metadata", "zipp"]
    distributions = {}
    while pending:
        dist = metadata.distribution(pending.pop())
        name = dist.metadata["Name"]
        if name in distributions:
            continue
        distributions[name] = dist
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if not requirement.marker or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    output = ROOT / "third_party/python"
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, dist in sorted(distributions.items()):
        notices = []
        for file in dist.files or []:
            if not any(word in str(file).lower() for word in ("license", "copying", "notice")):
                continue
            source = dist.locate_file(file)
            if not source.is_file():
                continue
            target = output / name / str(file)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            notices.append(str(target.relative_to(output)))
        if not notices:
            raise RuntimeError(f"No license file found for {name}")
        manifest.append({"package": name, "version": dist.version, "notices": notices})
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (ROOT / "requirements-build.lock").write_text(
        "# Versions used for the verified desktop build.\n" +
        "\n".join(f"{name}=={dist.version}" for name, dist in sorted(distributions.items())) + "\n")
    python_license = Path(sys.base_prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/LICENSE.txt"
    if python_license.is_file():
        runtime = ROOT / "third_party/python-runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(python_license, runtime / "LICENSE.txt")
    print(f"Collected notices for {len(manifest)} dependencies")


if __name__ == "__main__":
    main()
