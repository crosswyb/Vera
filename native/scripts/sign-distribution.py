#!/usr/bin/env python3
"""Sign, notarize, staple and verify a fresh copy of the desktop distribution.

Signing identities and notary credentials must already be in a keychain. This
script never reads a private key, imports credentials, or changes the installed
application. Credentials and release binaries stay outside the source tree.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


MACH_O_MAGICS = {
    bytes.fromhex(value)
    for value in (
        "feedface", "cefaedfe", "feedfacf", "cffaedfe",
        "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
    )
}


def run(arguments):
    subprocess.run([str(value) for value in arguments], check=True)


def notarize(artifact, label, arguments, output):
    authentication = ["--keychain-profile", arguments.notary_profile]
    if arguments.keychain:
        authentication += ["--keychain", str(arguments.keychain)]
    print(f"Submitting {label} to Apple notarization", flush=True)
    command = [
        "xcrun", "notarytool", "submit", str(artifact), *authentication,
        "--wait", "--timeout", "20m", "--output-format", "json",
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    # A profile-based command contains no plaintext Apple account credentials.
    if completed.stderr:
        print(completed.stderr, flush=True)
    try:
        receipt = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"No JSON receipt for {label}; exit {completed.returncode}") from None
    (output / f"{label}-notarization.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    )
    submission = receipt.get("id")
    if submission:
        run([
            "xcrun", "notarytool", "log", submission, *authentication,
            output / f"{label}-notarization-log.json",
        ])
    if completed.returncode or receipt.get("status") != "Accepted":
        raise RuntimeError(
            f"Apple has not accepted {label}: {receipt.get('status', 'unknown')}; "
            f"submission {submission}. Inspect the receipt and log before retrying."
        )
    print(f"Apple accepted {label}: {submission}", flush=True)
    return receipt


def main():
    native = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=native / "build/真实交互.app")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--identity", required=True, help="Developer ID Application identity or SHA-1")
    parser.add_argument("--keychain", type=Path)
    parser.add_argument("--notary-profile", required=True)
    arguments = parser.parse_args()
    source = arguments.app.resolve()
    output = arguments.output_dir.resolve()
    with (source / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != "com.zsjh.workbench":
        parser.error("Expected the Vera desktop app bundle")
    if info.get("CFBundleName") != "真实交互" or info.get("CFBundleDisplayName") != "真实交互":
        parser.error("The Vera app must use the Chinese application name")
    if output.exists() and any(output.iterdir()):
        parser.error("Choose an empty output directory; releases are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    project = native.parent
    build_python = Path(os.environ.get("ZSJH_BUILD_PYTHON", project / ".venv-live/bin/python"))
    if not build_python.is_file():
        build_python = Path(sys.executable)
    privacy_command = [build_python, project / "tools/release_privacy.py"]
    run([
        *privacy_command, "--source-root", project, "--git-ref", "HEAD", "--app", source,
        "--output", output / "privacy-before-signing.json",
    ])
    revision = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty or info.get("ZSJHSourceRevision") != revision:
        parser.error("Commit the source and rebuild the app before signing")
    identity_args = ["security", "find-identity", "-v", "-p", "codesigning"]
    if arguments.keychain:
        identity_args.append(str(arguments.keychain))
    identities = subprocess.run(identity_args, capture_output=True, text=True, check=True).stdout
    if "Developer ID Application:" not in identities or arguments.identity not in identities:
        parser.error("The selected keychain has no matching Developer ID Application identity")

    with tempfile.TemporaryDirectory(prefix="signing-", suffix=".noindex", dir=output) as temporary:
        stage = Path(temporary)
        app = stage / "真实交互.app"
        run(["ditto", source, app])
        signing = ["codesign", "--force", "--sign", arguments.identity, "--options", "runtime", "--timestamp"]
        if arguments.keychain:
            signing += ["--keychain", str(arguments.keychain)]
        binaries = []
        for path in app.rglob("*"):
            if path.is_symlink():
                if not path.resolve().is_relative_to(app):
                    raise RuntimeError(f"App contains an external symlink: {path.relative_to(app)}")
                continue
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                if stream.read(4) in MACH_O_MAGICS:
                    binaries.append(path)
        for binary in sorted(binaries, key=lambda path: len(path.parts), reverse=True):
            run([*signing, binary])
        for helper in sorted(app.rglob("*.app"), key=lambda path: len(path.parts), reverse=True):
            run([*signing, "--entitlements", native / "Signing.entitlements", helper])
        run([*signing, "--entitlements", native / "Signing.entitlements", app])
        run(["codesign", "--verify", "--deep", "--strict", app])
        run([*privacy_command, "--app", app, "--output", output / "privacy-signed-app.json"])

        submission_zip = stage / "application-for-notarization.zip"
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, submission_zip])
        app_receipt = notarize(submission_zip, "app", arguments, output)
        run(["xcrun", "stapler", "staple", app])
        run(["xcrun", "stapler", "validate", app])
        run(["spctl", "--assess", "--type", "execute", "--verbose=2", app])

        run(["/bin/zsh", native / "scripts/package-distribution.sh", app, output])
        name = f"Vera-{info['CFBundleShortVersionString']}-build{info['CFBundleVersion']}-apple-silicon"
        dmg = output / f"{name}.dmg"
        disk_signing = ["codesign", "--force", "--sign", arguments.identity, "--timestamp"]
        if arguments.keychain:
            disk_signing += ["--keychain", str(arguments.keychain)]
        run([*disk_signing, dmg])
        dmg_receipt = notarize(dmg, "dmg", arguments, output)
        run(["xcrun", "stapler", "staple", dmg])
        run(["xcrun", "stapler", "validate", dmg])
        run(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=2", dmg])
        run(["hdiutil", "verify", "-quiet", dmg])
        for suffix in ("dmg", "zip"):
            run([
                *privacy_command, "--archive", output / f"{name}.{suffix}",
                "--output", output / f"privacy-final-{suffix}.json",
            ])
        checksums = []
        for suffix in ("dmg", "zip"):
            artifact = output / f"{name}.{suffix}"
            with artifact.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            checksums.append(f"{digest}  {artifact.name}")
        (output / f"{name}.SHA256SUMS.txt").write_text("\n".join(checksums) + "\n")
        receipt = {
            "product": "Vera",
            "version": info["CFBundleShortVersionString"],
            "build": info["CFBundleVersion"],
            "source_revision": info.get("ZSJHSourceRevision"),
            "signed_mach_o_count": len(binaries),
            "app_notarization_id": app_receipt["id"],
            "dmg_notarization_id": dmg_receipt["id"],
            "app_stapled": True,
            "dmg_stapled": True,
            "gatekeeper_app": "accepted",
            "gatekeeper_dmg": "accepted",
            "privacy_gate": "passed",
        }
        (output / "release-verification.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
