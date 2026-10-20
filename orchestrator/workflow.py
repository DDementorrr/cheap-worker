"""Single external worker; planning and acceptance are performed in the Codex chat."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

from jsonschema import validate

from .agents import Agent
from .artifacts import Artifacts
from .config import secrets
from .models import Budget
from .schemas import REVIEW, gate
from .workspace import Workspace, git, push, repository


def digest(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare(config, contract):
    """The chat writes this contract; the worker cannot change it through tools."""
    if not isinstance(contract, dict) or set(contract) - {
            "task", "context", "writable_paths", "acceptance_criteria", "checks"}:
        raise ValueError("Invalid contract fields")
    if not isinstance(contract.get("task"), str) or not contract["task"].strip():
        raise ValueError("Contract needs a nonempty task")
    if not isinstance(contract.get("context", ""), str):
        raise ValueError("context must be text")
    for key in ("writable_paths", "acceptance_criteria"):
        items = contract.get(key)
        if not isinstance(items, list) or not items or not all(isinstance(x, str) and x.strip() for x in items):
            raise ValueError(f"Contract needs nonempty {key}")
        if len(set(items)) != len(items):
            raise ValueError(f"Duplicate {key}")
    for item in contract["writable_paths"]:
        if (Path(item).is_absolute() or ".." in Path(item).parts or
                not Path(item).parts or any(c in item for c in "*?[]\\") or
                item.rstrip("/") != Path(item).as_posix()):
            raise ValueError("writable_paths must be exact relative files or directories ending in '/' (no globs)")
    cfg = copy.deepcopy(config)
    checks = contract.get("checks")
    if not isinstance(checks, dict) or not checks:
        raise ValueError("Contract must supply checks with exact argv arrays")
    for name, check in checks.items():
        argv = check.get("argv") if isinstance(check, dict) else None
        if not isinstance(name, str) or not name or not isinstance(argv, list) or not argv or not all(
                isinstance(s, str) and s for s in argv):
            raise ValueError("Each contract check requires a nonempty name and string argv array")
        if name in cfg["checks"] and cfg["checks"][name] != check:
            raise ValueError(f"Contract cannot replace configured check: {name}")
    cfg["checks"].update(checks)
    names = list(dict.fromkeys(cfg["required_checks"] + list(checks)))
    if len(names) > cfg["workflow"]["max_verification_checks"]:
        raise ValueError("Too many verification checks")
    if not any(Path(cfg["checks"][n]["argv"][0]).name != "git" for n in names):
        raise ValueError("Include a substantive test/build/content check, not only Git checks")
    cfg["checks"] = {n: cfg["checks"][n] for n in names}
    cfg["required_checks"] = names
    cfg.setdefault("safety", {})["writable_paths"] = contract["writable_paths"]
    return cfg


def workspace(state, config, artifacts):
    ws = Workspace(Path(state["worktree"]), config, artifacts)
    if ws.base_commit != state["workspace_commit"] or ws.base_branch != state["branch"]:
        raise ValueError("Worktree HEAD or branch no longer matches this run")
    return ws


def _copy_ignore(directory, names):
    blocked = {".git", ".venv", "node_modules", "__pycache__", "runs", ".codex", ".agents"}
    result = []
    for name in names:
        lower = name.lower()
        if name in blocked or lower.startswith(".env") or lower.endswith((".key", ".pem", ".p12", ".pfx")):
            result.append(name)
    return result


def copy_workspace(source: Path, destination: Path) -> str:
    """Make a private snapshot and give it an internal baseline commit.

    The target repository is never put on a branch. The internal commit makes
    the candidate patch relative to the copied state, including dirty source
    files, instead of relative to the target's HEAD.
    """
    shutil.copytree(source, destination, symlinks=True, ignore=_copy_ignore)
    git(destination, "init", "-q")
    git(destination, "add", "-f", "-A")
    git(destination, "-c", "user.name=cheap-worker snapshot",
        "-c", "user.email=cheap-worker@localhost", "commit", "-qm", "Worker baseline")
    return git(destination, "rev-parse", "HEAD").strip()


def save(artifacts, state):
    artifacts.json("summary.json", state)
    artifacts.text("summary.md", "\n".join([
        f"Status: {state['status']}", f"Reason: {state.get('reason', '')}",
        f"Source: {state['source_repo']}", f"Worktree: {state.get('worktree', '')}",
        f"Rounds: {state['rounds']}", f"Model requests: {state['model_requests']}",
        "The worker cannot accept its own work. Chat review is required before apply.",
    ]) + "\n")
    print(f"{state['status']}: {state.get('reason', '')}\nRun: {artifacts.path}", flush=True)


def execute(config, artifacts, source, contract):
    import yaml
    config = prepare(config, contract)
    artifacts.json("contract.json", contract)
    artifacts.text("config.yaml", yaml.safe_dump(config, allow_unicode=True))
    base = git(source, "rev-parse", "HEAD").strip()
    source_status = git(source, "status", "--porcelain", "--untracked-files=all")
    state = {"status": "STARTING", "reason": "", "source_repo": str(source),
             "base_commit": base, "source_status": source_status,
             "branch": "", "mode": config.get("execution", {}).get("mode", "copy"),
             "worktree": str(artifacts.path / "worktree"), "rounds": 0,
             "model_requests": 0, "input_tokens": 0, "output_tokens": 0}
    save(artifacts, state)
    try:
        if state["mode"] == "copy":
            state["workspace_commit"] = copy_workspace(source, Path(state["worktree"]))
            state["branch"] = git(Path(state["worktree"]), "branch", "--show-current").strip()
        elif state["mode"] == "branch":
            # Optional legacy mode: detached worktree, no named branch is created.
            git(source, "worktree", "add", "--detach", state["worktree"], base)
            state["workspace_commit"] = base
        else:
            raise ValueError("execution.mode must be copy or branch")
    except Exception as exc:
        state.update(status="ERROR", reason=artifacts.clean(str(exc)))
        save(artifacts, state)
        return 1
    return iteration(config, artifacts, state, contract, "")


def iteration(config, artifacts, state, contract, feedback):
    if state["status"] == "APPLIED":
        raise ValueError("An applied run cannot be reworked")
    if state["rounds"] >= config["workflow"]["max_rework_rounds"] + 1:
        raise ValueError("Rework limit reached; current chat should take over or split the task")
    budget = Budget(config["workflow"]["max_model_requests"], state["model_requests"],
                    state["input_tokens"], state["output_tokens"])
    state["rounds"] += 1
    state.update(status="RUNNING", reason="", checks=[], executor_complete=False, snapshot_digest=None)
    round_name = f"round-{state['rounds']}"
    state["latest_round"] = round_name
    save(artifacts, state)
    logs = Artifacts(artifacts.path / round_name, secrets(config))
    agent = None
    try:
        ws = workspace(state, config, logs)
        ws.snapshot()  # Reject externally changed HEAD/scope before contacting API.
        payload = {"contract": contract, "feedback": feedback,
                   "worktree": str(ws.root), "files_preview": ws.list_files(),
                   "checks": config["checks"]}
        instructions = ws.root / "AGENTS.md"
        if instructions.is_file():
            payload["root_instructions"] = ws.read_file("AGENTS.md", max_lines=500)
        logs.text("feedback.txt", feedback)
        agent = Agent("executor", round_name, config, logs, budget, ws,
                      artifacts.clean(json.dumps(payload, ensure_ascii=False)))
        report, complete = agent.finish()
        logs.text("executor-report.txt", report)
        state["executor_complete"] = complete
        before = ws.snapshot()
        if len(before["diff"]) > config["workflow"]["max_review_chars"]:
            raise ValueError("Full diff exceeds max_review_chars; split the task")
        if artifacts.clean(before["diff"]) != before["diff"]:
            raise ValueError("Diff contains credential-like data; cannot export a redacted patch")
        checks = [ws.run_check(n) for n in config["required_checks"]]
        state["checks"] = checks
        logs.json("verification.json", checks)
        after = ws.snapshot()
        if before != after:
            raise ValueError("Verification modified reviewable files; inspect checks/ignore rules")
        logs.json("snapshot.json", after)
        artifacts.text("candidate.patch", after["diff"])
        state["snapshot_digest"] = digest(after)
        state["changed_files"] = after["changed_files"]
        reasons = []
        if not complete:
            reasons.append("Executor did not finish within its budget")
        if any(c.get("returncode") != 0 or c.get("error") for c in checks):
            reasons.append("Independent checks failed")
        state.update(status="NEEDS_REWORK" if reasons else "READY_FOR_REVIEW",
                     reason="; ".join(reasons) or "Checks passed; current chat must review full diff and criteria")
        code = 3 if reasons else 0
    except KeyboardInterrupt:
        state.update(status="INTERRUPTED", reason="Interrupted; artifacts retained")
        code = 130
    except Exception as exc:
        state.update(status="ERROR", reason=artifacts.clean(str(exc)))
        code = 1
    finally:
        if agent:
            agent.close()
        state.update(model_requests=budget.used, input_tokens=budget.input_tokens, output_tokens=budget.output_tokens)
        save(artifacts, state)
    return code


def inspect(config, artifacts, state):
    ws = workspace(state, config, artifacts)
    snap = ws.snapshot()
    current = digest(snap)
    result = {**state, "current_snapshot_digest": current,
              "verification_current": current == state.get("snapshot_digest"),
              "changes": snap}
    print(artifacts.clean(json.dumps(result, ensure_ascii=False, indent=2)))
    return 0 if result["verification_current"] else 3


def apply(config, artifacts, state, contract, review):
    validate(review, REVIEW)
    if state["status"] != "READY_FOR_REVIEW":
        raise ValueError("Only READY_FOR_REVIEW runs can be applied")
    reasons = gate(review, contract, state["checks"], state["executor_complete"])
    if review["decision"] != "PASS" or reasons:
        raise ValueError("Chat review did not pass: " + "; ".join(reasons))
    ws = workspace(state, config, artifacts)
    snap = ws.snapshot()
    if digest(snap) != state["snapshot_digest"] or review["snapshot_digest"] != state["snapshot_digest"]:
        raise ValueError("Review or verification is stale; changed files must be rechecked/reviewed")
    source = repository(state["source_repo"], require_clean=False)
    if git(source, "rev-parse", "HEAD").strip() != state["base_commit"]:
        raise ValueError("Source HEAD changed; integrate manually after fresh review")
    if git(source, "status", "--porcelain", "--untracked-files=all") != state["source_status"]:
        raise ValueError("Source working tree changed; integrate manually after fresh review")
    patch = artifacts.path / "candidate.patch"
    if patch.read_text(encoding="utf-8") != snap["diff"]:
        raise ValueError("Candidate patch does not match the reviewed worktree")
    artifacts.json("chat-review.json", review)
    if snap["diff"]:
        git(source, "apply", "--check", str(patch))
        git(source, "apply", str(patch))
    state.update(status="APPLIED", reason="Chat-approved patch applied to source; no commit/push/deployment",
                 applied_source_status=git(source, "status", "--porcelain", "--untracked-files=all"))
    save(artifacts, state)
    return 0


def rollback(config, artifacts, state):
    """Reverse exactly the reviewed patch, refusing if the source moved afterwards."""
    if state["status"] != "APPLIED":
        raise ValueError("Only an APPLIED run can be rolled back")
    source = repository(state["source_repo"], require_clean=False)
    if git(source, "rev-parse", "HEAD").strip() != state["base_commit"]:
        raise ValueError("Source HEAD changed; restore manually after reviewing the patch")
    patch = artifacts.path / "candidate.patch"
    if not patch.is_file():
        raise ValueError("Candidate patch is missing")
    # The source must still contain exactly the applied patch. This prevents
    # reverse-applying over edits made after acceptance.
    if git(source, "status", "--porcelain", "--untracked-files=all") != state.get("applied_source_status"):
        raise ValueError("Source changed after apply; refusing automatic rollback")
    current = git(source, "diff", "HEAD", "--no-ext-diff", "--no-textconv", ok=(0,))
    if state["mode"] == "branch" and patch.read_text(encoding="utf-8") != current:
        raise ValueError("Source changed after apply; refusing automatic rollback")
    git(source, "apply", "--reverse", "--check", str(patch))
    git(source, "apply", "--reverse", str(patch))
    state.update(status="ROLLED_BACK", reason="Reviewed patch reversed; source returned to its pre-apply state")
    save(artifacts, state)
    return 0
