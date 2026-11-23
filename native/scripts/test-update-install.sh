#!/bin/zsh
set -euo pipefail
task_native_dir="${0:A:h:h}"
task_fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/vera-update-install.XXXXXXXX")"
task_server_pid=''
cleanup() {
  if [[ -n "$task_server_pid" ]]; then
    kill "$task_server_pid" 2>/dev/null || true
    wait "$task_server_pid" 2>/dev/null || true
  fi
  rm -rf -- "$task_fixture_dir"
}
trap cleanup EXIT
"${ZSJH_PYTHON:-python3}" "$task_native_dir/scripts/update_download_fixture.py" "$task_fixture_dir" &
task_server_pid=$!
for task_attempt in {1..100}; do
  [[ -s "$task_fixture_dir/download-server.json" ]] && break
  kill -0 "$task_server_pid"
  sleep 0.05
done
test -s "$task_fixture_dir/download-server.json"
swiftc -parse-as-library -swift-version 5 \
  "$task_native_dir/Sources/DesktopHost/VeraRelease.swift" \
  "$task_native_dir/Sources/DesktopHost/VeraUpdatePackage.swift" \
  "$task_native_dir/Sources/DesktopHost/VeraUpdateInstaller.swift" \
  "$task_native_dir/scripts/UpdateInstallSmoke.swift" \
  -o "$task_fixture_dir/UpdateInstallSmoke"
# Match Finder launches that have no locale variables.
/usr/bin/env -u LANG -u LC_ALL -u LC_CTYPE "$task_fixture_dir/UpdateInstallSmoke" "$task_fixture_dir"
