#!/usr/bin/env python3
"""Publish a verified Vera release to an existing private GitHub repository."""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import uuid


RECEIPT_FIELDS = {
    "product", "version", "build", "source_revision", "signed_mach_o_count",
    "app_notarization_id", "dmg_notarization_id", "app_stapled", "dmg_stapled",
    "gatekeeper_app", "gatekeeper_dmg", "privacy_gate",
}


def run(arguments):
    try:
        return subprocess.run(
            [str(argument) for argument in arguments], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        tool = Path(str(arguments[0])).name
        raise RuntimeError(f"Verification command {tool} failed (exit {error.returncode})") from None


def release_assets(directory):
    receipt = json.loads((directory / "release-verification.json").read_text())
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        raise ValueError("The release receipt must contain only the approved verification fields")
    if type(receipt["signed_mach_o_count"]) is not int or receipt["signed_mach_o_count"] < 1:
        raise ValueError("The release receipt must confirm signed executable files")
    if receipt.get("product") != "Vera" or receipt.get("privacy_gate") != "passed":
        raise ValueError("A Vera release with a successful privacy gate is required")
    version = receipt.get("version", "")
    build = receipt.get("build", "")
    revision = receipt.get("source_revision", "")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Invalid release version")
    if (not isinstance(build, str) or not re.fullmatch(r"[0-9]+", build)
            or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)):
        raise ValueError("Invalid release build or source revision")
    for key in ("app_stapled", "dmg_stapled"):
        if receipt.get(key) is not True:
            raise ValueError("Both notarization tickets must be stapled")
    for key in ("gatekeeper_app", "gatekeeper_dmg"):
        if receipt.get(key) != "accepted":
            raise ValueError("Both Gatekeeper checks must pass")
    for key in ("app_notarization_id", "dmg_notarization_id"):
        value = receipt.get(key)
        try:
            valid_id = isinstance(value, str) and str(uuid.UUID(value)) == value
        except ValueError:
            valid_id = False
        if not valid_id:
            raise ValueError("Both Apple notarization receipts are required")
    prefix = f"Vera-{version}-build{build}-apple-silicon"
    artifacts = [directory / f"{prefix}.{suffix}" for suffix in ("dmg", "zip")]
    checksums = directory / f"{prefix}.SHA256SUMS.txt"
    expected = []
    for artifact in artifacts:
        with artifact.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        expected.append(f"{digest}  {artifact.name}")
    if checksums.read_text().splitlines() != expected:
        raise ValueError("The release checksums do not match its two archives")
    return receipt, [*artifacts, checksums, directory / "release-verification.json"]


def verify_archived_app(archive, receipt):
    with tempfile.TemporaryDirectory(prefix="vera-release-", suffix=".noindex") as temporary:
        root = Path(temporary)
        run(["ditto", "-x", "-k", archive, root])
        app = root / "真实交互.app"
        if {path.name for path in root.glob("*.app")} != {app.name}:
            raise ValueError("The archive must contain only the Chinese-named Vera app")
        with (app / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        if (
            info.get("CFBundleName") != "真实交互"
            or info.get("CFBundleDisplayName") != "真实交互"
            or info.get("CFBundleShortVersionString") != receipt["version"]
            or info.get("CFBundleIdentifier") != "com.zsjh.workbench"
            or info.get("CFBundleVersion") != receipt["build"]
            or info.get("ZSJHSourceRevision") != receipt["source_revision"]
        ):
            raise ValueError("The archived app does not match the release receipt")
        run(["codesign", "--verify", "--deep", "--strict", app])
        run(["xcrun", "stapler", "validate", app])
        run(["spctl", "--assess", "--type", "execute", "--verbose=2", app])


def main():
    project = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--repo", required=True, help="Existing private owner/repository")
    parser.add_argument("--notes-file", type=Path, required=True)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", arguments.repo):
        parser.error("Expected owner/repository")
    directory = arguments.release_dir.resolve()
    notes = arguments.notes_file.resolve()
    if not notes.is_relative_to(project) or not notes.is_file():
        parser.error("Release notes must be a tracked file inside the source repository")
    run(["git", "-C", project, "ls-files", "--error-unmatch", notes.relative_to(project)])
    if run(["git", "-C", project, "status", "--porcelain"]):
        parser.error("Commit the verified source and release notes before publishing")
    receipt, assets = release_assets(directory)
    head = run(["git", "-C", project, "rev-parse", "HEAD"])
    run(["git", "-C", project, "merge-base", "--is-ancestor", receipt["source_revision"], head])
    changed = run([
        "git", "-C", project, "diff", "--name-only", receipt["source_revision"], head,
    ]).splitlines()
    if any(not path.startswith("docs/") for path in changed):
        parser.error("Runtime or packaging source changed after the app was built; rebuild it")
    privacy = [sys.executable, project / "tools/release_privacy.py"]
    run([
        *privacy, "--source-root", project, "--git-ref", "HEAD",
        "--output", directory / "privacy-before-publish-source.json",
    ])
    for artifact in assets[:2]:
        run([
            *privacy, "--archive", artifact,
            "--output", directory / f"privacy-before-publish-{artifact.suffix[1:]}.json",
        ])
    verify_archived_app(assets[1], receipt)
    run(["xcrun", "stapler", "validate", assets[0]])
    run(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=2", assets[0]])
    repository = json.loads(run(["gh", "repo", "view", arguments.repo, "--json", "visibility"]))
    if repository.get("visibility") != "PRIVATE":
        parser.error("Publishing is restricted to private GitHub repositories")
    tag = f"v{receipt['version']}-build{receipt['build']}"
    for ref in (tag, "main"):
        remote = run(["gh", "api", f"repos/{arguments.repo}/commits/{ref}", "--jq", ".sha"])
        if remote != head:
            parser.error("Push the verified source and release tag before uploading")
    run([
        "gh", "release", "create", tag, "--repo", arguments.repo, "--verify-tag",
        "--title", f"Vera · 真实交互 {receipt['version']} · build {receipt['build']}",
        "--notes-file", notes, *assets,
    ])
    uploaded = json.loads(run([
        "gh", "release", "view", tag, "--repo", arguments.repo, "--json", "assets,url",
    ]))
    remote_assets = {asset["name"]: asset for asset in uploaded["assets"]}
    if set(remote_assets) != {asset.name for asset in assets}:
        raise RuntimeError("The uploaded release asset list does not match its allowlist")
    for asset in assets:
        with asset.open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if remote_assets[asset.name].get("digest") != digest:
            raise RuntimeError("An uploaded release asset digest does not match")
    (directory / "delivery-verification.json").write_text(
        json.dumps({"tag": tag, "commit": head, "private": True,
                    "assets": sorted(remote_assets), "server_digests_match": True}, indent=2) + "\n"
    )
    print(f"Published {tag}; four assets verified, private repository confirmed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        # Captured child output can include a local account path or certificate
        # identity. Keep failure output limited to the safe exception category.
        raise SystemExit(f"Release stopped: {error}. No automatic retry.") from None
    except OSError:
        raise SystemExit("Release stopped: required input or command is unavailable. No automatic retry.") from None
