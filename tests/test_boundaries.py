from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from orchestrator.agents import Agent, READ_TOOLS, WRITE_TOOLS
from orchestrator.artifacts import Artifacts
from orchestrator.config import HERE
from orchestrator.schemas import gate
from orchestrator.workspace import Workspace
from tests.helpers import contract, review
PLAN = contract()
REVIEW = review({"snapshot_digest": "a" * 64})
from tests.helpers import create_repo, git


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = create_repo(self.base / "repo")
        self.config = yaml.safe_load((HERE / "config.yaml").read_text())
        self.artifacts = Artifacts(self.base / "artifacts", ["a-private-key"])
        self.ws = Workspace(self.root, self.config, self.artifacts)

    def test_path_traversal_absolute_secrets_and_git_rejected(self):
        for name in ("../outside", "/etc/passwd", ".git/config", ".env", "nested/.env.local",
                     "key.pem", ".ssh/id_rsa", "credentials.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.ws.path(name)

    def test_symlink_rejected_and_not_listed(self):
        (self.root / "external.py").symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            self.ws.read_file("external.py")
        self.assertNotIn("external.py", self.ws.list_files()["files"])

    def test_protected_instructions_readable_not_writable(self):
        (self.root / "AGENTS.md").write_text("Instructions")
        self.assertEqual(self.ws.read_file("AGENTS.md")["content"], "Instructions")
        with self.assertRaises(ValueError):
            self.ws.write_file("AGENTS.md", "weakened")
        with self.assertRaises(ValueError):
            self.ws.write_file(".github/workflows/test.yml", "disabled")
        with self.assertRaises(ValueError):
            self.ws.write_file(".gitlab-ci.yml", "disabled")
        with self.assertRaises(ValueError):
            self.ws.write_file(".agents/skills/override/SKILL.md", "override")

    def test_worktree_branch_change_invalidates_acceptance(self):
        git(self.root, "switch", "-c", "unexpected-branch")
        with self.assertRaises(ValueError):
            self.ws.snapshot()

    def test_hardlink_cannot_be_overwritten(self):
        outside = self.base / "important.txt"
        outside.write_text("original")
        os.link(outside, self.root / "hardlink.txt")
        with self.assertRaises(ValueError):
            self.ws.write_file("hardlink.txt", "modified")
        self.assertEqual(outside.read_text(), "original")

    def test_unknown_check_not_executable(self):
        with self.assertRaises(ValueError):
            self.ws.run_check("python3 -c evil")
        self.assertEqual({s["function"]["name"] for s in WRITE_TOOLS},
                         {"write_file", "delete_file", "run_check"})
        self.assertEqual({s["function"]["name"] for s in READ_TOOLS}, {"read_file", "list_files"})

    def test_commands_do_not_inherit_api_keys(self):
        code = "import os; assert 'EXECUTOR_API_KEY' not in os.environ; print('clean')"
        self.config["checks"]["env-test"] = {"argv": [sys.executable, "-c", code]}
        with patch.dict(os.environ, {"EXECUTOR_API_KEY": "a-private-key"}):
            result = self.ws.run_check("env-test")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("clean", result["stdout"])

    def test_check_timeout_records_failure(self):
        self.config["workflow"]["command_timeout_seconds"] = 1
        self.config["checks"]["timeout"] = {"argv": [sys.executable, "-c", "import time; time.sleep(5)"]}
        result = self.ws.run_check("timeout")
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["error"], "Check timed out")

    def test_output_limit_records_failure(self):
        self.config["workflow"]["max_check_log_bytes"] = 100
        self.config["checks"]["output"] = {"argv": [sys.executable, "-c", "print('x'*10000)"]}
        result = self.ws.run_check("output")
        self.assertEqual(result["error"], "Check output limit exceeded")

    def test_new_deleted_and_existing_files_in_snapshot(self):
        self.ws.write_file("new.py", "hello = 1\n")
        self.ws.delete_file("calc.py")
        snapshot = self.ws.snapshot()
        self.assertIn("new.py", snapshot["changed_files"])
        self.assertEqual(snapshot["hashes"]["calc.py"], "DELETED")
        self.assertIn("+hello = 1", snapshot["diff"])
        self.assertIn("-def add", snapshot["diff"])

    def test_artifact_secret_redaction(self):
        self.artifacts.json("test.json", {"key": "a-private-key", "token": "Bearer abcd1234"})
        self.assertNotIn("a-private-key", (self.artifacts.path / "test.json").read_text())
        self.assertNotIn("abcd1234", (self.artifacts.path / "test.json").read_text())

    def test_missing_or_duplicate_criteria_cannot_pass(self):
        for criteria in ([], REVIEW["criteria_status"] * 2):
            review = copy.deepcopy(REVIEW)
            review["criteria_status"] = criteria
            self.assertTrue(gate(review, PLAN, [{"returncode": 0}], True))

    def test_unresolved_finding_cannot_pass(self):
        review = copy.deepcopy(REVIEW)
        review["findings"] = [{"severity": "high", "file": "calc.py", "issue": "broken", "required_fix": "fix"}]
        self.assertTrue(gate(review, PLAN, [{"returncode": 0}], True))


if __name__ == "__main__":
    unittest.main()
