#!/bin/zsh
set -euo pipefail
native_dir="${0:A:h:h}"
fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/zsjh-asr-smoke.XXXXXXXX")"
trap 'rm -rf -- "$fixture_dir"' EXIT
swiftc -parse-as-library -swift-version 5 -target arm64-apple-macos26.0 \
  "$native_dir"/Sources/AudioCore/*.swift "$native_dir/scripts/CloudASRSmoke.swift" \
  -o "$fixture_dir/CloudASRSmoke"
"$fixture_dir/CloudASRSmoke"
