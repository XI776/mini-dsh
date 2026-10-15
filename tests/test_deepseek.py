from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from agent_harness.contracts import ContextView, ModelFailure
from agent_harness.deepseek import DeepSeekModel, DeepSeekPricing, load_api_key


class FakeResponse:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class DeepSeekTests(unittest.TestCase):
    def test_dotenv_key_and_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# comment\nexport DEEPSEEK_API_KEY='file-secret'\n")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
                self.assertEqual(load_api_key(path), "file-secret")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "process-secret"}):
                self.assertEqual(load_api_key(path), "process-secret")

    def test_tool_call_mapping_and_usage(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "content": None, "tool_calls": [{"id": "call_1", "function": {
                        "name": "tool_0", "arguments": '{"text":"hello"}',
                    }}],
                }}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                          "prompt_cache_hit_tokens": 75, "prompt_cache_miss_tokens": 25},
            })

        model = DeepSeekModel("test-secret", pricing=DeepSeekPricing(0.1, 1.0, 2.0), opener=opener)
        view = ContextView(({"role": "user", "content": "test"},
                            {"role": "tool", "content": "old result"}), (), "full_history")
        result = model.complete(view, [{"name": "mcp.echo.say", "parameters": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}], 12.0)
        self.assertFalse(result.finish)
        self.assertEqual(result.tool_calls[0].name, "mcp.echo.say")
        self.assertEqual(result.tool_calls[0].arguments, {"text": "hello"})
        self.assertEqual(result.usage.cache_read_tokens, 75)
        self.assertTrue(result.usage.cache_usage_known)
        self.assertAlmostEqual(result.usage.cost_usd, (75 * 0.1 + 25 * 1 + 20 * 2) / 1_000_000)
        sent = json.loads(requests[0][0].data)
        self.assertEqual(sent["tools"][0]["function"]["name"], "tool_0")
        self.assertEqual(sent["messages"][1]["role"], "user")
        self.assertEqual(requests[0][1], 12.0)
        self.assertNotIn("test-secret", json.dumps(sent))

    def test_bad_completion_keeps_reported_usage(self) -> None:
        model = DeepSeekModel("secret", opener=lambda *a, **k: FakeResponse({
            "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
                "id": "id", "function": {"name": "read_file", "arguments": "not-json"},
            }]}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2},
        }))
        with self.assertRaises(ModelFailure) as failure:
            model.complete(ContextView(({"role": "user", "content": "x"},), (), "full_history"),
                           [{"name": "read_file", "parameters": {"type": "object"}}], 1)
        self.assertEqual(failure.exception.usage.total_tokens, 11)
        self.assertFalse(failure.exception.usage.cache_usage_known)

    def test_http_error_excludes_response_body_and_key(self) -> None:
        def opener(*args, **kwargs):
            raise urllib.error.HTTPError("https://api.deepseek.com", 401, "bad-secret", None, None)

        model = DeepSeekModel("bad-secret", opener=opener)
        with self.assertRaises(ModelFailure) as failure:
            model.complete(ContextView(({"role": "user", "content": "x"},), (), "full_history"), [], 1)
        self.assertEqual(str(failure.exception), "DeepSeek HTTP 401")
        self.assertFalse(failure.exception.usage.known)


if __name__ == "__main__":
    unittest.main()
