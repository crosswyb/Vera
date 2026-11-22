#!/usr/bin/env python3
"""Offline privacy gate for Vera source and distributions.

Findings contain locations and fixed category labels, never matched values.
Use the build Python when inspecting a frozen backend: PyInstaller's own
archive reader is required, and its absence is a release-blocking finding.
Frozen code is only read as bytes; it is never imported or executed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import plistlib
import re
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


APPROVED_AUDIO = {
    "speech-probe-zh-CN.wav": "381dcd158ed6f6e17effd0676cfc4664c9aabf4911da2597a46d71ce6bb3a2d4",
    "opus-in-m4a.m4a": "a5ef13f531af43e9369629b8753fbe90081405a30b547f1baf8d79f9e0874483",
}
AUDIO_SUFFIXES = {
    ".wav", ".wave", ".mp3", ".m4a", ".aac", ".aiff", ".aif", ".aifc", ".flac",
    ".ogg", ".opus", ".caf", ".pcm", ".amr", ".mp4", ".webm", ".mov", ".mkv",
}
HOME_PATH = re.compile(r"/(?:Users|home)/([^/\s\x00\"'<>`\\:;,()\[\]{}]+)")
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9_.+-]*@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?![\w.-])")
PLACEHOLDER_USERS = {"user", "username", "your-user", "your-username", "example", "developer", "dev", "you", "name", "用户名", "你的用户名"}
BUILD_USERS = {"runner", "Barney"}
SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN PGP (?:PRIVATE) KEY BLOCK-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:[A-Z0-9]+_)*(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret)"
    r"[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']"
)
COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
# These are the two public password examples in Pydantic's SecretStr docs.
# Fingerprints keep the exception specific to reviewed values and module origin;
# a different password, API token or private key in the same module still fails.
PUBLIC_PASSWORD_EXAMPLES = {
    "pydantic.types": {
        "0b14d501a594442a01c6859541bcb3e8164d183d32937b851835442f69d5c94e",
        "5e6e45c8d3a5d85e6a63db9b9cab4ada159c5edcef70e83026ec5d71325ed6be",
    },
}


def _example_email(value: str) -> bool:
    domain = value.rsplit("@", 1)[-1].lower()
    return (domain in {"example.com", "example.org", "example.net", "vera.invalid"}
            or domain.endswith(".invalid") or domain.endswith(".example")
            or domain == "users.noreply.github.com")


def _synthetic_source(location: str) -> bool:
    path = _content_path(location)
    return ("tests" in path.parts or "Tests" in path.parts
            or path.name.startswith(("test_", "probe_"))
            or path.name.endswith(("_fixture.py", "Smoke.swift")))


def _synthetic_value(value: str) -> bool:
    return bool(re.search(r"(?i)(?:^|[-_ ])(?:fixture|synthetic|dummy|fake|test)(?:$|[-_ ])", value))


def _placeholder_value(value: str) -> bool:
    return (value.lower() in {"changeme", "change-me", "replace-me", "not-set", "none", "null"}
            or value.startswith(("${", "<", "YOUR_", "your-", "your_"))
            or bool(re.fullmatch(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", value)))


def _content_path(location: str) -> PurePosixPath:
    location = re.sub(r"^git:[0-9a-f]{12}:", "", location)
    return PurePosixPath(location.rsplit("!", 1)[-1])


def _placeholder_user(user: str) -> bool:
    return user.casefold() in PLACEHOLDER_USERS or user.startswith(("$", "%"))


def _public_password_example(match: re.Match[str], location: str, third_party: bool) -> bool:
    if not third_party or not match.group().lower().startswith("password"):
        return False
    approved = PUBLIC_PASSWORD_EXAMPLES.get(_content_path(location).name, set())
    return hashlib.sha256(match.group(1).encode()).hexdigest() in approved


class _Audit:
    def __init__(self, source: bool = False, private_terms: tuple[str, ...] = ()):
        self.source = source
        self.private_terms = private_terms
        self.findings: list[dict[str, str]] = []
        self._seen: set[tuple[str, str]] = set()
        # Hosted CI and root accounts are infrastructure identities. Matching
        # every occurrence of "runner" would reject ordinary function names;
        # explicit personal paths and the private dictionary are still scanned.
        home_user = Path.home().name
        self.home_user = "" if home_user in {"runner", "root"} else home_user

    def add(self, category: str, location: str) -> None:
        # Archive member names can themselves contain private values.
        safe = HOME_PATH.sub("/<home>/<redacted>", location)
        safe = EMAIL.sub("<redacted-email>", safe)
        for pattern in SECRET_PATTERNS:
            safe = pattern.sub("<redacted-credential>", safe)
        safe = SECRET_ASSIGNMENT.sub("<redacted-credential>", safe)
        for term in (*self.private_terms, self.home_user):
            if len(term) > 2:
                safe = re.sub(re.escape(term), "<private>", safe, flags=re.IGNORECASE)
        safe = "".join(char if char.isprintable() else "?" for char in safe)[:600]
        key = (category, safe)
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append({"category": category, "location": safe})

    def scan_text(self, data: bytes, location: str, *, third_party: bool = False) -> None:
        # Replacement characters keep separate binary runs from being joined
        # into a fictitious email address after invalid UTF-8 bytes are removed.
        text = data.decode("utf-8", errors="replace")
        if not third_party:
            if any(not _placeholder_user(match.group(1)) for match in HOME_PATH.finditer(text)):
                self.add("local_home_path", location)
            for match in EMAIL.finditer(text):
                synthetic_userinfo = (self.source and _synthetic_source(location)
                                      and match.group().split("@", 1)[0] in {"name", "user", "test"}
                                      and text[:match.start()].endswith(("https://", "http://")))
                if not _example_email(match.group()) and not synthetic_userinfo:
                    self.add("personal_email", location)
                    break
        else:
            # Common upstream build/sample paths are not personal project data.
            for match in HOME_PATH.finditer(text):
                if not _placeholder_user(match.group(1)) and match.group(1) not in BUILD_USERS:
                    self.add("local_home_path", location)
                    break
        for term in self.private_terms:
            if (term.casefold() in text.casefold()
                    or any(term.encode(encoding) in data for encoding in ("utf-16-le", "utf-16-be"))):
                self.add("private_term", location)
        # Native strings and macOS metadata can use UTF-16 as well as UTF-8.
        if len(self.home_user) > 2 and not third_party:
            if any(self.home_user.encode(encoding) in data for encoding in ("utf-8", "utf-16-le", "utf-16-be")):
                self.add("local_user_identifier", location)
        elif len(self.home_user) > 2:
            # A third-party license/build-path exemption never permits our user's bytes.
            if any(self.home_user.encode(encoding) in data for encoding in ("utf-8", "utf-16-le", "utf-16-be")):
                self.add("local_user_identifier", location)
        synthetic = self.source and _synthetic_source(location)
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                if not (synthetic and _synthetic_value(match.group())):
                    self.add("credential_pattern", location)
        for match in SECRET_ASSIGNMENT.finditer(text):
            value = match.group(1)
            if (not _placeholder_value(value) and not (synthetic and _synthetic_value(value))
                    and not _public_password_example(match, location, third_party)):
                self.add("credential_assignment", location)

    def scan_bytes(self, data: bytes, location: str, *, depth: int = 0,
                   third_party: bool = False, frozen: bool = False, base_library: bool = False) -> None:
        path = _content_path(location)
        name = path.name.lower()
        forbidden = (
            (name == ".env" or name.startswith(".env."))
            and not (self.source and name == ".env.live.example")
        )
        forbidden = forbidden or name in {
            "credentials.json", "providers.json", "workbench.env", "speech.json", "speech-settings.json",
            "feishu-sync.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".ds_store",
        }
        forbidden = forbidden or path.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".p8", ".p12", ".pfx", ".key", ".keystore", ".jks"}
        forbidden = forbidden or (path.suffix.lower() == ".pem"
                                  and bool(re.search(r"(?:^|[-_])(?:private|privkey|key|secret)(?:$|[-_])", path.stem.lower())))
        if forbidden or data.startswith(b"SQLite format 3\x00"):
            self.add("forbidden_file", location)
        if "__pycache__" in path.parts or (path.suffix.lower() == ".pyc" and not (frozen or base_library)):
            self.add("python_cache", location)
        media_header = (data.startswith((b"ID3", b"fLaC", b"OggS", b"caff", b"#!AMR"))
                        or (data.startswith(b"RIFF") and data[8:12] == b"WAVE")
                        or (data.startswith(b"FORM") and data[8:12] in {b"AIFF", b"AIFC"})
                        or data[4:8] == b"ftyp")
        if path.suffix.lower() in AUDIO_SUFFIXES or media_header:
            expected = APPROVED_AUDIO.get(path.name)
            if (expected is None or hashlib.sha256(data).hexdigest() != expected
                    or (not self.source and path.name != "speech-probe-zh-CN.wav")):
                self.add("unapproved_audio", location)
        self.scan_text(data, location, third_party=third_party)
        if data.startswith(b"PK\x03\x04") or path.suffix.lower() == ".zip":
            self.scan_zip(data, location, depth=depth, third_party=third_party,
                          base_library=path.name == "base_library.zip")
        if data.startswith(b"PYZ\x00") or COOKIE_MAGIC in data:
            self.scan_frozen(data, location, depth=depth)
        elif "backend" in path.parts and path.name.endswith("-backend"):
            self.add("frozen_archive_unreadable", location)

    def scan_zip(self, data: bytes, location: str, *, depth: int,
                 third_party: bool = False, base_library: bool = False) -> None:
        if depth >= 6:
            self.add("archive_limit", location)
            return
        try:
            with zipfile.ZipFile(io.BytesIO(data), metadata_encoding="utf-8") as archive:
                self.scan_text(archive.comment, f"{location}!archive-metadata")
                members = archive.infolist()
                if sum(member.file_size for member in members) > MAX_ARCHIVE_BYTES:
                    self.add("archive_limit", location)
                    return
                for member in members:
                    member_location = f"{location}!{member.filename}"
                    self.scan_text(member.filename.encode("utf-8"), f"{member_location}!name")
                    self.scan_text(member.comment + member.extra, f"{member_location}!member-metadata")
                    if not _safe_archive_name(member.filename):
                        self.add("unsafe_archive_path", member_location)
                        continue
                    if member.is_dir():
                        continue
                    if member.file_size > MAX_MEMBER_BYTES or member.flag_bits & 1:
                        self.add("archive_unreadable", member_location)
                        continue
                    content = archive.read(member)
                    if stat.S_ISLNK(member.external_attr >> 16):
                        self.scan_link(content.decode("utf-8", errors="replace"), member.filename, member_location)
                        continue
                    if member.filename.startswith("__MACOSX/") and PurePosixPath(member.filename).name.startswith("._"):
                        self.scan_appledouble(content, member_location, depth=depth + 1,
                                              third_party=_third_party(member.filename))
                        continue
                    self.scan_bytes(content, member_location, depth=depth + 1,
                                    third_party=third_party or base_library or _third_party(member.filename),
                                    base_library=base_library)
        except Exception:
            # Decoders for ZIP's supported compression methods raise different
            # exception classes. Malformed release input must always fail closed.
            self.add("archive_unreadable", location)

    def scan_appledouble(self, data: bytes, location: str, *, depth: int, third_party: bool) -> None:
        # Apple copyfile's big-endian AppleDouble header stores Finder metadata
        # and resource forks separately from the associated file's actual data.
        self.scan_text(data, location, third_party=third_party)
        if len(data) < 26 or data[:8] != bytes.fromhex("0005160700020000"):
            self.add("archive_unreadable", location)
            return
        count = struct.unpack_from(">H", data, 24)[0]
        header_end = 26 + 12 * count
        if header_end > len(data):
            self.add("archive_unreadable", location)
            return
        for index in range(count):
            kind, start, size = struct.unpack_from(">III", data, 26 + 12 * index)
            if start < header_end or start + size > len(data):
                self.add("archive_unreadable", location)
                return
            if size:
                self.scan_bytes(data[start:start + size], f"{location}!metadata-{kind}",
                                depth=depth, third_party=third_party)

    def scan_frozen(self, data: bytes, location: str, *, depth: int) -> None:
        if depth >= 6:
            self.add("archive_limit", location)
            return
        try:
            readers = importlib.import_module("PyInstaller.archive.readers")
        except ImportError:
            self.add("frozen_reader_unavailable", location)
            return
        try:
            with tempfile.TemporaryDirectory(prefix="vera-privacy-frozen-") as directory:
                archive_path = Path(directory) / "archive"
                archive_path.write_bytes(data)
                if data.startswith(b"PYZ\x00"):
                    reader = readers.ZlibArchiveReader(str(archive_path))
                    for name in reader.toc:
                        content = reader.extract(name, raw=True)
                        if content is not None:
                            self.scan_bytes(content, f"{location}!{name}", depth=depth + 1,
                                            third_party=not _first_party_module(name), frozen=True)
                else:
                    reader = readers.CArchiveReader(str(archive_path))
                    for name in reader.toc:
                        content = reader.extract(name)
                        self.scan_bytes(content, f"{location}!{name}", depth=depth + 1,
                                        third_party=not _first_party_module(name), frozen=True)
        except Exception:
            # Includes malformed marshal/struct/zlib data from archive readers.
            self.add("frozen_archive_unreadable", location)

    def scan_link(self, target: str, member_name: str, location: str) -> None:
        self.scan_text(target.encode("utf-8"), location)
        if target == "/Applications" and PurePosixPath(member_name).name == "Applications":
            return
        if target.startswith(("/", "\\")) or "\\" in target:
            self.add("unsafe_symlink", location)
            return
        parts = list(PurePosixPath(member_name).parent.parts)
        for part in PurePosixPath(target).parts:
            if part == "..":
                if not parts:
                    self.add("unsafe_symlink", location)
                    return
                parts.pop()
            elif part != ".":
                parts.append(part)

    def scan_tree(self, root: Path) -> None:
        if not root.is_dir():
            self.add("missing_input", "bundle")
            return
        backend_seen = False
        frozen_seen = False
        def unreadable_directory(error: OSError) -> None:
            try:
                location = Path(error.filename or root).relative_to(root).as_posix()
            except ValueError:
                location = "bundle"
            self.add("unreadable_file", location)

        for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable_directory):
            for name in sorted(dirs + files):
                path = Path(directory) / name
                location = path.relative_to(root).as_posix()
                self.scan_text(location.encode("utf-8"), f"{location}!name")
                if "__pycache__" in path.parts:
                    self.add("python_cache", location)
                try:
                    if path.is_symlink():
                        self.scan_link(os.readlink(path), location, location)
                    elif path.is_file():
                        content = path.read_bytes()
                        if "backend" in path.parts and (name.endswith("-backend") or name == "backend"):
                            backend_seen = True
                        if COOKIE_MAGIC in content or content.startswith(b"PYZ\x00"):
                            frozen_seen = True
                        self.scan_bytes(content, location, third_party=_third_party(location))
                    if hasattr(os, "listxattr"):
                        for attribute in os.listxattr(path, follow_symlinks=False):
                            content = os.getxattr(path, attribute, follow_symlinks=False)
                            self.scan_text(content, f"{location}!metadata", third_party=_third_party(location))
                except OSError:
                    self.add("unreadable_file", location)
        if backend_seen and not frozen_seen:
            self.add("frozen_archive_unreadable", "Contents/Resources/backend")


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts and "\\" not in name and ":" not in path.parts[0]


def _third_party(location: str) -> bool:
    lowered = location.lower()
    parts = PurePosixPath(lowered).parts
    if "third_party" in parts or "thirdparty" in parts:
        return True
    return ("_internal" in parts or ".framework/" in lowered or lowered.endswith((".dylib", ".so"))
            or "base_library.zip!" in lowered)


def _first_party_module(name: str) -> bool:
    return (name in {"live", "live_api", "app", "workbench_backend_entry"}
            or name.startswith(("live.", "live/", "tools.workbench_backend_entry")))


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def _identity_ok(name: str, email: str) -> bool:
    return ((name == "Vera Contributors" and email == "dev@vera.invalid")
            or bool(re.fullmatch(r"[A-Za-z0-9+_.-]+@users\.noreply\.github\.com", email)))


def _history(root: Path, ref: str, audit: _Audit) -> None:
    try:
        # Resolve first so an argument cannot be interpreted as an option.
        revision = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
        records = _git(root, "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00", revision).split(b"\x00")
        reachable = set()
        for offset in range(0, len(records) - 6, 6):
            commit, author, author_email, committer, committer_email, message = [value.decode("utf-8", errors="replace") for value in records[offset:offset + 6]]
            commit = commit.strip()
            reachable.add(commit)
            for kind, name, email in (("author", author, author_email), ("committer", committer, committer_email)):
                if not _identity_ok(name, email):
                    audit.add("git_identity", f"git:{commit[:12]}:{kind}")
            audit.scan_text(message.encode(), f"git:{commit[:12]}:message")
        revisions = [revision]
        tags = _git(root, "for-each-ref", "--format=%(objecttype)%00%(objectname)%00%(*objectname)%00%(taggername)%00%(taggeremail)", "refs/tags")
        for line in tags.decode("utf-8", errors="replace").splitlines():
            kind, object_id, target, name, email = line.split("\x00", 4)
            if kind == "tag" and target in reachable:
                revisions.append(object_id)
                if not _identity_ok(name, email.strip("<>")):
                    audit.add("git_identity", f"git:{object_id[:12]}:tagger")
        objects = _git(root, "-c", "core.quotepath=false", "rev-list", "--objects", *revisions).splitlines()
        with subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            assert process.stdin is not None and process.stdout is not None
            for entry in objects:
                object_id, _, raw_path = entry.partition(b" ")
                process.stdin.write(object_id + b"\n")
                process.stdin.flush()
                header = process.stdout.readline().split()
                if len(header) != 3:
                    raise ValueError("unreadable git object")
                content = process.stdout.read(int(header[2]))
                process.stdout.read(1)
                if header[1] == b"blob":
                    location = raw_path.decode("utf-8", errors="replace") or "object"
                    audit.scan_bytes(content, f"git:{object_id[:12].decode()}:{location}", third_party=_third_party(location))
                elif header[1] == b"tag":
                    # The tagger identity was checked above; scan its message too.
                    message = content.partition(b"\n\n")[2]
                    audit.scan_text(message, f"git:{object_id[:12].decode()}:tag-message")
            process.stdin.close()
            if process.wait() != 0:
                raise ValueError("git reader failed")
    except (OSError, subprocess.SubprocessError, ValueError, BrokenPipeError):
        audit.add("git_audit_failed", "git:history")


def audit_source(root: str | Path, history_ref: str | None = None, *,
                 private_terms: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Check tracked working-tree files and, when requested, reachable history."""
    root = Path(root)
    audit = _Audit(source=True, private_terms=private_terms)
    try:
        paths = _git(root, "ls-files", "-z").split(b"\x00")
        for raw_path in paths:
            if not raw_path:
                continue
            location = raw_path.decode("utf-8", errors="replace")
            path = root / location
            # A tracked deletion is safe and will be represented by the next commit.
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink():
                audit.scan_link(os.readlink(path), location, location)
            elif path.is_file():
                audit.scan_bytes(path.read_bytes(), location, third_party=_third_party(location))
        if history_ref is not None:
            _history(root, history_ref, audit)
    except (OSError, subprocess.SubprocessError):
        audit.add("git_audit_failed", "git:source")
    return audit.findings


