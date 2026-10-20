from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent.parent


def load_config(path: Path, env_file: Path) -> dict:
    # Explicit path: invoking from another repository must not load its .env.
    load_dotenv(env_file, override=False)
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config must be a YAML mapping")
    execution = config.get("execution", {})
    if not isinstance(execution, dict) or execution.get("mode", "copy") not in ("copy", "branch"):
        raise ValueError("execution.mode must be copy or branch")
    if set(config.get("roles", {})) != {"executor"}:
        raise ValueError("Only the executor API role is supported; planning/review belong to the current chat")
    for name in ("executor",):
        role = config["roles"][name]
        if role.get("api", "chat_completions") not in ("chat_completions", "responses"):
            raise ValueError(f"Invalid API protocol for {name}")
        if not isinstance(role.get("env_prefix"), str):
            raise ValueError(f"Missing env_prefix for {name}")
        if not isinstance(role.get("request_options", {}), dict):
            raise ValueError("request_options must be a mapping")
        reserved = {"model", "messages", "input", "instructions", "tools", "tool_choice",
                    "stream", "max_tokens", "max_completion_tokens", "max_output_tokens"}
        if reserved.intersection(role.get("request_options", {})):
            raise ValueError("request_options cannot override protocol fields or output budget")
        if role.get("max_output_tokens", 0) < 1:
            raise ValueError("max_output_tokens must be positive")
        if role.get("chat_token_parameter", "max_completion_tokens") not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("chat_token_parameter must be max_tokens or max_completion_tokens")
    for key in ("max_model_requests", "max_turns_per_stage", "max_tool_calls_per_stage",
                "command_timeout_seconds", "max_file_bytes", "max_context_chars",
                "max_review_chars", "max_command_output_chars", "max_check_log_bytes",
                "api_timeout_seconds", "max_verification_checks"):
        value = config["workflow"][key]
        if type(value) is not int or value <= 0:
            raise ValueError(f"workflow.{key} must be a positive integer")
    for key in ("max_rework_rounds", "api_max_retries"):
        value = config["workflow"][key]
        if type(value) is not int or value < 0:
            raise ValueError(f"workflow.{key} must be a nonnegative integer")
    checks = config.get("checks", {})
    if not isinstance(checks, dict):
        raise ValueError("checks must be a mapping")
    for name, check in checks.items():
        argv = check.get("argv")
        if not isinstance(name, str) or not isinstance(argv, list) or not argv or not all(
                isinstance(s, str) and s for s in argv):
            raise ValueError("Each check needs a name and a nonempty string argv array")
    required = config.get("required_checks", [])
    if not isinstance(required, list) or not all(isinstance(s, str) and s in checks for s in required):
        raise ValueError("required_checks must name configured checks")
    if not isinstance(config.get("safety", {}).get("command_env", {}), dict):
        raise ValueError("safety.command_env must be a mapping")
    return config


def endpoint(role: dict) -> dict:
    prefix = role["env_prefix"]
    values = {name: os.getenv(f"{prefix}_{name}", "").strip()
              for name in ("BASE_URL", "API_KEY", "MODEL")}
    for name, value in values.items():
        if not value or value.startswith(("replace_", "your-", "https://your-")):
            raise ValueError(f"Fill {prefix}_{name} in the selected env file")
    url = urlsplit(values["BASE_URL"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError(f"{prefix}_BASE_URL must be an HTTP(S) URL without embedded credentials")
    if url.query or url.fragment:
        raise ValueError("BASE_URL must not contain query parameters or fragments")
    return values


def secrets(config: dict) -> list[str]:
    return [os.getenv(f"{role['env_prefix']}_API_KEY", "")
            for role in config["roles"].values()
            if os.getenv(f"{role['env_prefix']}_API_KEY", "")]
