from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.agent import BudgetLimits
from agent_harness.context import FullHistory
from agent_harness.contracts import ContextView, ModelFailure, TerminalStatus, ToolCall
from agent_harness.docker_runtime import DockerRunner, ReadOnlyMount
from agent_harness.eval import EvaluationCase, evaluate
from agent_harness.model import CommandModel, ScriptedModel
from agent_harness.process import ProcessCapture
from agent_harness.runtime import ShellTool, ToolRuntime, WorkspacePolicy
from agent_harness.session import JsonlSession, read_trace
from agent_harness.trace_verify import verify_trace


class VersionTwoTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_command_model_json_protocol(self) -> None:
        bridge = self.root / "bridge.py"
        bridge.write_text(
            "import json,sys\n"
            "request=json.load(sys.stdin)\n"
            "assert request['protocol_version'] == 1\n"
            "print('bridge diagnostic', file=sys.stderr)\n"
            "print(json.dumps({'message':request['messages'][0]['content'], 'finish':True, "
            "'usage':{'input_tokens':4,'output_tokens':2}}))\n"
        )
        model = CommandModel([sys.executable, str(bridge)], self.root)
        view = ContextView(({"role": "user", "content": "hello"},), (), "full_history")
        result = model.complete(view, [], 2)
        self.assertEqual(result.message, "hello")
        self.assertEqual(result.usage.total_tokens, 6)

    def test_command_model_preserves_usage_on_failed_response(self) -> None:
        bridge = self.root / "failed_bridge.py"
        bridge.write_text(
            "import json,sys\n"
            "json.load(sys.stdin)\n"
            "print(json.dumps({'error':'provider failed','usage':{'input_tokens':7,'output_tokens':1}}))\n"
            "raise SystemExit(1)\n"
        )
        model = CommandModel([sys.executable, str(bridge)], self.root)
        view = ContextView(({"role": "user", "content": "hello"},), (), "full_history")
        with self.assertRaises(ModelFailure) as failure:
            model.complete(view, [], 2)
        self.assertEqual(failure.exception.usage.total_tokens, 8)

    def test_large_shell_output_is_streamed_to_artifact(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        with JsonlSession(self.root / "runs") as session:
            runtime = ToolRuntime([ShellTool(workspace, {"python"}, session, 32)], WorkspacePolicy(True), session, 32)
            result = runtime.execute(ToolCall("shell", {"argv": ["python", "-c", "print('x'*50000)"]}), 5)
            self.assertEqual(result.status, "ok")
            self.assertTrue(result.truncated)
            self.assertEqual(len(result.output), 32)
            self.assertEqual((session.root / "artifacts" / result.artifact_id).read_text(), "x" * 50000 + "\n")

    def test_docker_command_uses_isolation_flags_and_extra_mount(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        holdout = self.root / "holdout"
        holdout.mkdir()
        seen: list[str] = []

        def fake_process(argv, cwd, timeout_s, output_limit, **kwargs):
            seen.extend(argv)
            return ProcessCapture(0, "ok", 2, None, False)

        with JsonlSession(self.root / "runs") as session:
            with patch("agent_harness.docker_runtime.run_process", side_effect=fake_process):
                capture = DockerRunner("local-image:1", session).run(
                    ["python", "--version"], workspace, 5,
                    [ReadOnlyMount(holdout, "/holdout")],
                )
        self.assertEqual(capture.exit_code, 0)
        self.assertIn("none", seen)
        self.assertIn("--read-only", seen)
        self.assertIn("--cap-drop", seen)
        self.assertTrue(any("dst=/holdout,readonly" in arg for arg in seen))

    def test_evaluation_uses_tests_not_agent_claim_and_mounts_holdout_late(self) -> None:
        baseline = self.root / "baseline"
        baseline.mkdir()
        (baseline / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
        case = EvaluationCase(
            "example", "fix add", baseline, "local-image:1", ("test",),
            holdout_test=("hidden",), holdout_files={"test_hidden.py": "assert True"},
            limits=BudgetLimits(steps=3, tokens=1000, tool_calls=3, wall_seconds=10),
        )
        runs: list[tuple[str, bool]] = []

        class FakeDockerRunner:
            def __init__(self, image, session):
                self.session = session

            def run(self, argv, workspace, timeout_s, extra_mounts=()):
                mounted = bool(extra_mounts)
                runs.append((argv[0], mounted))
                if mounted:
                    self_test.assertTrue(any(event.type == "terminal" for event in self.session.events))
                    self_test.assertTrue((extra_mounts[0].source / "test_hidden.py").exists())
                else:
                    self_test.assertFalse((workspace.parent / "holdout").exists())
                fixed = "return a + b" in (workspace / "calculator.py").read_text()
                return ProcessCapture(0 if fixed else 1, "result", 6, None, False)

        self_test = self
        with patch("agent_harness.eval.DockerRunner", FakeDockerRunner):
            failed = evaluate(case, ScriptedModel([{"finish": True, "message": "done"}]), lambda _: FullHistory(), self.root / "runs")
            self.assertEqual(failed.run.status, TerminalStatus.FINISHED)
            self.assertFalse(failed.success)
            self.assertEqual(failed.public_exit_code, 1)
            self.assertEqual(failed.holdout_exit_code, 1)

            fixed = evaluate(
                case,
                ScriptedModel([
                    {"tool_calls": [{"name": "write_file", "arguments": {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"}}]},
                    {"finish": True, "message": "done"},
                ]),
                lambda _: FullHistory(), self.root / "runs",
            )
        self.assertTrue(fixed.success)
        self.assertEqual(runs, [("test", False), ("hidden", True), ("test", False), ("hidden", True)])
        events = read_trace(fixed.trace)
        setup = next(event for event in events if event["type"] == "evaluation_setup")
        self.assertEqual(setup["payload"]["holdout_test"], ["hidden"])
        started = next(event for event in events if event["type"] == "run_started")
        test_tool = next(tool for tool in started["payload"]["tools"] if tool["name"] == "run_tests")
        self.assertEqual(test_tool["configured_argv"], ["test"])
        self.assertLess(
            next(i for i, event in enumerate(events) if event["type"] == "terminal"),
            next(i for i, event in enumerate(events) if event["type"] == "holdout_test_result"),
        )

    def test_evaluation_rejects_holdout_content_in_baseline(self) -> None:
        baseline = self.root / "baseline"
        baseline.mkdir()
        (baseline / "copied_test.py").write_text("assert 2 + 2 == 4\n")
        case = EvaluationCase(
            "leaked", "fix task", baseline, "local-image:1", ("public",),
            holdout_test=("hidden",), holdout_files={"test_hidden.py": "assert 2 + 2 == 4\n"},
        )
        with self.assertRaisesRegex(ValueError, "holdout test content is present"):
            evaluate(case, ScriptedModel([]), lambda _: FullHistory(), self.root / "runs")

    def test_trace_verifier_requires_configured_holdout_result(self) -> None:
        baseline = self.root / "baseline"
        baseline.mkdir()
        (baseline / "source.py").write_text("answer = 42\n")
        case = EvaluationCase(
            "missing-holdout", "check", baseline, "local-image:1", ("public",),
            holdout_test=("hidden",), holdout_files={"test_hidden.py": "assert True\n"},
        )

        class FakeDockerRunner:
            def __init__(self, image, session):
                pass

            def run(self, argv, workspace, timeout_s, extra_mounts=()):
                return ProcessCapture(0, "ok", 2, None, False)

        with patch("agent_harness.eval.DockerRunner", FakeDockerRunner):
            result = evaluate(case, ScriptedModel([{"finish": True}]), lambda _: FullHistory(), self.root / "runs")
        events = [event for event in read_trace(result.trace) if event["type"] != "holdout_test_result"]
        for seq, event in enumerate(events, 1):
            event["seq"] = seq
        result.trace.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertIn("holdout_result_count", {error["code"] for error in verify_trace(result.trace)["errors"]})


if __name__ == "__main__":
    unittest.main()
