#!/bin/zsh
set -euo pipefail
task_native_dir="${0:A:h:h}"
swift build --package-path "$task_native_dir"
task_bin_dir="$(swift build --package-path "$task_native_dir" --show-bin-path)"
task_app_dir="$task_native_dir/build/ZSJHAudioHelper.app"
mkdir -p "$task_app_dir/Contents/MacOS" "$task_app_dir/Contents/Resources"
cp "$task_native_dir/Info.plist" "$task_app_dir/Contents/Info.plist"
cp "$task_bin_dir/AudioHelper" "$task_app_dir/Contents/MacOS/AudioHelper"
cp "$task_native_dir/../third_party/cicada/LICENSE" "$task_app_dir/Contents/Resources/Cicada-LICENSE"
cp "$task_native_dir/../third_party/cicada/UPSTREAM.md" "$task_app_dir/Contents/Resources/Cicada-UPSTREAM.md"
codesign --force --sign - --identifier com.zsjh.audiohelper "$task_app_dir"
print -r -- "$task_app_dir"
