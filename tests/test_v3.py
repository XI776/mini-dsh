from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from agent_harness.contracts import ToolCall
from agent_harness.experiment import summarize
from agent_harness.mcp_stdio import McpRemoteError, McpRemoteTool, McpStdioClient
from agent_harness.runtime import ToolRuntime, WorkspacePolicy
from agent_harness.session import JsonlSession


class VersionThreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_mcp_tool_uses_runtime_permission_and_budget_surface(self) -> None:
        server = Path(__file__).resolve().parents[1] / "examples" / "mcp_echo_server.py"
        with McpStdioClient("test", [sys.executable, str(server)], self.root) as client:
            self.assertEqual([tool["name"] for tool in client.tool_definitions], ["echo"])
            with JsonlSession(self.root / "runs") as session:
                tool = McpRemoteTool(client, client.tool_definitions[0], side_effect="write")
                runtime = ToolRuntime([tool], WorkspacePolicy(), session)
                denied = runtime.execute(ToolCall("mcp.test.echo", {"text": "hello"}), 2)
                self.assertEqual(denied.error_code, "permission_denied")
                permitted = ToolRuntime([tool], WorkspacePolicy(True), session)
                invalid = permitted.execute(ToolCall("mcp.test.echo", {"text": 3}), 2)
                self.assertEqual(invalid.error_code, "invalid_arguments")
                result = permitted.execute(ToolCall("mcp.test.echo", {"text": "hello"}), 2)
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.output, "hello")

    def test_mcp_timeout_closes_server(self) -> None:
        server = self.root / "slow_server.py"
        server.write_text(
            "import json,sys,time\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line); m=r['method']\n"
            " if m=='server/discover': v={'supportedVersions':['2026-07-28']}\n"
            " elif m=='tools/list': v={'tools':[{'name':'slow','inputSchema':{'type':'object','properties':{}}}]}\n"
            " else: time.sleep(2); v={'content':[{'type':'text','text':'done'}]}\n"
            " print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':v}),flush=True)\n"
        )
        client = McpStdioClient("slow", [sys.executable, str(server)], self.root)
        client.start(timeout_s=2)
        tool = McpRemoteTool(client, client.tool_definitions[0], side_effect="read")
        with JsonlSession(self.root / "runs") as session:
            result = ToolRuntime([tool], WorkspacePolicy(), session).execute(ToolCall("mcp.slow.slow", {}), 0.05)
        self.assertEqual(result.error_code, "timeout")
        self.assertIsNone(client._process)

    def test_mcp_remote_error_keeps_transport_available(self) -> None:
        server = self.root / "error_server.py"
        server.write_text(
            "import json,sys\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line); m=r['method']\n"
            " if m=='server/discover': x={'result':{'supportedVersions':['2026-07-28']}}\n"
            " elif m=='tools/list': x={'result':{'tools':[{'name':'echo','inputSchema':{'type':'object','properties':{}}}]}}\n"
            " elif r['params']['arguments'].get('fail'): x={'error':{'code':-32000,'message':'temporary'}}\n"
            " else: x={'result':{'content':[{'type':'text','text':'ok'}]}}\n"
            " print(json.dumps({'jsonrpc':'2.0','id':r['id'],**x}),flush=True)\n"
        )
        with McpStdioClient("error", [sys.executable, str(server)], self.root) as client:
            with self.assertRaises(McpRemoteError):
                client.request("tools/call", {"name": "echo", "arguments": {"fail": True}}, 2)
            result = client.request("tools/call", {"name": "echo", "arguments": {}}, 2)
            self.assertEqual(result["content"][0]["text"], "ok")

    def test_summary_keeps_unpriced_cost_separate_from_token_comparison(self) -> None:
        def row(strategy: str, tokens: int, cost: float, unpriced: int = 0) -> dict:
            return {
                "strategy": strategy, "success": True, "public_pass": True, "holdout_pass": True,
                "input_tokens": tokens - 1, "output_tokens": 1, "total_tokens": tokens,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "agent_wall_ms": 100, "evaluation_wall_ms": 500,
                "agent_status": "finished", "tool_errors": 0,
                "recorded_cost_usd": cost, "unpriced_calls": unpriced,
                "unknown_usage_calls": 0,
            }

        summary = summarize([row("full_history", 100, 1.0), row("summary", 90, 0.0, 1)])
        self.assertAlmostEqual(summary["summary"]["token_change_vs_full_history_pct"], -10)
        self.assertIsNone(summary["summary"]["mean_cost_usd"])
        self.assertIsNone(summary["summary"]["cost_change_vs_full_history_pct"])
        self.assertEqual(summary["summary"]["mean_evaluation_wall_ms"], 500)


if __name__ == "__main__":
    unittest.main()
