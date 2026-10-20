from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.fake_provider import server
from tests.helpers import contract, create_repo, git, invoke, review


class WorkflowTests(unittest.TestCase):
    def setup_case(self):
        temp = tempfile.TemporaryDirectory(prefix="cheap-worker-test-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        repo = create_repo(base / "repo")
        return base, repo

    def run_case(self, mode="pass", protocol="chat_completions", expected=0, options=None):
        base, repo = self.setup_case()
        branch = git(repo, "branch", "--show-current")
        with server(mode) as (url, provider):
            p, state, run = invoke(base / "runner", repo, url, protocol, options)
        self.assertEqual(p.returncode, expected, p.stdout + p.stderr)
        self.assertEqual(git(repo, "branch", "--show-current"), branch)
        self.assertEqual(git(repo, "status", "--porcelain"), "")
        self.assertIn("return 0", (repo / "calc.py").read_text())
        return state, run, provider

    def test_only_executor_api_and_chat_must_accept(self):
        state, run, provider = self.run_case()
        self.assertEqual(state["status"], "READY_FOR_REVIEW")
        self.assertEqual(state["model_requests"], 2)
        self.assertEqual(state["input_tokens"], 40)
        self.assertEqual(provider.review_rounds, 0)
        self.assertEqual({body["model"] for _, body in provider.requests}, {"offline-executor"})
        self.assertIn("tests/test_calc.py", (run / "candidate.patch").read_text())
        self.assertIn("class TestAdd", (run / "candidate.patch").read_text())
        self.assertNotIn("local-test-secret", (run / "round-1/events.jsonl").read_text())
        self.assertFalse((run / "chat-review.json").exists())

    def test_responses_protocol(self):
        state, _, provider = self.run_case(protocol="responses")
        self.assertEqual(state["status"], "READY_FOR_REVIEW")
        self.assertEqual({path for path, _ in provider.requests}, {"/v1/responses"})

    def test_failing_tests_cannot_be_applied(self):
        base, repo = self.setup_case()
        with server("failed-test") as (url, provider):
            p, state, run = invoke(base / "runner", repo, url)
            self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
            r = base / "review.json"
            r.write_text(json.dumps(review(state)))
            p, _, _ = invoke(base / "runner", repo, url, operation=[
                "apply", "--run", str(run), "--review-file", str(r)])
            self.assertEqual(p.returncode, 2)
            self.assertEqual(len(provider.requests), 2)
        self.assertEqual(state["status"], "NEEDS_REWORK")
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_explicit_chat_feedback_then_review_apply_without_api(self):
        base, repo = self.setup_case()
        with server("rework") as (url, provider):
            p, state, run = invoke(base / "runner", repo, url)
            self.assertEqual(p.returncode, 3)
            feedback = base / "feedback.txt"
            feedback.write_text("Unit failed: implementation subtracts. Use addition; preserve interfaces and tests.")
            p, state, _ = invoke(base / "runner", repo, url, operation=[
                "rework", "--run", str(run), "--feedback-file", str(feedback)])
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(state["model_requests"], 4)
            self.assertEqual(state["rounds"], 2)
            self.assertEqual(state["status"], "READY_FOR_REVIEW")
            p, _, _ = invoke(base / "runner", repo, url, operation=["inspect", "--run", str(run)])
            self.assertTrue(json.loads(p.stdout)["verification_current"])
            r = base / "review.json"
            r.write_text(json.dumps(review(state)))
            p, state, _ = invoke(base / "runner", repo, url, operation=[
                "apply", "--run", str(run), "--review-file", str(r)])
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(state["status"], "APPLIED")
            self.assertEqual(len(provider.requests), 4)
        self.assertIn("a + b", (repo / "calc.py").read_text())
        self.assertTrue((repo / "tests/test_calc.py").is_file())
        self.assertEqual(git(repo, "rev-parse", "HEAD"), state["base_commit"])

    def test_scope_enforced_in_file_tools(self):
        state, run, _ = self.run_case(mode="scope")
        self.assertFalse((Path(state["worktree"]) / "outside.txt").exists())
        self.assertIn("outside the chat-approved", (run / "round-1/events.jsonl").read_text())

    def test_turn_and_global_budgets(self):
        state, _, _ = self.run_case(mode="tool-limit", expected=3,
            options=lambda c: c["workflow"].update(max_tool_calls_per_stage=2))
        self.assertEqual(state["status"], "NEEDS_REWORK")
        state, _, _ = self.run_case(expected=1,
            options=lambda c: c["workflow"].update(max_model_requests=1))
        self.assertEqual(state["status"], "ERROR")
        self.assertEqual(state["model_requests"], 1)

    def test_diff_limit_blocks_review(self):
        state, _, _ = self.run_case(expected=1,
            options=lambda c: c["workflow"].update(max_review_chars=10))
        self.assertEqual(state["status"], "ERROR")

    def test_rework_limit_prevents_new_api_requests(self):
        base, repo = self.setup_case()
        with server() as (url, provider):
            _, _, run = invoke(base / "runner", repo, url,
                              options=lambda c: c["workflow"].update(max_rework_rounds=0))
            f = base / "feedback.txt"; f.write_text("Recheck")
            p, _, _ = invoke(base / "runner", repo, url, operation=[
                "rework", "--run", str(run), "--feedback-file", str(f)])
            self.assertEqual(p.returncode, 2)
            self.assertEqual(len(provider.requests), 2)

    def test_stale_review_or_changed_source_cannot_apply(self):
        for mutation in ("worktree", "source", "review", "patch", "criteria"):
            with self.subTest(mutation=mutation):
                base, repo = self.setup_case()
                with server() as (url, _):
                    _, state, run = invoke(base / "runner", repo, url)
                rv = review(state)
                if mutation == "worktree":
                    (Path(state["worktree"]) / "calc.py").write_text("broken = True\n")
                elif mutation == "source":
                    (repo / "local.txt").write_text("user edit")
                elif mutation == "review":
                    rv["snapshot_digest"] = "0" * 64
                elif mutation == "patch":
                    (run / "candidate.patch").write_text("tampered")
                else:
                    rv["criteria_status"] = []
                r = base / "review.json"; r.write_text(json.dumps(rv))
                p, _, _ = invoke(base / "runner", repo, "http://127.0.0.1:1/v1", operation=[
                    "apply", "--run", str(run), "--review-file", str(r)])
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn("return 0", (repo / "calc.py").read_text())

    def test_configuration_doctor_offline_and_tool_probe(self):
        base, repo = self.setup_case()
        with server() as (url, provider):
            p, state, _ = invoke(base / "runner", repo, url, operation=["doctor"])
            self.assertEqual(p.returncode, 0)
            self.assertIsNone(state)
            self.assertFalse(provider.requests)
            p, state, _ = invoke(base / "runner", repo, url, operation=["doctor", "--probe"])
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(state["status"], "PROBE_PASS")
            self.assertEqual(len(provider.requests), 2)

    def test_dirty_repository_is_snapshotted_without_branch_or_data_loss(self):
        base, repo = self.setup_case()
        (repo / "local.txt").write_text("user work")
        with server() as (url, provider):
            p, state, _ = invoke(base / "runner", repo, url)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(state["status"], "READY_FOR_REVIEW")
        self.assertEqual(state["mode"], "copy")
        self.assertIn("local.txt", state["source_status"])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual((repo / "local.txt").read_text(), "user work")

    def test_dirty_repository_apply_and_rollback_preserve_user_file(self):
        base, repo = self.setup_case()
        (repo / "local.txt").write_text("user work")
        (repo / "calc.py").write_text("def add(a, b):\n    return 10\n")
        with server() as (url, _):
            p, state, run = invoke(base / "runner", repo, url)
            self.assertEqual(p.returncode, 0)
            record = base / "review.json"
            record.write_text(json.dumps(review(state)))
            p, state, _ = invoke(base / "runner", repo, url, operation=[
                "apply", "--run", str(run), "--review-file", str(record)])
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(state["status"], "APPLIED")
            self.assertEqual((repo / "local.txt").read_text(), "user work")
            p, state, _ = invoke(base / "runner", repo, url,
                                 operation=["rollback", "--run", str(run)])
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(state["status"], "ROLLED_BACK")
        self.assertIn("return 10", (repo / "calc.py").read_text())
        self.assertFalse((repo / "tests/test_calc.py").exists())
        self.assertEqual((repo / "local.txt").read_text(), "user work")

    def test_verification_mutation_invalidates_candidate(self):
        base, repo = self.setup_case()
        c = contract()
        c["checks"]["mutating-check"] = {"argv": [
            __import__("sys").executable, "-c",
            "from pathlib import Path; Path('calc.py').write_text('def add(a,b): return -1\\n')"]}
        with server() as (url, _):
            p, state, run = invoke(base / "runner", repo, url, task_contract=c)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(state["status"], "ERROR")
        self.assertIsNone(state["snapshot_digest"])
        self.assertFalse((run / "candidate.patch").exists())
        self.assertIn("return 0", (repo / "calc.py").read_text())

    def test_resume_rejects_changed_worktree_head_before_api(self):
        base, repo = self.setup_case()
        with server() as (url, provider):
            _, state, run = invoke(base / "runner", repo, url)
            git(Path(state["worktree"]), "switch", "-c", "unexpected")
            f = base / "feedback.txt"
            f.write_text("Please recheck")
            p, state, _ = invoke(base / "runner", repo, url, operation=[
                "rework", "--run", str(run), "--feedback-file", str(f)])
            self.assertEqual(p.returncode, 1)
            self.assertEqual(state["status"], "ERROR")
            self.assertEqual(len(provider.requests), 2)

    def test_invalid_scope_and_git_only_checks_rejected_offline(self):
        for kind in ("scope", "checks", "override"):
            base, repo = self.setup_case()
            c = contract()
            if kind == "scope":
                c["writable_paths"] = ["../secret"]
            elif kind == "checks":
                c["checks"] = {"git-only": {"argv": ["git", "diff"]}}
            else:
                c["checks"] = {"whitespace": {"argv": ["true"]}}
            with server() as (url, provider):
                p, _, _ = invoke(base / "runner", repo, url, task_contract=c)
            self.assertEqual(p.returncode, 2)
            self.assertFalse(provider.requests)


if __name__ == "__main__":
    unittest.main()
