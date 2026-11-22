"""Release notice collection must not ship old Python modules or bytecode."""

import json
from pathlib import Path
import plistlib
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tools.collect_dependency_notices import collect_notices, is_notice_file


class DesktopBrandingTests(unittest.TestCase):
    def test_installed_name_is_chinese_and_existing_identity_is_preserved(self):
        native = Path(__file__).resolve().parents[1] / "native"
        with (native / "DesktopInfo.plist").open("rb") as stream:
            info = plistlib.load(stream)
        self.assertEqual(info["CFBundleName"], "真实交互")
        self.assertEqual(info["CFBundleDisplayName"], "真实交互")
        self.assertEqual(info["CFBundleIdentifier"], "com.zsjh.workbench")
        self.assertEqual(info["CFBundleExecutable"], "DesktopHost")


class DependencyNoticeTests(unittest.TestCase):
    def test_only_notice_text_files_are_accepted(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("LICENSE", "LICENSE.APACHE", "LICENSE.BSD", "COPYING.txt", "NOTICE", "ICON_LICENSE.md"):
                path = root / name
                path.write_text("Copyright Example. Permission is hereby granted.\n")
                self.assertTrue(is_notice_file(path), name)
            for name in ("LICENSE.py", "LICENSE.pyc", "licenses/__init__.py", "__pycache__/LICENSE.txt"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Copyright Example.\n")
                self.assertFalse(is_notice_file(path), name)
            directory = root / "LICENSES"
            directory.mkdir(exist_ok=True)
            self.assertFalse(is_notice_file(directory))
            (root / "LICENSE.txt").write_bytes(b"binary\0payload")
            self.assertFalse(is_notice_file(root / "LICENSE.txt"))

    def test_collection_removes_stale_modules_and_keeps_all_notice_attribution(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "installed"
            source.mkdir()
            files = ["example.dist-info/licenses/LICENSE", "example/ICON_LICENSE.md", "example/licenses/__init__.py"]
            for name in files:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Copyright Example.\n")
            output = root / "notices"
            stale = output / "example/licenses/__pycache__/__init__.pyc"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale build path")
            dist = SimpleNamespace(files=files, version="1.2.3", locate_file=lambda file: source / file)

            manifest = collect_notices({"example": dist}, output)

            expected = ["example/example.dist-info/licenses/LICENSE", "example/example/ICON_LICENSE.md"]
            self.assertEqual(manifest, [{"package": "example", "version": "1.2.3", "notices": expected}])
            self.assertEqual(json.loads((output / "manifest.json").read_text()), manifest)
            self.assertEqual({str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()},
                             {"manifest.json", *expected})

    def test_invalid_collection_preserves_existing_notices(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "notices"
            output.mkdir()
            existing = output / "LICENSE"
            existing.write_text("Existing attribution\n")
            dist = SimpleNamespace(files=[], version="1.2.3")

            with self.assertRaisesRegex(RuntimeError, "No license file found for example"):
                collect_notices({"example": dist}, output)

            self.assertEqual(existing.read_text(), "Existing attribution\n")


if __name__ == "__main__":
    unittest.main()
