#!/usr/bin/env python3
"""Resolve symlink-installed skill back to its maintained executor framework."""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[3]
runner = root / "run.sh"
if not runner.is_file():
    raise SystemExit("Framework missing: reinstall using multi_model_codex/scripts/install_skill.py")
os.execv("/bin/bash", ["bash", str(runner), *sys.argv[1:]])
