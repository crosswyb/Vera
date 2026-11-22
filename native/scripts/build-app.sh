#!/bin/zsh
set -euo pipefail
task_native_dir="${0:A:h:h}"
task_invoked_project_dir="${0:a:h:h:h}"
task_project_dir="${task_native_dir:h}"
task_build_flags=(-c release -Xswiftc -gnone)
# Map both spellings when the checkout is reached through a symlink.
for task_source_dir in "$task_invoked_project_dir" "$task_project_dir"; do
  task_build_flags+=(-Xswiftc -file-prefix-map -Xswiftc "$task_source_dir=/Vera")
  task_build_flags+=(-Xswiftc -debug-prefix-map -Xswiftc "$task_source_dir=/Vera")
done
swift build --package-path "$task_native_dir" "${task_build_flags[@]}"
task_bin_dir="$(swift build --package-path "$task_native_dir" -c release --show-bin-path)"
task_app_dir="$task_native_dir/build/ZSJHAudioHelper.app"
mkdir -p "$task_app_dir/Contents/MacOS" "$task_app_dir/Contents/Resources"
cp "$task_native_dir/Info.plist" "$task_app_dir/Contents/Info.plist"
cp "$task_bin_dir/AudioHelper" "$task_app_dir/Contents/MacOS/AudioHelper"
xcrun strip -S "$task_app_dir/Contents/MacOS/AudioHelper"
cp "$task_native_dir/../third_party/cicada/LICENSE" "$task_app_dir/Contents/Resources/Cicada-LICENSE"
cp "$task_native_dir/../third_party/cicada/UPSTREAM.md" "$task_app_dir/Contents/Resources/Cicada-UPSTREAM.md"
for task_sample in speech-probe-zh-CN.wav speech-probe-zh-CN.txt; do
  cp "$task_native_dir/Sources/AudioCore/Resources/$task_sample" "$task_app_dir/Contents/Resources/$task_sample"
done
if LC_ALL=C /usr/bin/strings "$task_app_dir/Contents/MacOS/AudioHelper" | /usr/bin/grep -E '/(Users|home)/[^/[:space:]]+/' >/dev/null; then
  print -u2 -- "AudioHelper contains a local build path; refusing to sign."
  exit 1
fi
codesign --force --sign - --identifier com.zsjh.audiohelper "$task_app_dir"
print -r -- "$task_app_dir"
