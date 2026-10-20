"""Local deterministic OpenAI-compatible test server. No external API or secrets."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TEST = "import unittest\nfrom calc import add\n\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"


class Provider:
    def __init__(self, mode="pass"):
        self.mode = mode
        self.requests = []
        self.execution_rounds = 0
        self.review_rounds = 0

    def answer(self, path, body):
        self.requests.append((path, body))
        responses = path.endswith("/responses")
        if responses:
            assert "messages" not in body
            system, history = body["instructions"], body["input"]
        else:
            assert "input" not in body
            system, history = body["messages"][0]["content"], body["messages"][1:]
        user = next(m["content"] for m in history if m.get("role") == "user")
        tool_count = sum(m.get("role") == "tool" or m.get("type") == "function_call_output" for m in history)
        calls, content = [], ""
        if "compatibility probe" in system:
            if tool_count == 0:
                calls = [("list_files", {"path": "."})]
            else:
                content = "OK"
        elif "economical coding" in system:
            if tool_count == 0:
                self.execution_rounds += 1
                if self.mode == "tool-limit":
                    calls = [("read_file", {"path": "calc.py"})] * 4
                else:
                    # Rework first produces a real failing implementation, second repairs it.
                    wrong = self.mode == "failed-test" or (self.mode == "rework" and self.execution_rounds == 1)
                    impl = "def add(a, b):\n    return a - b\n" if wrong else "def add(a, b):\n    return a + b\n"
                    calls = [("write_file", {"path": "calc.py", "content": impl}),
                             ("write_file", {"path": "tests/test_calc.py", "content": TEST})]
                    if self.mode == "scope":
                        calls.append(("write_file", {"path": "outside.txt", "content": "should be blocked"}))
            else:
                content = "Implemented add and a test. Independent gate must verify it."
        else:
            raise AssertionError("Unknown role")
        if responses:
            output = [{"type": "function_call", "id": f"fc_{i}", "call_id": f"call_{i}",
                       "name": name, "arguments": json.dumps(args), "status": "completed"}
                      for i, (name, args) in enumerate(calls)]
            if content:
                output.append({"type": "message", "id": "msg_test", "role": "assistant",
                               "status": "completed", "content": [{"type": "output_text",
                               "text": content, "annotations": []}]})
            return {"id": "resp_test", "object": "response", "created_at": 1,
                    "status": "completed", "model": body["model"], "output": output,
                    "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}
        msg = {"role": "assistant", "content": content or None}
        if calls:
            msg["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(calls)]
        return {"id": "chat_test", "object": "chat.completion", "created": 1, "model": body["model"],
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


@contextmanager
def server(mode="pass"):
    provider = Provider(mode)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                response = provider.answer(self.path, body)
                payload = json.dumps(response).encode()
                self.send_response(200)
            except Exception as exc:
                payload = json.dumps({"error": {"message": str(exc), "type": type(exc).__name__}}).encode()
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1", provider
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
