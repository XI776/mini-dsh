from __future__ import annotations

import tempfile
import unittest
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_harness.agent import AgentLoop, BudgetLimits
from agent_harness.cli import main
from agent_harness.context import FullHistory
from agent_harness.contracts import TerminalStatus, ToolResult
from agent_harness.model import ScriptedModel
from agent_harness.resume import workspace_digest
from agent_harness.runtime import (
    GitTool, PatchFileTool, ReadFileTool, ToolRuntime, WorkspaceFiles, WorkspacePolicy, WriteFileTool,
)
from agent_harness.session import JsonlSession, read_trace
from agent_harness.trace_verify import verify_trace


RESPONSES = [
    {"tool_calls": [{"name": "write_file", "arguments": {"path": "answer.txt", "content": "done"}}],
     "usage": {"input_tokens": 4, "output_tokens": 2}},
    {"message": "complete", "finish": True, "usage": {"input_tokens": 3, "output_tokens": 1}},
]


class InterruptedSession(JsonlSession):
    def __init__(self, *args, interrupt_type: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.interrupt_type = interrupt_type

    def append(self, event_type, step, payload):
        event = super().append(event_type, step, payload)
        if event_type == self.interrupt_type and step == 1:
            raise KeyboardInterrupt("simulated process interruption")
        return event


class CountingWrite(WriteFileTool):
    def __init__(self, files, counter):
        super().__init__(files)
        self.counter = counter

    def execute(self, arguments, timeout_s):
        self.counter.append("executed")
        return super().execute(arguments, timeout_s)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.traces = self.root / "traces"
        self.counter = []

    def _loop(self, session):
        tool = CountingWrite(WorkspaceFiles(self.workspace), self.counter)
        runtime = ToolRuntime([tool], WorkspacePolicy(True), session)
        return AgentLoop(
            ScriptedModel(RESPONSES), FullHistory(), runtime, session,
            BudgetLimits(steps=2, tokens=100, tool_calls=1, wall_seconds=60),
            checkpoint=lambda: workspace_digest(self.workspace, self.traces),
        )

    def _interrupt(self, event_type):
        with InterruptedSession(self.traces, interrupt_type=event_type) as session:
            run_id = session.run_id
            with self.assertRaises(KeyboardInterrupt):
                self._loop(session).run("write answer")
        return run_id

    def test_resume_after_completed_write_does_not_repeat_it(self):
        run_id = self._interrupt("workspace_checkpoint")
        self.assertEqual(self.counter, ["executed"])
        with JsonlSession.open_existing(self.traces, run_id) as session:
            result = self._loop(session).run(resume=True)
        self.assertEqual(result.status, TerminalStatus.FINISHED)
        self.assertEqual(result.usage.steps, 2)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(self.counter, ["executed"])
        self.assertEqual((self.workspace / "answer.txt").read_text(), "done")
        trace = self.traces / run_id / "trace.jsonl"
        self.assertEqual(
            [event["payload"]["state"] for event in read_trace(trace) if event["type"] == "tool_state"],
            ["pending", "running", "completed"],
        )
        self.assertTrue(verify_trace(trace)["ok"])

    def test_unknown_tool_result_fails_without_reexecution(self):
        run_id = self._interrupt("tool_request")
        self.assertEqual(self.counter, [])
        with JsonlSession.open_existing(self.traces, run_id) as session:
            result = self._loop(session).run(resume=True)
        self.assertEqual(result.status, TerminalStatus.FAILED)
        self.assertEqual(result.reason, "unknown_tool_result")
        self.assertEqual(self.counter, [])
        trace = self.traces / run_id / "trace.jsonl"
        self.assertEqual(
            [event["payload"]["state"] for event in read_trace(trace) if event["type"] == "tool_state"],
            ["pending", "unknown"],
        )
        self.assertTrue(verify_trace(trace)["ok"])

    def test_workspace_change_blocks_resume(self):
        run_id = self._interrupt("workspace_checkpoint")
        (self.workspace / "answer.txt").write_text("external change")
        with JsonlSession.open_existing(self.traces, run_id) as session:
            result = self._loop(session).run(resume=True)
        self.assertEqual(result.status, TerminalStatus.FAILED)
        self.assertEqual(result.reason, "workspace_changed")
        self.assertEqual(self.counter, ["executed"])
        self.assertTrue(verify_trace(self.traces / run_id / "trace.jsonl")["ok"])

    def test_unknown_model_result_is_accounted_and_not_retried(self):
        run_id = self._interrupt("model_request")
        with JsonlSession.open_existing(self.traces, run_id) as session:
            result = self._loop(session).run(resume=True)
        self.assertEqual(result.status, TerminalStatus.FAILED)
        self.assertEqual(result.reason, "unknown_model_result")
        self.assertEqual(result.usage.unknown_usage_calls, 1)
        self.assertEqual(self.counter, [])
        self.assertTrue(verify_trace(self.traces / run_id / "trace.jsonl")["ok"])

    def test_failed_external_tool_blocks_resume(self):
        class ExternalTool:
            name = "external"
            side_effect = "write"
            is_external = True
            schema = {"type": "object", "properties": {}, "required": []}

            def execute(inner, arguments, timeout_s):
                self.counter.append("attempted")
                return ToolResult("", "error", "remote result uncertain", "timeout")

        responses = [
            {"tool_calls": [{"name": "external", "arguments": {}}],
             "usage": {"input_tokens": 2, "output_tokens": 1}},
            {"message": "done", "finish": True, "usage": {"input_tokens": 2, "output_tokens": 1}},
        ]

        def loop(session):
            runtime = ToolRuntime([ExternalTool()], WorkspacePolicy(True), session)
            return AgentLoop(ScriptedModel(responses), FullHistory(), runtime, session,
                             checkpoint=lambda: workspace_digest(self.workspace, self.traces))

        with InterruptedSession(self.traces, interrupt_type="workspace_checkpoint") as session:
            run_id = session.run_id
            with self.assertRaises(KeyboardInterrupt):
                loop(session).run("call external")
        with JsonlSession.open_existing(self.traces, run_id) as session:
            result = loop(session).run(resume=True)
        self.assertEqual(result.reason, "external_tool_result_uncertain")
        self.assertEqual(self.counter, ["attempted"])
        self.assertTrue(verify_trace(self.traces / run_id / "trace.jsonl")["ok"])

    def test_incomplete_jsonl_line_refuses_resume(self):
        run_id = self._interrupt("tool_request")
        with (self.traces / run_id / "trace.jsonl").open("ab") as stream:
            stream.write(b'{"seq":')
        with self.assertRaisesRegex(ValueError, "incomplete final line"):
            JsonlSession.open_existing(self.traces, run_id)
        self.assertEqual(self.counter, [])

    def test_partial_tool_batch_is_not_continued(self):
        responses = [{
            "tool_calls": [
                {"name": "write_file", "arguments": {"path": "first.txt", "content": "one"}},
                {"name": "write_file", "arguments": {"path": "second.txt", "content": "two"}},
            ], "usage": {"input_tokens": 4, "output_tokens": 2},
        }]

        def loop(session):
            tool = CountingWrite(WorkspaceFiles(self.workspace), self.counter)
            runtime = ToolRuntime([tool], WorkspacePolicy(True), session)
            return AgentLoop(ScriptedModel(responses), FullHistory(), runtime, session,
                             checkpoint=lambda: workspace_digest(self.workspace, self.traces))

        with InterruptedSession(self.traces, interrupt_type="workspace_checkpoint") as session:
            run_id = session.run_id
            with self.assertRaises(KeyboardInterrupt):
                loop(session).run("write two files")
        with JsonlSession.open_existing(self.traces, run_id) as session:
            result = loop(session).run(resume=True)
        self.assertEqual(result.reason, "pending_tool_calls")
        self.assertEqual(self.counter, ["executed"])
        self.assertFalse((self.workspace / "second.txt").exists())
        self.assertTrue(verify_trace(self.traces / run_id / "trace.jsonl")["ok"])

    def test_cli_resume_continues_script_at_checkpoint(self):
        script = self.root / "script.json"
        script.write_text(json.dumps(RESPONSES))
        with InterruptedSession(self.traces, interrupt_type="workspace_checkpoint") as session:
            run_id = session.run_id
            files = WorkspaceFiles(self.workspace)
            runtime = ToolRuntime([
                ReadFileTool(files), WriteFileTool(files), PatchFileTool(files),
                GitTool(self.workspace, session),
            ], WorkspacePolicy(True), session)
            loop = AgentLoop(ScriptedModel(RESPONSES), FullHistory(), runtime, session,
                             checkpoint=lambda: workspace_digest(self.workspace, self.traces))
            with self.assertRaises(KeyboardInterrupt):
                loop.run("write answer")

        output = io.StringIO()
        argv = ["agent-harness", "resume", "--run-id", run_id, "--script", str(script),
                "--workspace", str(self.workspace), "--traces", str(self.traces), "--allow-writes"]
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())["status"], "finished")
        self.assertEqual((self.workspace / "answer.txt").read_text(), "done")
        trace = self.traces / run_id / "trace.jsonl"
        self.assertEqual(sum(event["type"] == "tool_request" for event in read_trace(trace)), 1)
        self.assertTrue(verify_trace(trace)["ok"])

    def test_checkpoint_ignores_trace_directory_but_checks_symlink(self):
        trace_root = self.workspace / ".runs"
        trace_root.mkdir()
        (trace_root / "trace.jsonl").write_text("first")
        before = workspace_digest(self.workspace, trace_root)
        (trace_root / "trace.jsonl").write_text("second")
        self.assertEqual(workspace_digest(self.workspace, trace_root), before)
        (self.workspace / "link").symlink_to("target-a")
        with_link = workspace_digest(self.workspace, trace_root)
        self.assertNotEqual(with_link, before)
        (self.workspace / "link").unlink()
        (self.workspace / "link").symlink_to("target-b")
        self.assertNotEqual(workspace_digest(self.workspace, trace_root), with_link)

    def test_resume_preserves_wall_clock_budget(self):
        run_id = self._interrupt("workspace_checkpoint")
        with JsonlSession.open_existing(self.traces, run_id) as session:
            with patch("agent_harness.agent.datetime") as clock:
                clock.now.return_value = datetime.now(timezone.utc) + timedelta(seconds=120)
                clock.fromisoformat.side_effect = datetime.fromisoformat
                result = self._loop(session).run(resume=True)
        self.assertEqual(result.status, TerminalStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.reason, "wall_seconds")
        self.assertEqual(result.usage.steps, 1)
        self.assertEqual(self.counter, ["executed"])
        self.assertTrue(verify_trace(self.traces / run_id / "trace.jsonl")["ok"])

    def test_changed_budget_blocks_resume(self):
        run_id = self._interrupt("workspace_checkpoint")
        with JsonlSession.open_existing(self.traces, run_id) as session:
            loop = self._loop(session)
            loop.limits = BudgetLimits(steps=3, tokens=100, tool_calls=1, wall_seconds=60)
            result = loop.run(resume=True)
        self.assertEqual(result.status, TerminalStatus.FAILED)
        self.assertEqual(result.reason, "configuration_changed")
        self.assertEqual(self.counter, ["executed"])
        self.assertTrue(verify_trace(self.traces / run_id / "trace.jsonl")["ok"])