def audit_bundle(app: str | Path, *, private_terms: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Inspect every file, safe symlink, archive member and macOS metadata value."""
    audit = _Audit(private_terms=private_terms)
    audit.scan_tree(Path(app))
    return audit.findings


def audit_archive(path: str | Path, *, private_terms: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Read ZIP members in memory or mount a DMG read-only; never execute content."""
    path = Path(path)
    audit = _Audit(private_terms=private_terms)
    if not path.is_file():
        audit.add("missing_input", "archive")
    elif path.suffix.lower() == ".zip":
        # In-memory member reads avoid writing attacker-controlled archive paths.
        try:
            audit.scan_zip(path.read_bytes(), "archive.zip", depth=0)
        except OSError:
            audit.add("archive_unreadable", "archive.zip")
    elif path.suffix.lower() == ".dmg" and sys.platform == "darwin":
        mounted = False
        with tempfile.TemporaryDirectory(prefix="vera-privacy-dmg-") as directory:
            mount = (Path(directory) / "volume").resolve()
            mount.mkdir()
            try:
                result = subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-plist",
                                         "-mountpoint", str(mount), str(path.resolve())],
                                        check=True, capture_output=True)
                metadata = plistlib.loads(result.stdout)
                mounted = any(entity.get("mount-point") == str(mount) for entity in metadata.get("system-entities", []))
                if not mounted:
                    audit.add("archive_unreadable", "archive.dmg")
                else:
                    audit.scan_tree(mount)
            except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
                audit.add("archive_unreadable", "archive.dmg")
            finally:
                if mounted or os.path.ismount(mount):
                    result = subprocess.run(["hdiutil", "detach", str(mount)], capture_output=True)
                    if result.returncode:
                        audit.add("archive_detach_failed", "archive.dmg")
    else:
        audit.add("unsupported_archive", "archive")
    return audit.findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--git-ref", help="Also scan history reachable from this commit or tag (normally HEAD).")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--archive", action="append", default=[], type=Path)
    parser.add_argument("--output", type=Path, help="Write the sanitized JSON report to this file as well.")
    parser.add_argument("--private-terms-file", type=Path, help="Local, untracked UTF-8 file; one private term per line.")
    arguments = parser.parse_args(argv)
    if not (arguments.source_root or arguments.app or arguments.archive):
        parser.error("provide --source-root, --app or --archive")
    if arguments.git_ref and not arguments.source_root:
        parser.error("--git-ref requires --source-root")
    default_terms_path = Path.home() / ".config" / "vera" / "release-private-terms.txt"
    terms_path = (arguments.private_terms_file or os.environ.get("VERA_PRIVATE_TERMS_FILE")
                  or (default_terms_path if default_terms_path.is_file() else None))
    try:
        private_terms = tuple(line.strip() for line in Path(terms_path).read_text(encoding="utf-8").splitlines()
                              if line.strip() and not line.lstrip().startswith("#")) if terms_path else ()
    except (OSError, UnicodeError):
        print(json.dumps({"ok": False, "findings": [{"category": "private_terms_unreadable", "location": "private-terms"}]}))
        return 1
    findings = []
    if arguments.source_root:
        findings.extend(audit_source(arguments.source_root, arguments.git_ref, private_terms=private_terms))
    if arguments.app:
        findings.extend(audit_bundle(arguments.app, private_terms=private_terms))
    for archive in arguments.archive:
        findings.extend(audit_archive(archive, private_terms=private_terms))
    report = json.dumps({"ok": not findings, "findings": findings}, ensure_ascii=False, indent=2)
    if arguments.output:
        try:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(report + "\n", encoding="utf-8")
        except OSError:
            findings.append({"category": "report_write_failed", "location": "output-report"})
            report = json.dumps({"ok": False, "findings": findings}, ensure_ascii=False, indent=2)
    print(report)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
