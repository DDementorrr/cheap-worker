from __future__ import annotations

import json

from jsonschema import validate


def strings(minimum=0):
    return {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": minimum}


REVIEW = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "snapshot_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "decision": {"enum": ["PASS", "REWORK", "BLOCKED"]},
        "summary": {"type": "string", "minLength": 1},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"severity": {"enum": ["critical", "high", "medium", "low"]},
                           **{k: {"type": "string"} for k in ("file", "issue", "required_fix")}},
            "required": ["severity", "file", "issue", "required_fix"],
        }},
        "criteria_status": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"criterion": {"type": "string"},
                           "status": {"enum": ["PASS", "FAIL", "UNVERIFIED"]},
                           "evidence": {"type": "string", "minLength": 1}},
            "required": ["criterion", "status", "evidence"],
        }},
    }, "required": ["snapshot_digest", "decision", "summary", "findings", "criteria_status"],
}


def parse(text: str, schema: dict) -> dict:
    text = text.strip()
    if text.startswith("```") and text.endswith("```") and "\n" in text:
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    validate(value, schema)
    return value


def gate(review: dict, plan: dict, checks: list[dict], executor_complete: bool) -> list[str]:
    reasons = []
    if not executor_complete:
        reasons.append("Executor did not finish within stage budget")
    if not checks or any(c.get("returncode") != 0 or c.get("error") for c in checks):
        reasons.append("Independent verification checks failed or did not run")
    expected = plan["acceptance_criteria"]
    criteria = review["criteria_status"]
    if (len(criteria) != len(expected) or
            sorted(c["criterion"] for c in criteria) != sorted(expected) or
            any(c["status"] != "PASS" for c in criteria)):
        reasons.append("Review must verify every original acceptance criterion exactly once")
    if any(f["severity"] in ("critical", "high", "medium") for f in review["findings"]):
        reasons.append("Unresolved critical/high/medium review findings")
    return reasons
