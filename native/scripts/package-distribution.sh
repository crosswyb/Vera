#!/bin/zsh
# Create a distributable archive of an already verified app, never user state.
set -euo pipefail
task_native_dir="${0:A:h:h}"
task_app_dir="${1:-$task_native_dir/build/Vera.app}"
task_output_dir="${2:-$task_native_dir/build/distribution}"
task_python="${ZSJH_BUILD_PYTHON:-$task_native_dir/../.venv-live/bin/python}"
[[ -d "$task_app_dir/Contents" ]] || { print -u2 -- "App bundle not found: $task_app_dir"; exit 64; }
"$task_python" "$task_native_dir/../tools/release_privacy.py" --source-root "$task_native_dir/.." --git-ref HEAD --app "$task_app_dir"
codesign --verify --deep --strict "$task_app_dir"
task_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$task_app_dir/Contents/Info.plist")"
task_build="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$task_app_dir/Contents/Info.plist")"
task_product="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleName' "$task_app_dir/Contents/Info.plist")"
[[ "$task_product" == Vera ]] || { print -u2 -- "Expected the Vera product name."; exit 64; }
task_name="Vera-${task_version}-build${task_build}-apple-silicon"
mkdir -p "$task_output_dir"
for task_suffix in dmg zip SHA256SUMS.txt; do
  [[ ! -e "$task_output_dir/$task_name.$task_suffix" ]] || { print -u2 -- "Refusing to replace existing release: $task_name.$task_suffix"; exit 65; }
done
task_stage="$(mktemp -d "$task_native_dir/build/distribution-stage.XXXXXX")"
trap 'rm -rf "$task_stage"' EXIT
ditto "$task_app_dir" "$task_stage/Vera.app"
cp "$task_native_dir/DISTRIBUTION.md" "$task_stage/安装与首次使用.md"
ln -s /Applications "$task_stage/Applications"
"$task_python" "$task_native_dir/../tools/release_privacy.py" --app "$task_stage/Vera.app"
hdiutil create -quiet -volname "Vera" -srcfolder "$task_stage" -format UDZO "$task_output_dir/$task_name.dmg"
hdiutil verify -quiet "$task_output_dir/$task_name.dmg"
ditto -c -k --sequesterRsrc "$task_stage" "$task_output_dir/$task_name.zip"
"$task_python" "$task_native_dir/../tools/release_privacy.py" --archive "$task_output_dir/$task_name.zip"
(
  cd "$task_output_dir"
  shasum -a 256 "$task_name.dmg" "$task_name.zip" > "$task_name.SHA256SUMS.txt"
)
print -r -- "$task_output_dir/$task_name.dmg"
print -r -- "$task_output_dir/$task_name.zip"
