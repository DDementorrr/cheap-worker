#!/usr/bin/env bash
set -euo pipefail
workflow_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$workflow_dir/.venv/bin/python" ]]; then
  echo 'Run: bash scripts/setup.sh' >&2
  exit 2
fi
# Keep sourced ROS Python packages out of the orchestrator's virtual environment.
exec env -u PYTHONPATH -u PYTHONHOME "$workflow_dir/.venv/bin/python" "$workflow_dir/main.py" "$@"
