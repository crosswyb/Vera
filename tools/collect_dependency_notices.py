"""Collect installed build/runtime dependency notices without local config/data."""

import importlib.metadata as metadata
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
NOTICE_NAME = re.compile(r"(?:^|[-_])(license|licence|copying|notice)(?:$|[._-])", re.IGNORECASE)
NOTICE_SUFFIXES = {"", ".txt", ".md", ".rst", ".apache", ".bsd", ".mit"}


def is_notice_file(path: Path) -> bool:
    """Accept notice text, never importable modules or cache directories."""
    if "__pycache__" in path.parts or not path.is_file():
        return False
    if not NOTICE_NAME.search(path.name) or path.suffix.lower() not in NOTICE_SUFFIXES:
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return bool(content.strip()) and "\0" not in content


def collect_notices(distributions, output: Path):
    """Rebuild generated notices so stale modules cannot remain in the bundle."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".python-notices-", dir=output.parent) as temporary:
        staged = Path(temporary) / "python"
        staged.mkdir()
        manifest = []
        for name, dist in sorted(distributions.items()):
            notices = []
            for file in dist.files or []:
                relative = Path(str(file))
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                source = Path(dist.locate_file(file))
                if not is_notice_file(source):
                    continue
                target = staged / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                notices.append(str(target.relative_to(staged)))
            if not notices:
                raise RuntimeError(f"No license file found for {name}")
            manifest.append({"package": name, "version": dist.version, "notices": notices})
        (staged / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if output.exists():
            shutil.rmtree(output)
        staged.replace(output)
    return manifest


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
    manifest = collect_notices(distributions, ROOT / "third_party/python")
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
