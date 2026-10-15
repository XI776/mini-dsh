from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from agent_harness.contracts import ContextView, ModelFailure
from agent_harness.agent import AgentLoop
from agent_harness.deepseek import DeepSeekModel, DeepSeekPricing, load_api_key
from agent_harness.runtime import ToolRuntime, WorkspacePolicy
from agent_harness.session import JsonlSession
from agent_harness.trace_verify import verify_trace


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
    def test_cache_mix_can_reverse_token_and_estimated_cost_order(self) -> None:
        pricing = DeepSeekPricing(cache_hit=0.003, cache_miss=0.15, output=0.6)
        more_tokens = pricing.estimate(input_tokens=1000, output_tokens=100, cache_hit_tokens=900)
        fewer_tokens = pricing.estimate(input_tokens=800, output_tokens=100, cache_hit_tokens=0)
        self.assertLess(800 + 100, 1000 + 100)
        self.assertGreater(fewer_tokens, more_tokens)

    def test_trace_context_view_matches_sent_messages(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            return FakeResponse({
                "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 2},
            })

        class Context:
            name = "spy"

            def build(self, events, task, token_budget):
                return ContextView(({"role": "user", "content": task},
                                    {"role": "tool", "content": "prior result"}), (), self.name)

        with tempfile.TemporaryDirectory() as directory:
            with JsonlSession(Path(directory)) as session:
                result = AgentLoop(
                    DeepSeekModel("test-secret", opener=opener), Context(),
                    ToolRuntime([], WorkspacePolicy(), session), session,
                ).run("task")
                self.assertEqual(result.status.value, "finished")
                view = next(event for event in session.events if event.type == "context_view")
                self.assertEqual(view.payload["messages"], requests[0]["messages"])
                self.assertEqual(view.payload["messages"][1]["role"], "user")
                self.assertTrue(verify_trace(session.root / "trace.jsonl")["ok"])
                records = [json.loads(line) for line in (session.root / "trace.jsonl").read_text().splitlines()]
                next(item for item in records if item["type"] == "context_view")["payload"]["messages"][0]["content"] = "changed"
                tampered = session.root / "tampered.jsonl"
                tampered.write_text("".join(json.dumps(item) + "\n" for item in records))
                codes = {item["code"] for item in verify_trace(tampered)["errors"]}
                self.assertIn("context_view_hash_mismatch", codes)

    def test_max_output_tokens_is_sent_and_snapshotted(self) -> None:
        sent = []
        def opener(request, timeout):
            sent.append(json.loads(request.data))
            return FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                                 "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
        model = DeepSeekModel("test-secret", opener=opener, max_output_tokens=4096)
        model.complete(ContextView(({"role": "user", "content": "task"},), (), "full_history"), [], 5)
        self.assertEqual(sent[0]["max_tokens"], 4096)
        self.assertEqual(model.configuration()["max_output_tokens"], 4096)
        with self.assertRaises(ValueError):
            DeepSeekModel("test-secret", max_output_tokens=0)

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
        self.assertEqual(result.usage.cost_source, "estimated")
        self.assertEqual(result.usage.raw_usage["prompt_cache_miss_tokens"], 25)
        sent = json.loads(requests[0][0].data)
        self.assertEqual(sent["tools"][0]["function"]["name"], "tool_0")
        self.assertEqual(sent["messages"][1]["role"], "user")
        self.assertAlmostEqual(requests[0][1], 12.0, places=3)
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

    def test_missing_cache_breakdown_has_no_price_estimate(self) -> None:
        model = DeepSeekModel("secret", pricing=DeepSeekPricing(0.1, 1.0, 2.0), opener=lambda *a, **k: FakeResponse({
            "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 3},
        }))
        result = model.complete(ContextView(({"role": "user", "content": "x"},), (), "full_history"), [], 1)
        self.assertIsNone(result.usage.cost_usd)
        self.assertFalse(result.usage.cache_usage_known)

    def test_transient_http_error_is_retried_and_counted(self) -> None:
        calls = []
        sleeps = []

        def opener(request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise urllib.error.HTTPError("https://api.deepseek.com", 503, "unavailable", None, None)
            return FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                                 "usage": {"prompt_tokens": 4, "completion_tokens": 1}})

        model = DeepSeekModel("secret", opener=opener, backoff_s=0.5, sleep=sleeps.append)
        result = model.complete(ContextView(({"role": "user", "content": "x"},), (), "full_history"), [], 30.0)
        self.assertTrue(result.finish)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(sleeps, [0.5])
        self.assertLessEqual(calls[1], calls[0])

    def test_retry_stops_at_max_attempts(self) -> None:
        attempts = []

        def opener(*args, **kwargs):
            attempts.append(1)
            raise urllib.error.HTTPError("https://api.deepseek.com", 503, "unavailable", None, None)

        model = DeepSeekModel("secret", opener=opener, max_attempts=2, sleep=lambda _: None)
        with self.assertRaises(ModelFailure) as failure:
            model.complete(ContextView(({"role": "user", "content": "x"},), (), "full_history"), [], 30.0)
        self.assertEqual(str(failure.exception), "DeepSeek HTTP 503")
        self.assertEqual(failure.exception.attempts, 2)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(failure.exception.usage.known)

    def test_retry_never_exceeds_the_caller_time_budget(self) -> None:
        class FakeClock:
            def __init__(self):
                self.now = 0.0

            def __call__(self) -> float:
                return self.now

            def sleep(self, delay: float) -> None:
                self.now += delay

        clock = FakeClock()
        calls = []

        def opener(*args, **kwargs):
            calls.append(clock.now)
            raise urllib.error.HTTPError("https://api.deepseek.com", 503, "unavailable", None, None)

        model = DeepSeekModel("secret", opener=opener, backoff_s=1.0, max_attempts=5,
                              sleep=clock.sleep, clock=clock)
        with self.assertRaises(ModelFailure) as failure:
            model.complete(ContextView(({"role": "user", "content": "x"},), (), "full_history"), [], 3.0)
        # Second backoff would reach the deadline, so the request stops after two attempts.
        self.assertEqual(failure.exception.attempts, 2)
        self.assertEqual(calls, [0.0, 1.0])
        self.assertLessEqual(clock.now, 3.0)

    def test_retry_after_header_bounds_the_backoff(self) -> None:
        sleeps = []

        def opener(request, timeout):
            if not sleeps:
                raise urllib.error.HTTPError("https://api.deepseek.com", 429, "slow down",
                                             {"Retry-After": "2"}, None)
            return FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                                 "usage": {"prompt_tokens": 4, "completion_tokens": 1}})

        model = DeepSeekModel("secret", opener=opener, backoff_s=0.1, sleep=sleeps.append)
        self.assertEqual(model.complete(
            ContextView(({"role": "user", "content": "x"},), (), "full_history"), [], 30.0).attempts, 2)
        self.assertEqual(sleeps, [2.0])

    def test_client_error_is_not_retried(self) -> None:
        attempts = []

        def opener(*args, **kwargs):
            attempts.append(1)
            raise urllib.error.HTTPError("https://api.deepseek.com", 401, "bad-secret", None, None)

        model = DeepSeekModel("secret", opener=opener, sleep=lambda _: None)
        with self.assertRaises(ModelFailure) as failure:
            model.complete(ContextView(({"role": "user", "content": "x"},), (), "full_history"), [], 30.0)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(failure.exception.attempts, 1)

    def test_agent_trace_records_retry_attempts(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError("https://api.deepseek.com", 503, "unavailable", None, None)
            return FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                                 "usage": {"prompt_tokens": 4, "completion_tokens": 1}})

        class Context:
            name = "full_history"

            def build(self, events, task, token_budget):
                return ContextView(({"role": "user", "content": task},), (), self.name)

        with tempfile.TemporaryDirectory() as directory:
            with JsonlSession(Path(directory)) as session:
                result = AgentLoop(
                    DeepSeekModel("test-secret", opener=opener, sleep=lambda _: None), Context(),
                    ToolRuntime([], WorkspacePolicy(), session), session,
                ).run("task")
                self.assertEqual(result.status.value, "finished")
                response = next(event for event in session.events if event.type == "model_response")
                self.assertEqual(response.payload["attempts"], 2)
                self.assertTrue(verify_trace(session.root / "trace.jsonl")["ok"])

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
