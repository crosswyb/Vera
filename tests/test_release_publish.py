"""Offline release checks with synthetic archives and no external commands."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vera_release_publisher", ROOT / "native/scripts/publish-release.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class ReleasePublishTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "release"
        self.directory.mkdir()
        self.receipt = {
            "product": "Vera",
            "version": "1.2.3",
            "build": "52",
            "source_revision": "a" * 40,
            "signed_mach_o_count": 2,
            "app_notarization_id": "11111111-1111-4111-8111-111111111111",
            "dmg_notarization_id": "22222222-2222-4222-8222-222222222222",
            "app_stapled": True,
            "dmg_stapled": True,
            "gatekeeper_app": "accepted",
            "gatekeeper_dmg": "accepted",
            "privacy_gate": "passed",
        }
        prefix = "Vera-1.2.3-build52-apple-silicon"
        self.archives = [self.directory / f"{prefix}.{suffix}" for suffix in ("dmg", "zip")]
        for artifact in self.archives:
            artifact.write_bytes(f"Synthetic {artifact.suffix} bytes; not an installer.".encode())
        self.checksums = self.directory / f"{prefix}.SHA256SUMS.txt"
        self.checksums.write_text("\n".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in self.archives
        ) + "\n")
        self.receipt_path = self.directory / "release-verification.json"
        self.write_receipt(self.receipt)
        # A missed mock must fail locally instead of signing, uploading or using the network.
        subprocess_guard = patch.object(publisher.subprocess, "run", side_effect=AssertionError("External command attempted"))
        subprocess_guard.start()
        self.addCleanup(subprocess_guard.stop)

    def write_receipt(self, receipt):
        self.receipt_path.write_text(json.dumps(receipt) + "\n")

    def test_publish_uses_exact_four_asset_allowlist(self):
        notes = self.root / "docs/release.md"
        notes.parent.mkdir()
        notes.write_text("Synthetic release notes. Contact: release@vera.invalid\n")
        for name in ("apple-raw-log.json", "privacy-local-report.json", "delivery-verification.json"):
            (self.directory / name).write_text('{"synthetic": true}\n')
        assets = [*self.archives, self.checksums, self.receipt_path]
        commands = []

        def fake_run(arguments):
            command = [str(argument) for argument in arguments]
            commands.append(command)
            if command[0] == "git":
                if "rev-parse" in command:
                    return self.receipt["source_revision"]
                return ""
            if command[:3] == ["gh", "repo", "view"]:
                return json.dumps({"visibility": "PRIVATE"})
            if command[:2] == ["gh", "api"]:
                return self.receipt["source_revision"]
            if command[:3] == ["gh", "release", "create"]:
                return ""
            if command[:3] == ["gh", "release", "view"]:
                return json.dumps({"assets": [
                    {"name": asset.name, "digest": "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest()}
                    for asset in assets
                ], "url": "https://releases.vera.invalid/synthetic"})
            if command[0] == sys.executable or command[0] in {"xcrun", "spctl"}:
                return ""
            self.fail(f"Unexpected command: {command[0]}")

        arguments = ["publish-release.py", "--release-dir", str(self.directory),
                     "--repo", "synthetic-owner/synthetic-repo", "--notes-file", str(notes)]
        with patch.object(publisher, "__file__", str(self.root / "native/scripts/publish-release.py")), \
                patch.object(sys, "argv", arguments), patch.object(publisher, "run", side_effect=fake_run), \
                patch.object(publisher, "verify_archived_app"), contextlib.redirect_stdout(io.StringIO()):
            publisher.main()

        upload = next(command for command in commands if command[:3] == ["gh", "release", "create"])
        self.assertEqual(upload[upload.index("--notes-file") + 2:], [str(asset) for asset in assets])
        delivery = json.loads((self.directory / "delivery-verification.json").read_text())
        self.assertEqual(delivery["assets"], sorted(asset.name for asset in assets))
        self.assertTrue(delivery["server_digests_match"])

    def test_changed_archive_is_rejected_before_any_external_command(self):
        self.archives[1].write_bytes(b"Synthetic archive changed after checksum generation")
        with self.assertRaisesRegex(ValueError, "checksums do not match"):
            publisher.release_assets(self.directory)

    def test_invalid_or_unverified_receipts_are_rejected(self):
        cases = [
            ("product", "Other"),
            ("signed_mach_o_count", 0),
            ("privacy_gate", "failed"),
            ("version", "invalid"),
            ("version", None),
            ("build", "52x"),
            ("source_revision", "a" * 39),
            ("app_stapled", False),
            ("dmg_stapled", None),
            ("gatekeeper_app", "rejected"),
            ("gatekeeper_dmg", None),
            ("app_notarization_id", "invalid"),
            ("dmg_notarization_id", "-" * 36),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                receipt = {**self.receipt, field: value}
                if value is None and field != "version":
                    receipt.pop(field)
                self.write_receipt(receipt)
                with self.assertRaises(ValueError):
                    publisher.release_assets(self.directory)

    def test_receipt_cannot_carry_extra_private_fields(self):
        for receipt in (None, [], {**self.receipt, "local_account": "synthetic-private-detail"}):
            with self.subTest(receipt_type=type(receipt).__name__):
                self.write_receipt(receipt)
                with self.assertRaisesRegex(ValueError, "approved verification fields"):
                    publisher.release_assets(self.directory)

    def test_unsigned_archived_app_is_rejected_even_with_claimed_receipt(self):
        commands = []

        def fake_subprocess(arguments, **kwargs):
            command = [str(argument) for argument in arguments]
            commands.append(command)
            if command[0] == "ditto":
                info = Path(command[-1]) / "真实交互.app/Contents/Info.plist"
                info.parent.mkdir(parents=True)
                info.write_bytes(plistlib.dumps({
                    "CFBundleName": "真实交互",
                    "CFBundleDisplayName": "真实交互",
                    "CFBundleShortVersionString": self.receipt["version"],
                    "CFBundleIdentifier": "com.zsjh.workbench",
                    "CFBundleVersion": self.receipt["build"],
                    "ZSJHSourceRevision": self.receipt["source_revision"],
                }))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if command[0] == "codesign":
                raise subprocess.CalledProcessError(1, command, stderr="Synthetic unsigned archive")
            self.fail(f"Command after unsigned archive: {command[0]}")

        with patch.object(publisher.subprocess, "run", side_effect=fake_subprocess):
            with self.assertRaisesRegex(RuntimeError, "Verification command codesign failed"):
                publisher.verify_archived_app(self.archives[1], self.receipt)
        self.assertEqual([command[0] for command in commands], ["ditto", "codesign"])

    def test_chinese_app_bundle_matches_english_release_brand(self):
        commands = []

        def fake_run(arguments):
            command = [str(argument) for argument in arguments]
            commands.append(command)
            if command[0] == "ditto":
                info = Path(command[-1]) / "真实交互.app/Contents/Info.plist"
                info.parent.mkdir(parents=True)
                info.write_bytes(plistlib.dumps({
                    "CFBundleName": "真实交互",
                    "CFBundleDisplayName": "真实交互",
                    "CFBundleShortVersionString": self.receipt["version"],
                    "CFBundleIdentifier": "com.zsjh.workbench",
                    "CFBundleVersion": self.receipt["build"],
                    "ZSJHSourceRevision": self.receipt["source_revision"],
                }))
            return ""

        with patch.object(publisher, "run", side_effect=fake_run):
            publisher.verify_archived_app(self.archives[1], self.receipt)
        self.assertEqual([command[0] for command in commands], ["ditto", "codesign", "xcrun", "spctl"])
        self.assertTrue(all(command[-1].endswith("/真实交互.app") for command in commands[1:]))
        self.assertTrue(all(artifact.name.startswith("Vera-") for artifact in self.archives))

    def test_incorrect_bundle_or_visible_name_is_rejected_before_signature_checks(self):
        cases = [("Vera.app", "真实交互", "真实交互", None),
                 ("真实交互.app", "Vera", "真实交互", None),
                 ("真实交互.app", "真实交互", "Vera", None),
                 ("真实交互.app", "真实交互", "真实交互", "Vera.app")]
        for bundle_name, product_name, display_name, extra_bundle in cases:
            with self.subTest(bundle=bundle_name, name=product_name, display=display_name, extra=extra_bundle):
                commands = []

                def fake_run(arguments):
                    command = [str(argument) for argument in arguments]
                    commands.append(command)
                    self.assertEqual(command[0], "ditto", "Wrong names must fail before signature commands")
                    info = Path(command[-1]) / bundle_name / "Contents/Info.plist"
                    info.parent.mkdir(parents=True)
                    info.write_bytes(plistlib.dumps({
                        "CFBundleName": product_name,
                        "CFBundleDisplayName": display_name,
                        "CFBundleShortVersionString": self.receipt["version"],
                        "CFBundleIdentifier": "com.zsjh.workbench",
                        "CFBundleVersion": self.receipt["build"],
                        "ZSJHSourceRevision": self.receipt["source_revision"],
                    }))
                    if extra_bundle:
                        (Path(command[-1]) / extra_bundle).mkdir()
                    return ""

                with patch.object(publisher, "run", side_effect=fake_run):
                    with self.assertRaises(ValueError):
                        publisher.verify_archived_app(self.archives[1], self.receipt)
                self.assertEqual(len(commands), 1)


if __name__ == "__main__":
    unittest.main()
