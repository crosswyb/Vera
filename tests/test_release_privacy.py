"""Offline regressions for private data that previously escaped release checks."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import struct
import subprocess
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import release_privacy as privacy


class ReleasePrivacyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, relative, content=b"safe"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def categories(self, findings):
        return {finding["category"] for finding in findings}

    def private_path(self):
        # Construct probe values so the real source audit remains clean.
        return "/" + "Users" + "/" + "private-release-person" + "/project/file.swift"

    def zip_bytes(self, members):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in members.items():
                archive.writestr(name, value)
        return output.getvalue()

    def git(self, *arguments, env=None):
        return subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env).stdout

    def repository(self):
        self.git("init", "--quiet")
        self.git("config", "user.name", "Vera Contributors")
        self.git("config", "user.email", "dev@vera.invalid")

    def commit(self, message, **identity):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", message, env={**os.environ, **identity})

    def test_native_bytes_containing_personal_home_path_block_release(self):
        self.write("Contents/MacOS/DesktopHost", b"\xcf\xfa\xed\xfe\x00" + self.private_path().encode() + b"\x00")
        findings = privacy.audit_bundle(self.root)
        self.assertIn("local_home_path", self.categories(findings))
        serialized = json.dumps(findings)
        self.assertNotIn(self.private_path(), serialized)
        self.assertNotIn("private-release-person", serialized)
        self.assertIn("Contents/MacOS/DesktopHost", serialized)

    def test_hosted_runner_identity_does_not_disable_personal_path_or_dictionary_checks(self):
        with patch.object(privacy.Path, "home", return_value=Path("/home") / "runner"):
            audit = privacy._Audit(private_terms=("Synthetic Private Meeting",))
        audit.scan_bytes(b"def background_runner(): pass", "live/jobs.py")
        self.assertFalse(audit.findings)
        audit.scan_bytes(self.private_path().encode(), "live/jobs.py")
        audit.scan_bytes(b"Synthetic Private Meeting", "live/meeting.py")
        self.assertTrue({"local_home_path", "private_term"} <= self.categories(audit.findings))

    def test_python_cache_and_raw_pyc_are_rejected_and_still_content_scanned(self):
        self.write("Contents/Resources/backend/__pycache__/private.pyc", self.private_path())
        self.write("Contents/Resources/copied.pyc", b"cached")
        findings = privacy.audit_bundle(self.root)
        self.assertTrue({"python_cache", "local_home_path"} <= self.categories(findings))

    def test_key_inside_compressed_zip_is_detected_without_echoing_value(self):
        key = "sk-" + "Ab9q7x2P" * 4
        self.write("Contents/Resources/assets.zip", self.zip_bytes({"config.txt": key}))
        findings = privacy.audit_bundle(self.root)
        self.assertIn("credential_pattern", self.categories(findings))
        self.assertNotIn(key, json.dumps(findings))
        self.assertTrue(any("assets.zip!config.txt" in item["location"] for item in findings))

    def test_historical_author_is_checked_after_current_identity_is_clean(self):
        self.repository()
        self.write("README.md", "Vera")
        old_email = "historical-private" + "@" + "mail.test"
        self.commit("Initial", GIT_AUTHOR_NAME="Private author", GIT_AUTHOR_EMAIL=old_email)
        self.write("README.md", "Vera release")
        self.commit("Public release")
        findings = privacy.audit_source(self.root, "HEAD")
        self.assertIn("git_identity", self.categories(findings))
        self.assertNotIn(old_email, json.dumps(findings))
        self.assertFalse(privacy.audit_source(self.root))

    def test_zip_metadata_and_sensitive_filenames_are_checked_without_echoing_them(self):
        key = "sk-" + "Ab9q7x2P" * 4
        archive_path = self.root / "release.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.comment = key.encode()
            entry = zipfile.ZipInfo("Vera.app/" + key + ".txt")
            entry.comment = self.private_path().encode()
            payload = key.encode()
            entry.extra = struct.pack("<HH", 0xCAFE, len(payload)) + payload
            archive.writestr(entry, b"safe")
        findings = privacy.audit_archive(archive_path)
        self.assertTrue({"credential_pattern", "local_home_path"} <= self.categories(findings))
        self.assertNotIn(key, json.dumps(findings))
        self.assertNotIn("private-release-person", json.dumps(findings))

    def test_macos_sidecars_are_metadata_and_still_block_private_content(self):
        def appledouble(payload):
            return (struct.pack(">II16sH", 0x00051607, 0x00020000, b"Mac OS X        ", 1)
                    + struct.pack(">III", 9, 38, len(payload)) + payload)
        archive = self.root / "release.zip"
        metadata = {"__MACOSX/Vera.app/._speech-probe.wav": appledouble(b"safe Finder metadata"),
                    "__MACOSX/Vera.app/._base_library.zip": appledouble(b"safe Finder metadata"),
                    "__MACOSX/Vera.app/backend/._vera-backend": appledouble(b"safe Finder metadata")}
        archive.write_bytes(self.zip_bytes(metadata))
        self.assertFalse(privacy.audit_archive(archive))
        metadata["__MACOSX/Vera.app/._base_library.zip"] = appledouble(self.private_path().encode())
        archive.write_bytes(self.zip_bytes(metadata))
        self.assertIn("local_home_path", self.categories(privacy.audit_archive(archive)))
        metadata["__MACOSX/Vera.app/._base_library.zip"] = appledouble(b"safe")[:-1]
        archive.write_bytes(self.zip_bytes(metadata))
        self.assertIn("archive_unreadable", self.categories(privacy.audit_archive(archive)))

    def test_historical_deleted_private_content_remains_release_blocking(self):
        self.repository()
        path = self.write("old.txt", self.private_path())
        self.commit("Initial")
        path.unlink()
        self.commit("Remove private content")
        findings = privacy.audit_source(self.root, "HEAD")
        self.assertIn("local_home_path", self.categories(findings))
        self.assertFalse(privacy.audit_source(self.root))

    def test_forbidden_settings_database_and_arbitrary_recordings_are_rejected(self):
        for name in ("providers.json", "credentials.json", "speech.json", ".env.live", "workbench.env",
                     "meeting.sqlite3", "private-key.pem", "AuthKey.p8"):
            self.write("Contents/Resources/" + name)
        self.write("Contents/Resources/meeting.wav", b"RIFFprivate recording")
        self.write("Contents/Resources/speech-probe-zh-CN.wav", b"renamed private recording")
        self.write("Contents/Resources/renamed.bin", b"RIFF\x00\x00\x00\x00WAVEprivate recording")
        findings = privacy.audit_bundle(self.root)
        self.assertEqual(sum(item["category"] == "forbidden_file" for item in findings), 8)
        self.assertEqual(sum(item["category"] == "unapproved_audio" for item in findings), 3)

    def test_only_known_audio_bytes_and_allowed_delivery_context_are_exempt(self):
        project = Path(__file__).resolve().parents[1]
        synthetic = project / "native/Sources/AudioCore/Resources/speech-probe-zh-CN.wav"
        fixture = project / "native/Tests/Fixtures/opus-in-m4a.m4a"
        self.assertEqual(hashlib.sha256(synthetic.read_bytes()).hexdigest(), privacy.APPROVED_AUDIO[synthetic.name])
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), privacy.APPROVED_AUDIO[fixture.name])
        self.write("Contents/Resources/" + synthetic.name, synthetic.read_bytes())
        self.assertFalse(privacy.audit_bundle(self.root))
        self.write("Contents/Resources/" + fixture.name, fixture.read_bytes())
        self.assertIn("unapproved_audio", self.categories(privacy.audit_bundle(self.root)))

    def test_third_party_license_attribution_is_allowed_but_secrets_are_not(self):
        attribution = "Copyright Example Contributor <maintainer" + "@" + "upstream.test>"
        path = self.write("third_party/Library/LICENSE", attribution)
        self.assertFalse(privacy.audit_bundle(self.root))
        path.write_text(attribution + "\n" + "-----BEGIN " + "PRIVATE KEY-----\nprivate")
        self.assertIn("credential_pattern", self.categories(privacy.audit_bundle(self.root)))

    def test_stdlib_zip_pyc_and_upstream_sample_paths_are_allowed(self):
        sample = "/" + "Users" + "/Barney/project/sample.py"
        self.write("Contents/Resources/base_library.zip", self.zip_bytes({"os.pyc": sample}))
        self.assertFalse(privacy.audit_bundle(self.root))

    def test_source_synthetic_secret_exemption_does_not_extend_to_production(self):
        self.repository()
        synthetic = 'api_key = "model-fixture-secret"\n'
        self.write("tests/test_provider.py", synthetic)
        self.write("live/provider.py", synthetic)
        self.git("add", ".")
        findings = privacy.audit_source(self.root)
        self.assertEqual(findings, [{"category": "credential_assignment", "location": "live/provider.py"}])

    def test_public_upstream_password_exemption_is_bound_to_value_and_origin(self):
        public_example = ("password" + " = " + repr("password1")).encode()
        audit = privacy._Audit()
        audit.scan_bytes(public_example, "backend!PYZ.pyz!pydantic.types", third_party=True, frozen=True)
        self.assertFalse(audit.findings)
        audit.scan_bytes(public_example, "backend!PYZ.pyz!live.provider", frozen=True)
        self.assertIn("credential_assignment", self.categories(audit.findings))
        audit = privacy._Audit()
        changed = ("password" + " = " + repr("different-private-password")).encode()
        audit.scan_bytes(changed, "backend!PYZ.pyz!pydantic.types", third_party=True, frozen=True)
        self.assertIn("credential_assignment", self.categories(audit.findings))
        audit = privacy._Audit()
        api_key = ("api_key" + " = " + repr("password1")).encode()
        audit.scan_bytes(api_key, "backend!PYZ.pyz!pydantic.types", third_party=True, frozen=True)
        self.assertIn("credential_assignment", self.categories(audit.findings))

    def test_templates_are_placeholders_but_actual_values_still_fail(self):
        audit = privacy._Audit()
        audit.scan_bytes(('SECRET' + ' = ' + repr('%(secret)s')).encode(), "backend!werkzeug.debug.tbtools", third_party=True)
        self.assertFalse(audit.findings)
        audit.scan_bytes(('SECRET' + ' = ' + repr('private-value-1234')).encode(), "backend!werkzeug.debug.tbtools", third_party=True)
        self.assertIn("credential_assignment", self.categories(audit.findings))

    def test_binary_runs_cannot_be_joined_into_a_fake_email_address(self):
        email_parts = b"random" + b"@" + b"upstream.test"
        broken = email_parts.replace(b"@", b"\xff@")
        audit = privacy._Audit()
        audit.scan_bytes(broken, "Contents/Resources/image.png")
        self.assertNotIn("personal_email", self.categories(audit.findings))
        audit.scan_bytes(email_parts, "Contents/MacOS/DesktopHost")
        self.assertIn("personal_email", self.categories(audit.findings))

    def test_explicit_test_url_userinfo_does_not_hide_real_test_email(self):
        audit = privacy._Audit(source=True)
        userinfo = "https://" + "name" + "@" + "feishu.cn/drive/example"
        audit.scan_bytes(userinfo.encode(), "tests/test_feishu_cli.py")
        self.assertFalse(audit.findings)
        audit.scan_bytes(("private-person" + "@" + "feishu.cn").encode(), "tests/test_feishu_cli.py")
        self.assertIn("personal_email", self.categories(audit.findings))

    def test_only_the_named_env_example_is_allowed_in_source(self):
        self.repository()
        self.write(".env.live.example", "API_KEY=\n")
        self.write(".env.example", "API_KEY=\n")
        self.git("add", ".")
        findings = privacy.audit_source(self.root)
        self.assertEqual(findings, [{"category": "forbidden_file", "location": ".env.example"}])

    def test_archive_path_traversal_and_escaping_symlinks_are_rejected(self):
        archive_path = self.root / "release.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../outside.txt", b"unsafe")
            link = zipfile.ZipInfo("Vera.app/escape")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../../outside")
            applications = zipfile.ZipInfo("Applications")
            applications.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(applications, "/Applications")
        findings = privacy.audit_archive(archive_path)
        self.assertEqual(self.categories(findings), {"unsafe_archive_path", "unsafe_symlink"})
        self.assertFalse((self.root.parent / "outside.txt").exists())

    def test_frozen_backend_requires_reader_and_extracts_only_raw_module_bytes(self):
        self.write("Contents/Resources/backend/vera-backend", privacy.COOKIE_MAGIC)
        with patch.object(privacy.importlib, "import_module", side_effect=ImportError):
            self.assertIn("frozen_reader_unavailable", self.categories(privacy.audit_bundle(self.root)))
        extracts = []
        class RawReader:
            toc = {"live.provider": (0, 0, 0)}
            def __init__(self, filename):
                pass
            def extract(self, name, raw=False):
                extracts.append(raw)
                return self_private_path.encode()
        self_private_path = self.private_path()
        self.write("Contents/Resources/backend/code.pyz", b"PYZ\x00synthetic")
        fake = SimpleNamespace(CArchiveReader=lambda filename: SimpleNamespace(toc={}), ZlibArchiveReader=RawReader)
        with patch.object(privacy.importlib, "import_module", return_value=fake):
            findings = privacy.audit_bundle(self.root)
        self.assertEqual(extracts, [True])
        self.assertIn("local_home_path", self.categories(findings))

    @unittest.skipUnless(importlib.util.find_spec("PyInstaller"), "build Python required for real PyInstaller archive regression")
    def test_real_carchive_and_pyz_compression_cannot_hide_key_or_home_path(self):
        # Build the documented on-disk format, not an executable fixture.
        import marshal
        pyz_content = zlib.compress(marshal.dumps(compile("value = " + repr(self.private_path()), "live/provider.py", "exec")))
        header_size = 12
        toc_offset = header_size + len(pyz_content)
        pyz = b"PYZ\x00" + importlib.util.MAGIC_NUMBER + struct.pack("!i", toc_offset)
        pyz += pyz_content + marshal.dumps([("live.provider", (0, header_size, len(pyz_content)))])
        key = "sk-" + "Ab9q7x2P" * 4
        entries = [("PYZ.pyz", pyz, "z"), ("config", key.encode(), "x")]
        payload, toc = b"", b""
        for name, content, kind in entries:
            compressed = zlib.compress(content)
            entry_name = name.encode() + b"\x00"
            length = 18 + len(entry_name)
            padding = (16 - length % 16) % 16
            toc += struct.pack("!IIIIBc", length + padding, len(payload), len(compressed), len(content), 1, kind.encode())
            toc += entry_name + b"\x00" * padding
            payload += compressed
        cookie_size = struct.calcsize("!8sIIII64s")
        cookie = struct.pack("!8sIIII64s", privacy.COOKIE_MAGIC, len(payload) + len(toc) + cookie_size,
                             len(payload), len(toc), 312, b"Python")
        self.write("Contents/Resources/backend/vera-backend", b"not executable\x00" + payload + toc + cookie)
        findings = privacy.audit_bundle(self.root)
        self.assertTrue({"credential_pattern", "local_home_path"} <= self.categories(findings), findings)
        self.assertNotIn("frozen_archive_unreadable", self.categories(findings))

    def test_private_terms_default_file_and_cli_output_are_sanitized(self):
        term = "Synthetic Private Meeting"
        self.write("Vera.app/Contents/Resources/notes.txt", term)
        terms = self.write("home/.config/vera/release-private-terms.txt", term + "\n")
        output = io.StringIO()
        with patch.object(privacy.Path, "home", return_value=self.root / "home"), contextlib.redirect_stdout(output):
            result = privacy.main(["--app", str(self.root / "Vera.app")])
        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.assertIn("private_term", self.categories(report["findings"]))
        self.assertNotIn(term, output.getvalue())
        self.assertNotIn(str(terms), output.getvalue())


if __name__ == "__main__":
    unittest.main()
