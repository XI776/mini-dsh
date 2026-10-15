from __future__ import annotations

import json
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path

from agent_harness.agent import AgentLoop, BudgetLimits
from agent_harness.context import FullHistory, PythonSymbolContext, RepositoryContext, RepositoryIndex, RetrievalSummaryContext, SlidingWindow, SummaryContext
from agent_harness.contracts import ContextView, Event, ModelResult, TerminalStatus, ToolCall
from agent_harness.model import ScriptedModel
from agent_harness.plugins import PluginGroup
from agent_harness.runtime import ReadFileTool, ShellTool, ToolRuntime, WorkspaceFiles, WorkspacePolicy, WriteFileTool
from agent_harness.session import JsonlSession, read_trace
from agent_harness.trace import first_difference


class HarnessTests(unittest.TestCase):
    def test_protocol_injection_accepts_memory_session_runtime_and_event_sink(self) -> None:
        class MemorySession:
            run_id = "memory"

            def __init__(self):
                self.saved = []

            @property
            def events(self):
                return tuple(self.saved)

            def append(self, event_type, step, payload):
                event = Event(self.run_id, len(self.saved) + 1, event_type, step, payload, "2026-01-01T00:00:00+00:00", 0)
                self.saved.append(event)
                return event

        class MemoryRuntime:
            permission = WorkspacePolicy()
            tools = {}

            def schemas(self):
                return []

            def execute(self, call, timeout_s):
                raise AssertionError("no tool calls expected")

        class Context:
            name = "memory"

            def build(self, events, task, token_budget):
                return ContextView(({"role": "user", "content": task},), (), self.name)

        class Model:
            identity = "memory-model"

            def complete(self, view, tool_schemas, timeout_s):
                return ModelResult("done", finish=True)

        class Sink:
            def __init__(self):
                self.events = []

            def emit(self, event):
                self.events.append(event)

        session, sink = MemorySession(), Sink()
        result = AgentLoop(Model(), Context(), MemoryRuntime(), session, event_sink=sink).run("task")
        self.assertEqual(result.status, TerminalStatus.FINISHED)
        self.assertEqual(sink.events, session.saved)

        class BrokenSink:
            def emit(self, event):
                raise RuntimeError("sink unavailable")

        recovery_session = MemorySession()
        recovered = AgentLoop(
            Model(), Context(), MemoryRuntime(), recovery_session, event_sink=BrokenSink()
        ).run("task")
        self.assertEqual(recovered.status, TerminalStatus.FINISHED)
        self.assertEqual(sum(event.type == "event_sink_error" for event in recovery_session.events), 1)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def session(self) -> JsonlSession:
        session = JsonlSession(self.root / "runs")
        self.addCleanup(session.close)
        return session

    def runtime(self, session: JsonlSession, allow_writes: bool = False) -> ToolRuntime:
        files = WorkspaceFiles(self.workspace)
        return ToolRuntime([ReadFileTool(files), WriteFileTool(files)], WorkspacePolicy(allow_writes), session)

    def test_multistep_run_records_context_and_single_terminal(self) -> None:
        session = self.session()
        model = ScriptedModel([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "answer.txt", "content": "42"}}],
             "usage": {"input_tokens": 10, "output_tokens": 2}},
            {"message": "done", "finish": True, "usage": {"input_tokens": 12, "output_tokens": 1}},
        ])
        result = AgentLoop(model, FullHistory(), self.runtime(session, True), session).run("write answer")
        self.assertEqual(result.status, TerminalStatus.FINISHED)
        self.assertEqual(result.usage.tokens, 25)
        self.assertEqual((self.workspace / "answer.txt").read_text(), "42")
        events = read_trace(session.root / "trace.jsonl")
        self.assertEqual(sum(event["type"] == "terminal" for event in events), 1)
        self.assertEqual(sum(event["type"] == "context_view" for event in events), 2)
        self.assertEqual([tool["name"] for tool in events[0]["payload"]["tools"]], ["read_file", "write_file"])

    def test_budgets_and_failed_model_usage(self) -> None:
        session = self.session()
        model = ScriptedModel([{"error": "rate limited", "usage": {"input_tokens": 7, "output_tokens": 1}}])
        result = AgentLoop(model, FullHistory(), self.runtime(session), session).run("task")
        self.assertEqual(result.status, TerminalStatus.FAILED)
        self.assertEqual(result.usage.tokens, 8)

        limited = self.session()
        model = ScriptedModel([{"tool_calls": [{"name": "read_file", "arguments": {"path": "x"}}]}])
        result = AgentLoop(model, FullHistory(), self.runtime(limited), limited, BudgetLimits(tool_calls=0)).run("task")
        self.assertEqual(result.status, TerminalStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.reason, "tool_calls")

    def test_exact_token_limit_and_cancellation(self) -> None:
        exact = self.session()
        result = AgentLoop(
            ScriptedModel([{"message": "done", "finish": True, "usage": {"input_tokens": 9, "output_tokens": 1}}]),
            FullHistory(), self.runtime(exact), exact, BudgetLimits(tokens=10),
        ).run("task")
        self.assertEqual(result.status, TerminalStatus.FINISHED)
        cancelled = self.session()
        signal = threading.Event()
        signal.set()
        result = AgentLoop(ScriptedModel([]), FullHistory(), self.runtime(cancelled), cancelled, cancel=signal).run("task")
        self.assertEqual(result.status, TerminalStatus.CANCELLED)
        self.assertEqual(len([event for event in cancelled.events if event.type == "terminal"]), 1)

    def test_tool_validation_permission_and_output_artifact(self) -> None:
        (self.workspace / "long.txt").write_text("x" * 50)
        session = self.session()
        runtime = ToolRuntime([ReadFileTool(WorkspaceFiles(self.workspace)), WriteFileTool(WorkspaceFiles(self.workspace))], WorkspacePolicy(), session, 10)
        denied = runtime.execute(ToolCall("write_file", {"path": "x", "content": "x"}), 1)
        self.assertEqual(denied.error_code, "permission_denied")
        invalid = runtime.execute(ToolCall("read_file", {"path": 1}), 1)
        self.assertEqual(invalid.error_code, "invalid_arguments")
        result = runtime.execute(ToolCall("read_file", {"path": "long.txt"}), 1)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.output), 10)
        self.assertEqual((session.root / "artifacts" / result.artifact_id).read_text(), "x" * 50)

    def test_path_escape_and_symlink_are_rejected(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("secret")
        os.symlink(outside, self.workspace / "link.txt")
        runtime = self.runtime(self.session())
        for path in ("../outside.txt", "link.txt", str(outside)):
            result = runtime.execute(ToolCall("read_file", {"path": path}), 1)
            self.assertEqual(result.status, "error", path)

    def test_file_size_limit_prevents_large_read_and_write(self) -> None:
        (self.workspace / "large.txt").write_text("12345")
        files = WorkspaceFiles(self.workspace, max_file_bytes=4)
        with JsonlSession(self.root / "limited-runs") as session:
            runtime = ToolRuntime([ReadFileTool(files), WriteFileTool(files)], WorkspacePolicy(True), session)
            read = runtime.execute(ToolCall("read_file", {"path": "large.txt"}), 1)
            write = runtime.execute(ToolCall("write_file", {"path": "large.txt", "content": "abcdef"}), 1)
            self.assertEqual(read.status, "error")
            self.assertEqual(write.status, "error")
            self.assertEqual((self.workspace / "large.txt").read_text(), "12345")

    def test_credentials_are_blocked_and_redacted_from_session(self) -> None:
        secret = "ds-test-secret-0123456789"
        (self.workspace / ".env").write_text("DEEPSEEK_API_KEY=" + secret)
        (self.workspace / "custom-key.txt").write_text(secret)
        files = WorkspaceFiles(self.workspace, [self.workspace / "custom-key.txt"])
        with JsonlSession(self.root / "safe-runs", secrets=(secret,)) as session:
            runtime = ToolRuntime([ReadFileTool(files), WriteFileTool(files)], WorkspacePolicy(True), session)
            for name in (".env", "custom-key.txt"):
                self.assertEqual(runtime.execute(ToolCall("read_file", {"path": name}), 1).status, "error")
                self.assertEqual(runtime.execute(ToolCall("write_file", {"path": name, "content": "x"}), 1).status, "error")
            event = session.append("tool_result", 1, {"output": "token=" + secret})
            self.assertEqual(event.payload["output"], "token=[REDACTED]")
            content = b"x" * (1024 * 1024 - 7) + secret.encode() + b"suffix"
            artifact_id = session.save_artifact_stream(io.BytesIO(content))
            self.assertNotIn(secret, (session.root / "artifacts" / artifact_id).read_text())
            self.assertNotIn(secret, (session.root / "trace.jsonl").read_text())

    def test_context_is_redacted_before_model_call(self) -> None:
        secret = "ds-test-secret-0123456789"

        class SecretContext:
            name = "secret_context"

            def build(self, events, task, token_budget):
                return ContextView(({"role": "user", "content": task},
                                    {"role": "system", "content": "repository hit: " + secret}),
                                   (), self.name)

        class SpyModel:
            def complete(self, view, tool_schemas, timeout_s):
                self.view = view
                return ModelResult("done " + secret, finish=True)

        model = SpyModel()
        with JsonlSession(self.root / "redacted-runs", secrets=(secret,)) as session:
            result = AgentLoop(model, SecretContext(), self.runtime(session), session).run("task " + secret)
            self.assertEqual(result.status, TerminalStatus.FINISHED)
            self.assertNotIn(secret, str(model.view.messages))
            self.assertIn("[REDACTED]", str(model.view.messages))
            self.assertEqual(result.answer, "done [REDACTED]")
            self.assertNotIn(secret, (session.root / "trace.jsonl").read_text())

    def test_shell_timeout(self) -> None:
        session = self.session()
        runtime = ToolRuntime([ShellTool(self.workspace, {"sleep"}, session)], WorkspacePolicy(True), session)
        result = runtime.execute(ToolCall("shell", {"argv": ["sleep", "2"]}), 0.05)
        self.assertEqual(result.error_code, "timeout")

    def test_plugin_startup_rollback(self) -> None:
        log: list[str] = []

        class TestPlugin:
            def __init__(self, name: str, requires: tuple[str, ...] = (), fail: bool = False):
                self.name, self.requires, self.fail = name, requires, fail

            def start(self) -> None:
                log.append("start:" + self.name)
                if self.fail:
                    raise RuntimeError("failed")

            def close(self) -> None:
                log.append("close:" + self.name)

        group = PluginGroup([TestPlugin("a"), TestPlugin("b", ("a",), True)])
        with self.assertRaises(RuntimeError):
            group.start()
        self.assertEqual(log, ["start:a", "start:b", "close:b", "close:a"])
        with self.assertRaises(ValueError):
            PluginGroup([TestPlugin("a", ("b",)), TestPlugin("b", ("a",))]).ordered()

    def test_plugin_lifecycle_dependency_and_cleanup_errors(self) -> None:
        calls: list[str] = []

        class Plugin:
            def __init__(self, name: str, requires: tuple[str, ...] = (), close_fails: bool = False):
                self.name, self.requires, self.close_fails = name, requires, close_fails

            def start(self) -> None:
                calls.append("start:" + self.name)

            def close(self) -> None:
                calls.append("close:" + self.name)
                if self.close_fails:
                    raise RuntimeError(self.name)

        group = PluginGroup([Plugin("dependent", ("base",)), Plugin("base")])
        group.start()
        with self.assertRaises(RuntimeError):
            group.start()
        group.close()
        group.close()
        self.assertEqual(calls, ["start:base", "start:dependent", "close:dependent", "close:base"])
        with self.assertRaisesRegex(ValueError, "missing plugin dependency"):
            PluginGroup([Plugin("dependent", ("missing",))]).start()
        with self.assertRaisesRegex(ValueError, "duplicate plugin name"):
            PluginGroup([Plugin("same"), Plugin("same")]).start()

        calls.clear()
        failing_close = PluginGroup([Plugin("base", close_fails=True), Plugin("dependent", ("base",))])
        failing_close.start()
        with self.assertRaises(ExceptionGroup):
            failing_close.close()
        self.assertEqual(calls[-2:], ["close:dependent", "close:base"])

    def test_context_strategies_and_trace_diff(self) -> None:
        (self.workspace / "example.py").write_text("def find_answer():\n    return 42\n")
        session = self.session()
        session.append("model_response", 1, {"message": "search find_answer"})
        session.append("tool_result", 1, {"name": "read_file", "status": "ok", "output": "42"})
        self.assertIn("find_answer", RepositoryContext(RepositoryIndex(self.workspace)).build(session.events, "find_answer", None).messages[1]["content"])
        self.assertIn("find_answer", PythonSymbolContext(self.workspace).build(session.events, "find_answer", None).messages[1]["content"])
        self.assertEqual(SummaryContext(recent_events=1).build(session.events, "task", None).metadata["summarized_range"], [1, 1])
        other = self.session()
        other.append("model_response", 1, {"message": "different"})
        session.append("terminal", 1, {"status": "finished"})
        other.append("terminal", 1, {"status": "finished"})
        difference = first_difference(session.root / "trace.jsonl", other.root / "trace.jsonl")
        self.assertEqual(difference["left"]["type"], "model_response")
        self.assertIsNone(first_difference(session.root / "trace.jsonl", session.root / "trace.jsonl"))

    def test_context_selection_records_window_and_budget_drops(self) -> None:
        session = self.session()
        first = session.append("model_response", 1, {"message": "a" * 30})
        second = session.append("model_response", 2, {"message": "b" * 30})
        window = SlidingWindow(1).build(session.events, "task", None)
        self.assertEqual(window.metadata["window_dropped_range"], [first.seq, first.seq])
        self.assertEqual(window.event_ids, (second.seq,))
        budgeted = FullHistory().build(session.events, "task", 10)
        self.assertIn(first.seq, budgeted.metadata["selection"]["dropped_event_ids"])
        self.assertEqual(budgeted.metadata["selection"]["reason"], "token_budget")

    def test_retrieval_indexes_refresh_after_agent_write(self) -> None:
        for name, make_context in (
            ("repository", lambda: RepositoryContext(RepositoryIndex(self.workspace))),
            ("retrieval_summary", lambda: RetrievalSummaryContext(RepositoryIndex(self.workspace))),
            ("python_symbols", lambda: PythonSymbolContext(self.workspace)),
        ):
            with self.subTest(strategy=name):
                source = self.workspace / "example.py"
                source.write_text("def old_symbol():\n    return 1\n")
                session = self.session()
                model = ScriptedModel([
                    {"tool_calls": [{"name": "write_file", "arguments": {
                        "path": "example.py", "content": "def fresh_symbol():\n    return 2\n",
                    }}]},
                    {"message": "done", "finish": True},
                ])
                result = AgentLoop(model, make_context(), self.runtime(session, True), session).run("find fresh_symbol")
                self.assertEqual(result.status, TerminalStatus.FINISHED)
                views = [event.payload for event in session.events if event.type == "context_view"]
                self.assertEqual(len(views), 2)
                self.assertNotEqual(views[0]["metadata"]["snapshot_id"], views[1]["metadata"]["snapshot_id"])
                self.assertIn("fresh_symbol", str(views[1]["messages"][1:]))
                refresh = views[1]["metadata"]["index_refresh"]
                self.assertEqual(refresh["reason"], "tool_result")
                self.assertTrue(refresh["changed"])
                self.assertEqual(refresh["previous_snapshot_id"], views[0]["metadata"]["snapshot_id"])
                tool_result = next(event for event in session.events if event.type == "tool_result")
                self.assertEqual(refresh["event_ids"], [tool_result.seq])

    def test_index_ignores_read_tools_and_refreshes_after_failed_command(self) -> None:
        source = self.workspace / "example.py"
        source.write_text("def old_symbol():\n    return 1\n")
        session = self.session()
        session.append("run_started", 0, {"tools": [
            {"name": "read_file", "side_effect": "read"},
            {"name": "mcp.reader.lookup", "side_effect": "read"},
            {"name": "shell", "side_effect": "write"},
        ]})
        context = RepositoryContext(RepositoryIndex(self.workspace))
        first = context.build(session.events, "fresh_symbol", None)
        session.append("tool_result", 1, {"name": "read_file", "status": "ok"})
        session.append("tool_result", 1, {"name": "mcp.reader.lookup", "status": "ok"})
        read_view = context.build(session.events, "fresh_symbol", None)
        self.assertNotIn("index_refresh", read_view.metadata)
        self.assertEqual(read_view.metadata["snapshot_id"], first.metadata["snapshot_id"])

        source.write_text("def fresh_symbol():\n    return 2\n")
        failed = session.append("tool_result", 1, {
            "name": "shell", "status": "error", "error_code": "nonzero_exit",
        })
        refreshed = context.build(session.events, "fresh_symbol", None)
        self.assertNotEqual(refreshed.metadata["snapshot_id"], first.metadata["snapshot_id"])
        self.assertEqual(refreshed.metadata["index_refresh"]["event_ids"], [failed.seq])

    def test_indexes_detect_external_edits_without_tool_events(self) -> None:
        source = self.workspace / "example.py"
        source.write_text("def old_symbol():\n    return 1\n")
        contexts = (
            RepositoryContext(RepositoryIndex(self.workspace)),
            RetrievalSummaryContext(RepositoryIndex(self.workspace)),
            PythonSymbolContext(self.workspace),
        )
        first = [context.build((), "fresh_symbol", None) for context in contexts]

        source.write_text("def fresh_symbol():\n    return 2\n")
        second = [context.build((), "fresh_symbol", None) for context in contexts]
        for before, after in zip(first, second):
            self.assertNotEqual(before.metadata["snapshot_id"], after.metadata["snapshot_id"])
            self.assertEqual(after.metadata["index_refresh"]["reason"], "workspace_changed")
            self.assertEqual(after.metadata["index_refresh"]["event_ids"], [])
            self.assertIn("fresh_symbol", str(after.messages[1:]))

        source.unlink()
        third = [context.build((), "fresh_symbol", None) for context in contexts]
        for before, after in zip(second, third):
            self.assertNotEqual(before.metadata["snapshot_id"], after.metadata["snapshot_id"])
            self.assertEqual(after.metadata["hits"], [])

    def test_hidden_and_linked_directories_are_not_indexed(self) -> None:
        hidden = self.workspace / ".private"
        hidden.mkdir()
        (hidden / "secret.py").write_text("def hidden_symbol():\n    pass\n")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "other.py").write_text("def linked_symbol():\n    pass\n")
        (self.workspace / "linked").symlink_to(outside, target_is_directory=True)
        cache = self.workspace / "__pycache__"
        cache.mkdir()
        (cache / "cached.py").write_text("def cached_symbol():\n    pass\n")
        repo = RepositoryContext(RepositoryIndex(self.workspace))
        symbols = PythonSymbolContext(self.workspace)
        for context in (repo, symbols):
            view = context.build((), "hidden_symbol linked_symbol cached_symbol", None)
            self.assertEqual(view.metadata["hits"], [])


if __name__ == "__main__":
    unittest.main()
