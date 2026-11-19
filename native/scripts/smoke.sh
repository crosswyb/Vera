#!/bin/zsh
set -euo pipefail
task_native_dir="${0:A:h:h}"
swift build --package-path "$task_native_dir"
task_bin_dir="$(swift build --package-path "$task_native_dir" --show-bin-path)"
mkdir -p "$task_native_dir/build"
swiftc -parse-as-library -swift-version 5 -I "$task_bin_dir" -L "$task_bin_dir" -lAudioCore \
  "$task_native_dir/scripts/CoreSmoke.swift" -o "$task_native_dir/build/AudioCoreSmoke"
"$task_native_dir/build/AudioCoreSmoke"
swiftc -parse-as-library -swift-version 5 "$task_native_dir/Sources/AudioCore/AudioEvent.swift" \
  "$task_native_dir/Sources/AudioCore/CaptureRecording.swift" "$task_native_dir/scripts/RecordingSmoke.swift" \
  -o "$task_native_dir/build/RecordingSmoke"
"$task_native_dir/build/RecordingSmoke"
