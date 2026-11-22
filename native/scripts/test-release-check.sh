#!/bin/zsh
set -euo pipefail
task_native_dir="${0:A:h:h}"
task_fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/vera-release-check.XXXXXXXX")"
trap 'rm -rf -- "$task_fixture_dir"' EXIT
swiftc -parse-as-library -swift-version 5 \
  "$task_native_dir/Sources/DesktopHost/VeraRelease.swift" "$task_native_dir/scripts/ReleaseCheckSmoke.swift" \
  -o "$task_fixture_dir/ReleaseCheckSmoke"
"$task_fixture_dir/ReleaseCheckSmoke" "$@"
