#!/bin/zsh
set -euo pipefail
task_native_dir="${0:A:h:h}"
task_source="$task_native_dir/Assets/AppIcon/AppIcon.png"
task_iconset="$task_native_dir/build/AppIcon.iconset"
if [[ ! -f "$task_source" ]]; then
  print -u2 -- "Missing app icon artwork: $task_source"
  exit 66
fi
mkdir -p "$task_iconset"
for task_size in 16 32 128 256 512; do
  /usr/bin/sips -z "$task_size" "$task_size" "$task_source" \
    --out "$task_iconset/icon_${task_size}x${task_size}.png" >/dev/null
  task_retina=$((task_size * 2))
  /usr/bin/sips -z "$task_retina" "$task_retina" "$task_source" \
    --out "$task_iconset/icon_${task_size}x${task_size}@2x.png" >/dev/null
done
/usr/bin/iconutil --convert icns --output "$task_native_dir/build/AppIcon.icns" "$task_iconset"
print -r -- "$task_native_dir/build/AppIcon.icns"
