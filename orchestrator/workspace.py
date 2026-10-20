from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def git(root: Path, *args: str, ok=(0,)) -> str:
    p = subprocess.run(["git", "--no-pager", "-c", "core.hooksPath=/dev/null",
                        "-c", "core.fsmonitor=false", *args], cwd=root,
                       text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=60)
    if p.returncode not in ok:
        raise RuntimeError(p.stderr.strip() or f"git {args[0]} failed ({p.returncode})")
    return p.stdout


def repository(path: str, *, require_clean=True) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir() or Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
        raise ValueError("--repo must be the root of a Git repository")
    git(root, "rev-parse", "--verify", "HEAD")
    if require_clean and git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("Target has uncommitted changes; use a clean repository/worktree. No files changed.")
    return root


def push(root: Path) -> str:
    """Push the current branch through its configured upstream, without force."""
    branch = git(root, "branch", "--show-current").strip()
    if not branch:
        raise ValueError("Cannot push a detached HEAD; checkout a named branch first")
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("Refusing preflight push with uncommitted changes")
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", ok=(0, 128)).strip()
    if not upstream:
        raise ValueError("No upstream configured; add a remote and set upstream before cheap-worker")
    return git(root, "push")


class Workspace:
    def __init__(self, root: Path, config: dict, artifacts):
        self.root, self.config, self.artifacts = root, config, artifacts
        self.log_counter = 0
        self.base_commit = git(root, "rev-parse", "HEAD").strip()
        self.base_branch = git(root, "branch", "--show-current").strip()

    def path(self, rel: str, write=False) -> Path:
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError("Use a relative repository path without '..'")
        p = self.root / rel
        for part in [p, *p.parents]:
            if part == self.root:
                break
            if part.is_symlink():
                raise ValueError("Symlink access is blocked")
        p = p.resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError("Path escapes repository")
        parts = p.relative_to(self.root).parts
        secret_patterns = (".env*", "*.key", "*.pem", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                           "credentials*", "secrets*", ".netrc", ".npmrc", ".pypirc")
        protected = {".git", ".ssh", ".aws", ".azure", ".venv", "node_modules", "__pycache__"}
        if any(s in protected or any(fnmatch.fnmatch(s.lower(), pat) for pat in secret_patterns)
               for s in parts):
            raise ValueError("Protected/secret path is blocked")
        if any(fnmatch.fnmatch(str(p.relative_to(self.root)), pat)
               for pat in self.config.get("safety", {}).get("denied_paths", [])):
            raise ValueError("Path blocked by safety.denied_paths")
        if write and (p.name in ("AGENTS.md", ".gitmodules", ".gitlab-ci.yml", ".travis.yml",
                                "Jenkinsfile", "azure-pipelines.yml") or
                      any(s in (".github", ".gitlab", ".codex", ".agents") for s in parts)):
            raise ValueError("Agent instructions, submodules and CI settings are read-only")
        if write and "writable_paths" in self.config.get("safety", {}):
            name = p.relative_to(self.root).as_posix()
            allowed = self.config["safety"]["writable_paths"]
            if not any(name == item or (item.endswith("/") and name.startswith(item)) for item in allowed):
                raise ValueError("Path outside the chat-approved writable_paths")
        return p

    def files(self) -> list[str]:
        names = git(self.root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
        allowed = []
        for name in sorted(set(names) - {""}):
            try:
                if self.path(name).is_file():
                    allowed.append(name)
            except ValueError:
                pass
        return allowed

    def list_files(self, path=".") -> dict:
        directory = self.path(path)
        if not directory.is_dir():
            raise ValueError("Not a directory")
        prefix = "" if path == "." else str(directory.relative_to(self.root)) + "/"
        names = [s for s in self.files() if s.startswith(prefix)]
        return {"files": names[:1000], "truncated": len(names) > 1000}

    def read_file(self, path: str, start_line=1, max_lines=200) -> dict:
        p = self.path(path)
        if type(start_line) is not int or start_line < 1 or type(max_lines) is not int or not 1 <= max_lines <= 500:
            raise ValueError("start_line >= 1 and 1 <= max_lines <= 500 required")
        if p.stat().st_size > self.config["workflow"]["max_file_bytes"]:
            raise ValueError("File exceeds max_file_bytes")
        raw = p.read_bytes()
        if b"\0" in raw:
            raise ValueError("Binary file")
        lines = raw.decode("utf-8").splitlines(keepends=True)
        content = "".join(lines[start_line - 1:start_line - 1 + max_lines])
        limit = min(self.config["workflow"]["max_file_bytes"], 20000)
        return {"path": path, "start_line": start_line, "total_lines": len(lines),
                "content": content[:limit], "truncated": len(content) > limit or
                start_line - 1 + max_lines < len(lines)}

    def write_file(self, path: str, content: str) -> dict:
        p = self.path(path, write=True)
        if not isinstance(content, str) or len(content.encode()) > self.config["workflow"]["max_file_bytes"]:
            raise ValueError("Content must be text within max_file_bytes")
        if p.exists() and (not p.is_file() or p.stat().st_nlink > 1):
            raise ValueError("Only regular files with one hard link may be replaced")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"written": path, "bytes": len(content.encode())}

    def delete_file(self, path: str) -> dict:
        p = self.path(path, write=True)
        if not p.is_file():
            raise ValueError("Only regular files may be deleted")
        p.unlink()
        return {"deleted": path}

    def read_check_log(self, log: str, start_line=1, max_lines=200) -> dict:
        if not re.fullmatch(r"check_[0-9]+\.log", log):
            raise ValueError("Use a check log name returned by the orchestrator")
        if type(start_line) is not int or start_line < 1 or type(max_lines) is not int or not 1 <= max_lines <= 500:
            raise ValueError("Invalid line range")
        p = self.artifacts.path / log
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        content = "".join(lines[start_line - 1:start_line - 1 + max_lines])
        return {"log": log, "content": content[:20000], "total_lines": len(lines),
                "truncated": len(content) > 20000 or start_line - 1 + max_lines < len(lines)}

    def snapshot(self) -> dict:
        if (git(self.root, "rev-parse", "HEAD").strip() != self.base_commit or
                git(self.root, "branch", "--show-current").strip() != self.base_branch):
            raise ValueError("Worktree HEAD/branch changed; automatic commits or switches invalidate acceptance")
        tracked = git(self.root, "diff", "HEAD", "--name-only", "-z", "--no-ext-diff", "--no-textconv").split("\0")
        untracked = git(self.root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        changed = sorted(set(tracked + untracked) - {""})
        hashes = {}
        for name in changed:
            p = self.path(name, write=True)  # Protected changes cannot silently enter review.
            if p.exists():
                if not p.is_file() or p.stat().st_size > self.config["workflow"]["max_file_bytes"]:
                    raise ValueError(f"Changed file too large or unsupported: {name}")
                raw = p.read_bytes()
                if b"\0" in raw:
                    raise ValueError(f"Binary change requires manual review: {name}")
                raw.decode("utf-8")
                hashes[name] = hashlib.sha256(raw).hexdigest()
            else:
                hashes[name] = "DELETED"
        diff = git(self.root, "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--")
        for name in sorted(set(untracked) - {""}):
            # Include new files without staging them in the index.
            diff += git(self.root, "diff", "--no-index", "--no-ext-diff", "--no-textconv",
                        "--", "/dev/null", name, ok=(0, 1))
        return {"changed_files": changed, "hashes": hashes, "diff": diff,
                "status": git(self.root, "status", "--short", "--untracked-files=all")}

    def run_check(self, name: str) -> dict:
        checks = self.config["checks"]
        if name not in checks:
            raise ValueError("Unknown check; choose a configured check name")
        argv = checks[name]["argv"]
        # No model-controlled command suffix and no shell. Never inherit API keys.
        safe_names = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "TMPDIR")
        env = {k: os.environ[k] for k in safe_names if k in os.environ}
        env.update({str(k): str(v) for k, v in self.config.get("safety", {}).get("command_env", {}).items()})
        env["GIT_TERMINAL_PROMPT"] = "0"
        timeout = self.config["workflow"]["command_timeout_seconds"]
        output_limit = self.config["workflow"]["max_check_log_bytes"]
        started = time.monotonic()
        error = None
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(argv, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, start_new_session=True)
            try:
                while True:
                    if time.monotonic() - started > timeout:
                        error = "Check timed out"
                    if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > output_limit:
                        error = "Check output limit exceeded"
                    if error or proc.poll() is not None:
                        break
                    time.sleep(0.05)
            finally:
                # Kill the process group, including children left by test runners.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            out.seek(0)
            err.seek(0)
            stdout = out.read(output_limit).decode("utf-8", errors="replace")
            stderr = err.read(output_limit).decode("utf-8", errors="replace")
        self.log_counter += 1
        log = f"check_{self.log_counter:03d}.log"
        self.artifacts.text(log, f"argv: {argv!r}\nstdout:\n{stdout}\nstderr:\n{stderr}")
        limit = self.config["workflow"]["max_command_output_chars"]
        result = {"name": name, "argv": argv, "returncode": proc.returncode,
                  "stdout": stdout[-limit:], "stderr": stderr[-limit:], "log": log,
                  "output_truncated": len(stdout) > limit or len(stderr) > limit,
                  "seconds": round(time.monotonic() - started, 3)}
        if error:
            result["error"] = error
        self.artifacts.event("check", **result)
        return result
