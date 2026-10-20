"""Exercise execute -> chat feedback -> rework -> chat review -> apply, using localhost only."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.cli import run_id
from orchestrator.config import HERE
from tests.fake_provider import server
from tests.helpers import create_repo, invoke, review


def main():
    base = HERE / "runs" / run_id("skill-offline-demo")
    base.mkdir(mode=0o700, parents=True)
    repo = create_repo(base / "sandbox-repo")
    with server("rework") as (url, provider):
        p, state, run = invoke(base / "runner", repo, url)
        print(p.stdout, end="")
        if p.returncode != 3 or state["status"] != "NEEDS_REWORK":
            raise RuntimeError("Expected first-round failing tests")
        feedback = base / "chat-feedback.txt"
        feedback.write_text("Unit test failed because add subtracts. Implement addition, preserve existing tests.")
        p, state, _ = invoke(base / "runner", repo, url, operation=[
            "rework", "--run", str(run), "--feedback-file", str(feedback)])
        print(p.stdout, end="")
        if p.returncode:
            raise RuntimeError(p.stderr)
        p, _, _ = invoke(base / "runner", repo, url, operation=["inspect", "--run", str(run)])
        inspected = json.loads(p.stdout)
        if p.returncode or not inspected["verification_current"]:
            raise RuntimeError("Verification stale")
        # Deterministic simulation of chat-authored review, not real high-model acceptance.
        record = base / "simulated-chat-review.json"
        record.write_text(json.dumps(review(state), ensure_ascii=False, indent=2))
        requests_before_apply = len(provider.requests)
        p, state, _ = invoke(base / "runner", repo, url, operation=[
            "apply", "--run", str(run), "--review-file", str(record)])
        print(p.stdout, end="")
        if p.returncode or len(provider.requests) != requests_before_apply:
            raise RuntimeError("Apply failed or unexpectedly contacted API")
    check = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                           cwd=repo, text=True, capture_output=True)
    (base / "source-verification.log").write_text(check.stdout + check.stderr)
    if check.returncode:
        raise RuntimeError("Source verification failed")
    print(json.dumps({"status": state["status"], "rounds": state["rounds"],
                      "executor_requests": state["model_requests"], "demo": str(base)}, indent=2))
    print("Offline simulation only. No external API or actual chat review was invoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
