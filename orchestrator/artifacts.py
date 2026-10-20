from __future__ import annotations

import json
import re
from pathlib import Path


class Artifacts:
    def __init__(self, path: Path, secrets: list[str]):
        self.path = path
        self.secrets = sorted(set(secrets), key=len, reverse=True)
        path.mkdir(parents=True, mode=0o700, exist_ok=False)

    @classmethod
    def open_existing(cls, path: Path, secrets: list[str]):
        if not path.is_dir() or not (path / "summary.json").is_file():
            raise ValueError("Not an existing worker run")
        obj = cls.__new__(cls)
        obj.path = path
        obj.secrets = sorted(set(secrets), key=len, reverse=True)
        return obj

    def clean(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)

    def text(self, name: str, value: str):
        p = self.path / name
        p.write_text(self.clean(value), encoding="utf-8")
        p.chmod(0o600)

    def json(self, name: str, value):
        self.text(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def event(self, kind: str, **data):
        p = self.path / "events.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(self.clean(json.dumps({"kind": kind, **data}, ensure_ascii=False)) + "\n")
        p.chmod(0o600)
