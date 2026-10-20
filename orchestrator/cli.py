from __future__ import annotations

import argparse
import fcntl
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents import READ_TOOLS
from .artifacts import Artifacts
from .config import HERE, endpoint, load_config, secrets
from .models import Budget, Session
from . import workflow
from .workspace import push, repository


def run_id(prefix="run"):
    return f"{prefix}-{datetime.now(ZoneInfo('Asia/Shanghai')):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"


def doctor(config, probe, runs_dir):
    ep = endpoint(config["roles"]["executor"])
    print(f"executor: configured, protocol={config['roles']['executor']['api']}, model={ep['MODEL']}")
    if not probe:
        print("Offline check complete. No API contacted. Planning/review use the current Codex chat.")
        return 0
    artifacts = Artifacts(runs_dir / run_id("probe"), secrets(config))
    budget = Budget(config["workflow"]["max_model_requests"])
    session = Session("executor", config, artifacts, budget,
                      "This is a tool compatibility probe. Call list_files once, then say OK.",
                      "Call list_files with path '.' now; then answer OK.", READ_TOOLS[:1])
    try:
        _, calls = session.request()
        if len(calls) != 1 or calls[0]["name"] != "list_files":
            raise RuntimeError("Expected exactly one list_files tool call")
        args = json.loads(calls[0]["arguments"])
        if not isinstance(args, dict) or set(args) - {"path"}:
            raise RuntimeError("Invalid probe tool arguments")
        session.result(calls[0]["id"], {"files": [], "probe": True})
        text, more = session.request()
        if more or not text.strip():
            raise RuntimeError("Tool result continuation failed")
        artifacts.json("summary.json", {"status": "PROBE_PASS", "model_requests": budget.used})
        print(f"Executor API/tool round-trip OK. Artifacts: {artifacts.path}")
        return 0
    finally:
        session.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chat-led delegation to one economical execution model")
    ap.add_argument("--config", type=Path, default=HERE / "config.yaml")
    ap.add_argument("--env-file", type=Path, default=HERE / ".env")
    ap.add_argument("--runs-dir", type=Path, default=HERE / "runs")
    commands = ap.add_subparsers(dest="command", required=True)
    p = commands.add_parser("doctor", help="Offline config check; --probe contacts paid API")
    p.add_argument("--probe", action="store_true")
    p = commands.add_parser("push", help="Push the clean current branch to its configured upstream")
    p.add_argument("--repo", required=True)
    p = commands.add_parser("execute", help="Run a chat-written contract in a detached worktree")
    p.add_argument("--repo", required=True)
    p.add_argument("--contract", required=True, type=Path)
    p.add_argument("--push-before", action="store_true",
                   help="Push the clean current branch before creating the worktree")
    for name in ("inspect", "rework", "apply", "rollback"):
        p = commands.add_parser(name)
        p.add_argument("--run", required=True, type=Path)
        if name == "rework":
            p.add_argument("--feedback-file", required=True, type=Path)
        if name == "apply":
            p.add_argument("--review-file", required=True, type=Path)
    args = ap.parse_args(argv)
    config = None
    try:
        runs_dir = args.runs_dir.expanduser().resolve()
        if args.command == "push":
            root = repository(args.repo)
            push(root)
            print(f"Pushed {root}")
            return 0
        if args.command == "execute":
            config = load_config(args.config.expanduser().resolve(), args.env_file.expanduser().resolve())
            source = repository(args.repo, require_clean=False)
            if runs_dir == source or source in runs_dir.parents:
                raise ValueError("--runs-dir must be outside the target repository")
            if args.push_before:
                push(source)
            contract = json.loads(args.contract.read_text(encoding="utf-8"))
            effective = workflow.prepare(config, contract)
            endpoint(config["roles"]["executor"])
            artifacts = Artifacts(runs_dir / run_id(), secrets(config))
            return workflow.execute(effective, artifacts, source, contract)
        if args.command == "doctor":
            config = load_config(args.config.expanduser().resolve(), args.env_file.expanduser().resolve())
            return doctor(config, args.probe, runs_dir)
        run_dir = args.run.expanduser().resolve()
        config = load_config(run_dir / "config.yaml", args.env_file.expanduser().resolve())
        artifacts = Artifacts.open_existing(run_dir, secrets(config))
        with (run_dir / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another operation is using this run") from None
            state = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            contract = json.loads((run_dir / "contract.json").read_text(encoding="utf-8"))
            if args.command == "inspect":
                return workflow.inspect(config, artifacts, state)
            if args.command == "rework":
                feedback = args.feedback_file.read_text(encoding="utf-8")
                if not feedback.strip():
                    raise ValueError("Feedback must not be empty")
                endpoint(config["roles"]["executor"])
                return workflow.iteration(config, artifacts, state, contract, feedback)
            if args.command == "apply":
                review = json.loads(args.review_file.read_text(encoding="utf-8"))
                return workflow.apply(config, artifacts, state, contract, review)
            return workflow.rollback(config, artifacts, state)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        msg = str(exc)
        if config:
            for secret in secrets(config):
                msg = msg.replace(secret, "[REDACTED]")
        print("ERROR: " + msg, file=sys.stderr)
        return 2
