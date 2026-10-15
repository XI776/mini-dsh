from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_harness.agent import AgentLoop
from agent_harness.context import FullHistory
from agent_harness.model import ScriptedModel
from agent_harness.runtime import ReadFileTool, ToolRuntime, WorkspaceFiles, WorkspacePolicy
from agent_harness.session import JsonlSession
from agent_harness.trace_verify import verify_trace


class TraceVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _trace_with_artifact(self) -> Path:
        (self.workspace / "long.txt").write_text("answer=" + "x" * 100)
        with JsonlSession(self.root / "runs") as session:
            runtime = ToolRuntime([ReadFileTool(WorkspaceFiles(self.workspace))], WorkspacePolicy(), session, 8)
            model = ScriptedModel([
                {"tool_calls": [{"name": "read_file", "arguments": {"path": "long.txt"}}],
                 "usage": {"input_tokens": 10, "output_tokens": 2}},
                {"message": "done", "finish": True, "usage": {"input_tokens": 12, "output_tokens": 1}},
            ])
            AgentLoop(model, FullHistory(), runtime, session).run("read long.txt")
            session.append("public_test_result", 2, {"exit_code": 0})
            session.append("evaluation_result", 2, {
                "success": True, "agent_status": "finished", "public_exit_code": 0,
                "holdout_exit_code": None,
            })
            return session.root / "trace.jsonl"

    def test_complete_trace_and_artifact_pass(self) -> None:
        trace = self._trace_with_artifact()
        report = verify_trace(trace)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["model_calls"], 2)
        self.assertEqual(report["tool_calls"], 1)
        self.assertEqual(report["artifacts_checked"], 1)

    def test_detects_broken_references_accounting_and_evaluation(self) -> None:
        trace = self._trace_with_artifact()
        original = [json.loads(line) for line in trace.read_text().splitlines()]
        edits = (
            ("invalid_view_id", "model_request", "view_id", 999),
            ("tool_result_mismatch", "tool_result", "name", "other_tool"),
            ("incomplete_model_usage", "model_response", "usage", {"input_tokens": 10}),
            ("usage_mismatch", "terminal", "usage", {**next(e for e in original if e["type"] == "terminal")["payload"]["usage"], "input_tokens": 999}),
            ("evaluation_mismatch", "evaluation_result", "success", False),
        )
        for code, event_type, field, value in edits:
            with self.subTest(code=code):
                events = json.loads(json.dumps(original))
                next(event for event in events if event["type"] == event_type)["payload"][field] = value
                trace.write_text("\n".join(json.dumps(event) for event in events) + "\n")
                errors = verify_trace(trace)["errors"]
                self.assertIn(code, {error["code"] for error in errors})

    def test_detects_missing_or_modified_artifact_and_incomplete_trace(self) -> None:
        trace = self._trace_with_artifact()
        original_trace = trace.read_text()
        artifact_id = next(
            event["payload"]["artifact_id"] for event in map(json.loads, original_trace.splitlines())
            if event["type"] == "tool_result"
        )
        artifact = trace.parent / "artifacts" / artifact_id
        original_artifact = artifact.read_bytes()
        artifact.write_bytes(b"tampered")
        self.assertIn("artifact_hash_mismatch", {error["code"] for error in verify_trace(trace)["errors"]})
        artifact.unlink()
        self.assertIn("missing_artifact", {error["code"] for error in verify_trace(trace)["errors"]})
        artifact.write_bytes(original_artifact)
        trace.write_text("\n".join(original_trace.splitlines()[:-3]) + "\n")
        self.assertIn("terminal_count", {error["code"] for error in verify_trace(trace)["errors"]})
        trace.write_text("\n".join(original_trace.splitlines()[:-1]) + "\n")
        self.assertIn("evaluation_result_count", {error["code"] for error in verify_trace(trace)["errors"]})

    def test_failed_model_usage_is_reconciled(self) -> None:
        with JsonlSession(self.root / "runs") as session:
            runtime = ToolRuntime([], WorkspacePolicy(), session)
            AgentLoop(
                ScriptedModel([{"error": "provider failed", "usage": {"input_tokens": 7, "output_tokens": 1}}]),
                FullHistory(), runtime, session,
            ).run("task")
            report = verify_trace(session.root / "trace.jsonl")
            self.assertTrue(report["ok"], report["errors"])

    def test_legacy_evaluation_without_holdout_config_remains_verifiable(self) -> None:
        with JsonlSession(self.root / "runs") as session:
            session.append("evaluation_setup", 0, {"case_id": "legacy"})
            runtime = ToolRuntime([], WorkspacePolicy(), session)
            AgentLoop(ScriptedModel([{"finish": True, "message": "done"}]),
                      FullHistory(), runtime, session).run("task")
            session.append("public_test_result", 1, {"exit_code": 0})
            session.append("holdout_test_result", 1, {"exit_code": 0})
            session.append("evaluation_result", 1, {
                "case_id": "legacy", "success": True, "agent_status": "finished",
                "public_exit_code": 0, "holdout_exit_code": 0,
            })
            report = verify_trace(session.root / "trace.jsonl")
            self.assertTrue(report["ok"], report["errors"])


if __name__ == "__main__":
    unittest.main()
