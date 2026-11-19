#!/bin/zsh
set -euo pipefail
task_project_dir="${0:A:h:h}"
task_python="${ZSJH_BUILD_PYTHON:-$task_project_dir/.venv-live/bin/python}"
# Some bundled Python runtimes load a sibling rejection list when constructing
# any SSLContext. Keep that runtime policy available at the frozen ssl.py path.
task_ssl_policy="$("$task_python" -c 'import ssl; from pathlib import Path; p = Path(ssl.__file__).with_name("codex-rejected-cas.pem"); print(p if p.is_file() else "")')"
task_runtime_data=()
if [[ -n "$task_ssl_policy" ]]; then
  task_runtime_data+=(--add-data "$task_ssl_policy:.")
fi
"$task_python" -m PyInstaller --noconfirm --clean --onedir \
  --name zsjh-workbench-backend \
  --paths "$task_project_dir" \
  --distpath "$task_project_dir/native/build/backend-dist" \
  --workpath "$task_project_dir/native/build/backend-work" \
  --specpath "$task_project_dir/native/build" \
  --add-data "$task_project_dir/templates:templates" \
  --add-data "$task_project_dir/static:static" \
  "${task_runtime_data[@]}" \
  --exclude-module livekit --exclude-module torch --exclude-module numpy \
  --exclude-module scipy --exclude-module grpc --exclude-module matplotlib \
  --exclude-module IPython --exclude-module pytest \
  "$task_project_dir/tools/workbench_backend_entry.py"
