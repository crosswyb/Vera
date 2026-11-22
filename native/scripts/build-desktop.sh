#!/bin/zsh
set -euo pipefail
task_native_dir="${0:A:h:h}"
task_backend_dir="${1:-$task_native_dir/build/backend-dist/zsjh-workbench-backend}"
if [[ ! -x "$task_backend_dir/zsjh-workbench-backend" ]]; then
  print -u2 -- "Pass the PyInstaller onedir directory containing zsjh-workbench-backend."
  exit 64
fi
/bin/zsh "$task_native_dir/scripts/build-app.sh"
/bin/zsh "$task_native_dir/scripts/build-icon.sh"
task_bin_dir="$(swift build --package-path "$task_native_dir" -c release --show-bin-path)"
task_app_dir="$task_native_dir/build/真实交互.app"
mkdir -p "$task_app_dir/Contents/MacOS" "$task_app_dir/Contents/Resources"
cp "$task_native_dir/DesktopInfo.plist" "$task_app_dir/Contents/Info.plist"
task_source_revision="$(git -C "$task_native_dir/.." rev-parse HEAD 2>/dev/null || print unknown)"
/usr/libexec/PlistBuddy -c "Add :ZSJHSourceRevision string $task_source_revision" "$task_app_dir/Contents/Info.plist"
cp "$task_native_dir/build/AppIcon.icns" "$task_app_dir/Contents/Resources/AppIcon.icns"
cp "$task_bin_dir/DesktopHost" "$task_app_dir/Contents/MacOS/DesktopHost"
xcrun strip -S "$task_app_dir/Contents/MacOS/DesktopHost"
# ditto replaces the selected build resources; it does not touch user state.
rm -rf "$task_app_dir/Contents/Resources/backend" "$task_app_dir/Contents/Resources/ZSJHAudioHelper.app" "$task_app_dir/Contents/Resources/ThirdParty"
ditto "$task_backend_dir" "$task_app_dir/Contents/Resources/backend"
ditto "$task_native_dir/build/ZSJHAudioHelper.app" "$task_app_dir/Contents/Resources/ZSJHAudioHelper.app"
ditto "$task_native_dir/../third_party" "$task_app_dir/Contents/Resources/ThirdParty"
cp "$task_native_dir/../LICENSE" "$task_app_dir/Contents/Resources/LICENSE"
test -s "$task_app_dir/Contents/Resources/ZSJHAudioHelper.app/Contents/Resources/speech-probe-zh-CN.wav"
task_python="${ZSJH_BUILD_PYTHON:-$task_native_dir/../.venv-live/bin/python}"
"$task_python" "$task_native_dir/../tools/release_privacy.py" --app "$task_app_dir"
codesign --force --sign - --identifier com.zsjh.workbench "$task_app_dir"
codesign --verify --deep --strict "$task_app_dir"
print -r -- "$task_app_dir"
