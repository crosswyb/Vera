#!/bin/zsh
# Create a distributable archive of an already verified app, never user state.
set -euo pipefail
task_native_dir="${0:A:h:h}"
task_app_dir="${1:-$task_native_dir/build/ZSJHWorkbench.app}"
task_output_dir="${2:-$task_native_dir/build/distribution}"
[[ -d "$task_app_dir/Contents" ]] || { print -u2 -- "App bundle not found: $task_app_dir"; exit 64; }
codesign --verify --deep --strict "$task_app_dir"
task_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$task_app_dir/Contents/Info.plist")"
task_build="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$task_app_dir/Contents/Info.plist")"
task_name="ZSJHWorkbench-${task_version}-build${task_build}-apple-silicon"
mkdir -p "$task_output_dir"
for task_suffix in dmg zip SHA256SUMS.txt; do
  [[ ! -e "$task_output_dir/$task_name.$task_suffix" ]] || { print -u2 -- "Refusing to replace existing release: $task_name.$task_suffix"; exit 65; }
done
task_stage="$(mktemp -d "$task_native_dir/build/distribution-stage.XXXXXX")"
trap 'rm -rf "$task_stage"' EXIT
ditto "$task_app_dir" "$task_stage/真实交互工作台.app"
cp "$task_native_dir/DISTRIBUTION.md" "$task_stage/安装与首次使用.md"
ln -s /Applications "$task_stage/Applications"
# Fail if a future build accidentally adds credentials or meeting databases.
task_forbidden="$(find "$task_stage/真实交互工作台.app" \( -name '.env' -o -name '.env.*' -o -name 'credentials.json' -o -name 'providers.json' -o -name 'workbench.env' -o -name '*.sqlite3' \) -print -quit)"
[[ -z "$task_forbidden" ]] || { print -u2 -- "Release contains forbidden local state; packaging stopped."; exit 66; }
hdiutil create -quiet -volname "真实交互工作台" -srcfolder "$task_stage" -format UDZO "$task_output_dir/$task_name.dmg"
hdiutil verify -quiet "$task_output_dir/$task_name.dmg"
ditto -c -k --sequesterRsrc "$task_stage" "$task_output_dir/$task_name.zip"
(
  cd "$task_output_dir"
  shasum -a 256 "$task_name.dmg" "$task_name.zip" > "$task_name.SHA256SUMS.txt"
)
print -r -- "$task_output_dir/$task_name.dmg"
print -r -- "$task_output_dir/$task_name.zip"
