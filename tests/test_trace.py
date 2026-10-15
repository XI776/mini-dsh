from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_harness.session import JsonlSession
from agent_harness.trace import divergence_report, first_difference


class TraceDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def trace(
        self, *, strategy: str = "full_history", metadata: dict | None = None,
        messages: list | None = None, tool_name: str = "read_file",
        call_name: str = "read_file", usage: int = 5,
        result_code: str | None = None,
        evaluation_success: bool | None = None,
        model_config: dict | None = None,
    ) -> Path:
        with JsonlSession(self.root / "runs") as session:
            session.append("run_started", 0, {
                "task": "task", "limits": {"steps": 3}, "model": "deepseek:model:url",
                "model_config": model_config or {},
                "context": strategy, "tools": [{"name": tool_name, "parameters": {"type": "object"}}],
            })
            session.append("context_view", 1, {
                "strategy": strategy, "messages": messages or [{"role": "user", "content": "task"}],
                "metadata": metadata or {}, "event_ids": [], "estimated_tokens": 100,
            })
            session.append("model_response", 1, {
                "message": "", "finish": False,
                "tool_calls": [{"name": call_name, "arguments": {"path": "a.py"}, "call_id": "random-" + strategy}],
                "usage": {"input_tokens": usage},
            })
            session.append("tool_request", 1, {
                "name": call_name, "arguments": {"path": "a.py"}, "call_id": "random-" + strategy,
            })
            session.append("tool_result", 1, {
                "name": call_name, "status": "error" if result_code else "ok",
                "output": "denied" if result_code else "source", "error_code": result_code,
                "call_id": "random-" + strategy, "duration_ms": 25 if strategy == "summary" else 5,
            })
            session.append("terminal", 1, {"status": "finished", "reason": "done", "usage": {"input_tokens": usage}})
            if evaluation_success is not None:
                session.append("evaluation_result", 1, {"success": evaluation_success})
            return session.root / "trace.jsonl"

    def test_ignores_strategy_metadata_and_random_ids(self) -> None:
        left = self.trace()
        right = self.trace(strategy="summary", metadata={"summarized_range": None})
        self.assertIsNone(first_difference(left, right))

    def test_context_model_and_usage_divergences_are_distinct(self) -> None:
        left = self.trace()
        different_context = self.trace(messages=[{"role": "user", "content": "changed"}])
        self.assertEqual(first_difference(left, different_context)["reason"], "context_view")
        different_model = self.trace(strategy="summary", call_name="patch_file")
        self.assertEqual(first_difference(left, different_model)["reason"], "model_decision")
        different_usage = self.trace(strategy="summary", usage=9)
        difference = first_difference(left, different_usage)
        self.assertEqual(difference["reason"], "usage_accounting")
        self.assertEqual(difference["step"], 1)

    def test_checks_tool_catalog_and_permission_result(self) -> None:
        left = self.trace()
        different_tools = self.trace(tool_name="write_file")
        mismatch = first_difference(left, different_tools)
        self.assertEqual((mismatch["reason"], mismatch["field"]), ("config_mismatch", "tools"))
        different_model_config = self.trace(model_config={"temperature": 0.7})
        self.assertEqual(first_difference(left, different_model_config)["field"], "model_config")
        denied = self.trace(strategy="summary", result_code="permission_denied")
        self.assertEqual(first_difference(left, denied)["reason"], "permission")

    def test_evaluation_result_is_compared_after_agent_events(self) -> None:
        left = self.trace(evaluation_success=True)
        right = self.trace(strategy="summary", evaluation_success=False)
        self.assertEqual(first_difference(left, right)["reason"], "evaluation")

    def test_report_separates_input_and_behavior_with_trace_locations(self) -> None:
        left = self.trace()
        changed_input = self.trace(strategy="summary", messages=[{"role": "user", "content": "changed"}])
        report = divergence_report(left, changed_input)
        self.assertEqual(report["first_context_difference"]["left_trace"], str(left))
        self.assertIsNotNone(report["first_context_difference"]["left_seq"])
        self.assertIsNone(report["first_observable_behavior_difference"])

        changed_action = self.trace(strategy="summary", call_name="patch_file")
        report = divergence_report(left, changed_action)
        self.assertIsNone(report["first_context_difference"])
        self.assertEqual(report["first_observable_behavior_difference"]["category"], "model_action")
        self.assertEqual(report["first_tool_call_difference"]["category"], "tool_call")
        changed_usage = self.trace(strategy="summary", usage=9)
        report = divergence_report(left, changed_usage)
        self.assertEqual(report["first_usage_difference"]["category"], "usage")
        self.assertEqual(report["first_budget_or_terminal_difference"]["category"], "budget_or_terminal")


if __name__ == "__main__":
    unittest.main()
