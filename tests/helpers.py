from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from orchestrator.config import HERE


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.STDOUT).strip()


def create_repo(root: Path):
    root.mkdir()
    git(root, "init", "-q")
    (root / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/test_baseline.py").write_text(
        "import unittest\nclass Baseline(unittest.TestCase):\n    def test_environment(self):\n        self.assertTrue(True)\n",
        encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=Local Offline Test", "-c", "user.email=offline@example.invalid",
        "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Offline fixture")
    return root


def contract():
    return {"task": "Implement add(a,b) and test it", "context": "Keep the interface",
            "writable_paths": ["calc.py", "tests/test_calc.py"],
            "acceptance_criteria": ["add(2,3) returns 5; existing tests remain"],
            "checks": {"unit": {"argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]}}}


def review(state):
    return {"snapshot_digest": state["snapshot_digest"], "decision": "PASS",
            "summary": "Reviewed implementation and actual test logs", "findings": [],
            "criteria_status": [{"criterion": contract()["acceptance_criteria"][0], "status": "PASS",
                                 "evidence": "calc.py implements addition and unit check passed"}]}


def invoke(base: Path, repo: Path, url: str, protocol="chat_completions", options=None,
           extra=(), task_contract=None, operation=None):
    config = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    config["roles"]["executor"].update(api=protocol, env_prefix="TEST_EXECUTOR")
    config["workflow"].update(api_max_retries=0, command_timeout_seconds=10, api_timeout_seconds=10)
    if options:
        options(config)
    base.mkdir(exist_ok=True)
    config_file = base / "config.yaml"
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    contract_file = base / "contract.json"
    contract_file.write_text(json.dumps(task_contract or contract()), encoding="utf-8")
    env = os.environ.copy()
    env.update(TEST_EXECUTOR_BASE_URL=url, TEST_EXECUTOR_API_KEY="local-test-secret-12345",
               TEST_EXECUTOR_MODEL="offline-executor", NO_PROXY="127.0.0.1,localhost")
    # No high-model configuration is available to the process.
    for key in list(env):
        if key.startswith(("SOL_", "REVIEW_", "PLANNER_", "DEEPSEEK_")):
            del env[key]
    command = [sys.executable, str(HERE / "main.py"), "--config", str(config_file),
               "--env-file", str(base / "nonexistent.env"), "--runs-dir", str(base / "runs")]
    command += list(operation) if operation else ["execute", "--repo", str(repo), "--contract", str(contract_file)]
    process = subprocess.run(command + list(extra), cwd=base, env=env,
                             capture_output=True, text=True, timeout=40)
    run_dirs = sorted((base / "runs").glob("*")) if (base / "runs").exists() else []
    summary = json.loads((run_dirs[-1] / "summary.json").read_text()) if run_dirs else None
    return process, summary, run_dirs[-1] if run_dirs else None
