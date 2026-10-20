from __future__ import annotations

import json

from jsonschema import validate

from .config import HERE
from .models import Session


def tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}}


STRING = {"type": "string"}
READ_TOOLS = [
    tool("list_files", "List non-secret Git tracked and nonignored files.", {"path": STRING}, []),
    tool("read_file", "Read a UTF-8 file in bounded line ranges.",
         {"path": STRING, "start_line": {"type": "integer", "minimum": 1},
          "max_lines": {"type": "integer", "minimum": 1, "maximum": 500}}, ["path"]),
]
WRITE_TOOLS = [
    tool("write_file", "Create/replace one UTF-8 file. Inspect existing files first.",
         {"path": STRING, "content": STRING}, ["path", "content"]),
    tool("delete_file", "Delete one regular file inside the worktree.", {"path": STRING}, ["path"]),
    tool("run_check", "Run one check from the configured catalog, without extra arguments.",
         {"name": STRING}, ["name"]),
]
LOG_TOOL = tool("read_check_log", "Read an orchestrator check log in bounded line ranges.",
                {"log": STRING, "start_line": {"type": "integer", "minimum": 1},
                 "max_lines": {"type": "integer", "minimum": 1, "maximum": 500}}, ["log"])


class Agent:
    def __init__(self, role, stage, config, artifacts, budget, workspace, user, extra_prompt=""):
        self.stage, self.config, self.artifacts, self.workspace = stage, config, artifacts, workspace
        if role != "executor":
            raise ValueError("Only executor sessions are supported")
        self.tools = READ_TOOLS + WRITE_TOOLS + [LOG_TOOL]
        system = (HERE / "prompts" / f"{role}.md").read_text(encoding="utf-8") + extra_prompt
        self.session = Session(role, config, artifacts, budget, system, user, self.tools)
        self.turns, self.calls = 0, 0

    def finish(self) -> tuple[str, bool]:
        workflow = self.config["workflow"]
        while self.turns < workflow["max_turns_per_stage"]:
            self.turns += 1
            text, calls = self.session.request()
            if not calls:
                return text, bool(text.strip())
            for call in calls:
                if self.calls >= workflow["max_tool_calls_per_stage"]:
                    return "Tool-call budget exhausted", False
                self.calls += 1
                try:
                    spec = next((s for s in self.tools if s["function"]["name"] == call["name"]), None)
                    if spec is None:
                        raise ValueError("Tool unavailable to this role")
                    args = json.loads(call["arguments"])
                    validate(args, spec["function"]["parameters"])
                    result = getattr(self.workspace, call["name"])(**args)
                except Exception as exc:
                    result = {"error": str(exc)[:1500]}
                self.artifacts.event("tool", stage=self.stage, name=call["name"], result=result)
                self.session.result(call["id"], result)
        return "Stage-turn budget exhausted", False

    def close(self):
        self.session.close()
