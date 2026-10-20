#!/usr/bin/env bash
set -euo pipefail
workflow_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$workflow_dir/.venv/bin/python" ]]; then
  env -u PYTHONPATH -u PYTHONHOME python3 -m venv "$workflow_dir/.venv"
fi
env -u PYTHONPATH -u PYTHONHOME "$workflow_dir/.venv/bin/python" -m pip install \
  --timeout 90 --progress-bar off -r "$workflow_dir/requirements.lock.txt"
env -u PYTHONPATH -u PYTHONHOME "$workflow_dir/.venv/bin/python" -m pip check
if [[ ! -e "$workflow_dir/.env" ]]; then
  cp "$workflow_dir/.env.example" "$workflow_dir/.env"
  chmod 600 "$workflow_dir/.env"
fi
env -u PYTHONPATH -u PYTHONHOME "$workflow_dir/.venv/bin/python" "$workflow_dir/scripts/install_skill.py"
echo 'Setup complete. Fill .env, select the executor API protocol in config.yaml.'
