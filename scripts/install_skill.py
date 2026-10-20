#!/usr/bin/env python3
"""Install one skill source into documented and legacy user discovery locations."""
import argparse
import os
from pathlib import Path

root = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument("--skills-dir", type=Path, help="Install only into this explicit directory")
args = parser.parse_args()
source = root / "skills" / "cheap-worker"
if args.skills_dir:
    directories = [args.skills_dir.expanduser()]
else:
    directories = [Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills",
                   Path.home() / ".agents" / "skills"]
destinations = list(dict.fromkeys(d / "cheap-worker" for d in directories))
# Preflight every destination before creating either alias.
for destination in destinations:
    if (destination.exists() or destination.is_symlink()) and not (
            destination.is_symlink() and destination.resolve() == source):
        raise SystemExit(f"Existing unrelated skill left untouched: {destination}")
for destination in destinations:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        print(f"Already installed: {destination}")
    else:
        destination.symlink_to(source, target_is_directory=True)
        print(f"Installed: {destination} -> {source}")
