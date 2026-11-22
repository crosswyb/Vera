#!/bin/zsh
set -euo pipefail
task_native_dir="${0:A:h:h}"
task_fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/vera-capture-finish.XXXXXXXX")"
trap 'rm -rf -- "$task_fixture_dir"' EXIT
"${ZSJH_PYTHON:-python3}" - "$task_native_dir" "$task_fixture_dir" <<'PY'
from pathlib import Path
import sys

native, fixture = map(Path, sys.argv[1:])
source = (native / 'Sources/DesktopHost/DesktopHost.swift').read_text()
marker = '\n@main\nstruct DesktopHostMain {'
assert source.count(marker) == 1, 'Cannot isolate DesktopHost entry point'
source = source.split(marker)[0].replace('import AudioCore\n', '')
(fixture / 'CaptureFinishFixture.swift').write_text(
    source + '\n' + (native / 'scripts/CaptureFinishSmoke.swift').read_text())
PY
swiftc -parse-as-library -swift-version 5 -target arm64-apple-macos26.0 \
  "$task_native_dir"/Sources/AudioCore/*.swift "$task_fixture_dir/CaptureFinishFixture.swift" \
  -o "$task_fixture_dir/CaptureFinishSmoke"
"$task_fixture_dir/CaptureFinishSmoke"
