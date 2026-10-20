from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAI

from .config import endpoint


@dataclass
class Budget:
    limit: int
    used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def charge(self):
        if self.used >= self.limit:
            raise RuntimeError("Global model request budget exhausted")
        self.used += 1


class Session:
    """Protocol adapter: one independent conversation per role/stage."""

    def __init__(self, name, config, artifacts, budget, system, user, tools):
        self.name, self.config, self.artifacts, self.budget = name, config, artifacts, budget
        role = config["roles"][name]
        ep = endpoint(role)
        self.role, self.model, self.api = role, ep["MODEL"], role["api"]
        self.client = OpenAI(base_url=ep["BASE_URL"], api_key=ep["API_KEY"],
                             timeout=config["workflow"]["api_timeout_seconds"],
                             max_retries=config["workflow"]["api_max_retries"])
        self.system, self.tools = system, tools
        self.history = ([{"role": "system", "content": system}] if self.api == "chat_completions" else [])
        self.user(user)

    def close(self):
        self.client.close()

    def user(self, content):
        self.history.append({"role": "user", "content": content})

    def request(self):
        limit = self.config["workflow"]["max_context_chars"]
        if len(json.dumps(self.history, ensure_ascii=False)) + len(self.system) > limit:
            raise RuntimeError("Conversation exceeds max_context_chars; split the task")
        self.budget.charge()
        options = dict(self.role.get("request_options", {}))
        try:
            if self.api == "chat_completions":
                options[self.role.get("chat_token_parameter", "max_completion_tokens")] = self.role["max_output_tokens"]
                response = self.client.chat.completions.create(
                    model=self.model, messages=self.history, tools=self.tools,
                    **options)
                if not response.choices or response.choices[0].finish_reason in ("length", "content_filter"):
                    raise RuntimeError("Model returned truncated/filtered output")
                msg = response.choices[0].message
                if msg.refusal:
                    raise RuntimeError("Model refused the request")
                self.history.append(msg.model_dump(exclude_none=True))
                calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                         for c in msg.tool_calls or []]
                content = msg.content or ""
                usage = response.usage
                inp = usage.prompt_tokens if usage else 0
                out = usage.completion_tokens if usage else 0
            else:
                functions = [{"type": "function", **t["function"], "strict": False} for t in self.tools]
                options.setdefault("store", False)
                options.setdefault("include", ["reasoning.encrypted_content"])
                response = self.client.responses.create(
                    model=self.model, instructions=self.system, input=self.history, tools=functions,
                    max_output_tokens=self.role["max_output_tokens"], **options)
                if response.status != "completed":
                    raise RuntimeError(f"Responses request did not complete: {response.status}")
                if any(part.type == "refusal" for item in response.output if item.type == "message"
                       for part in item.content):
                    raise RuntimeError("Model refused the request")
                self.history.extend(item.model_dump(exclude_none=True) for item in response.output)
                calls = [{"id": item.call_id, "name": item.name, "arguments": item.arguments}
                         for item in response.output if item.type == "function_call"]
                content = response.output_text
                usage = response.usage
                inp = usage.input_tokens if usage else 0
                out = usage.output_tokens if usage else 0
        except Exception as exc:
            # Provider bodies can contain credentials/requests. Keep diagnostic type/status only.
            if isinstance(exc, RuntimeError):
                raise
            status = getattr(exc, "status_code", None)
            raise RuntimeError(f"{self.name} API failed: {type(exc).__name__}, status={status}; "
                               "check protocol, URL, model ID and key") from None
        self.budget.input_tokens += inp
        self.budget.output_tokens += out
        self.artifacts.event("model", role=self.name, api=self.api, model=self.model,
                             request=self.budget.used, input_tokens=inp, output_tokens=out,
                             text=content, tool_calls=calls)
        return content, calls

    def result(self, call_id, result):
        content = self.artifacts.clean(json.dumps(result, ensure_ascii=False))
        if self.api == "chat_completions":
            self.history.append({"role": "tool", "tool_call_id": call_id, "content": content})
        else:
            self.history.append({"type": "function_call_output", "call_id": call_id, "output": content})
