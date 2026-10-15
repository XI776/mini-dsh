"""Pinned MCP 2026-07-28 stdio client for text tools.

The transport intentionally supports one in-flight request at a time. It does
not implement MCP tasks, input_required, subscriptions, or older handshakes.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ToolResult


PROTOCOL_VERSION = "2026-07-28"


class McpProtocolError(Exception):
    pass


class McpRemoteError(McpProtocolError):
    """A well-formed JSON-RPC error; the transport remains usable."""


class McpStdioClient:
    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        cwd: Path,
        pass_environment: Sequence[str] = (),
        max_frame_bytes: int = 4 * 1024 * 1024,
    ):
        if not name or not argv or max_frame_bytes < 1024:
            raise ValueError("MCP name, command and frame limit are required")
        self.name = name
        self.argv = tuple(argv)
        self.cwd = Path(cwd).resolve(strict=True)
        self.pass_environment = tuple(pass_environment)
        self.max_frame_bytes = max_frame_bytes
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr = None
        self._buffer = bytearray()
        self._request_id = 0
        self._lock = threading.Lock()
        self._tool_definitions: list[Mapping[str, Any]] = []

    def start(self, timeout_s: float = 10.0) -> None:
        if self._process is not None:
            raise RuntimeError("MCP client already started")
        environment = {
            name: os.environ[name]
            for name in ("PATH", "LANG", "LC_ALL", "HOME", *self.pass_environment)
            if name in os.environ
        }
        self._stderr = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(
                self.argv, cwd=self.cwd, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                start_new_session=True,
            )
            discovery = self.request("server/discover", {}, timeout_s)
            if PROTOCOL_VERSION not in discovery.get("supportedVersions", []):
                raise McpProtocolError(f"server does not support MCP {PROTOCOL_VERSION}")
            cursor = None
            seen_cursors: set[str] = set()
            definitions: list[Mapping[str, Any]] = []
            for _ in range(100):
                params = {"cursor": cursor} if cursor is not None else {}
                page = self.request("tools/list", params, timeout_s)
                tools = page.get("tools")
                if not isinstance(tools, list):
                    raise McpProtocolError("tools/list response has no tools array")
                definitions.extend(tools)
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in seen_cursors:
                    raise McpProtocolError("invalid or repeating tools/list cursor")
                seen_cursors.add(cursor)
            else:
                raise McpProtocolError("tools/list exceeded 100 pages")
            names = [tool.get("name") for tool in definitions if isinstance(tool, Mapping)]
            if len(names) != len(definitions) or len(set(names)) != len(names) or not all(isinstance(name, str) and name for name in names):
                raise McpProtocolError("invalid or duplicate MCP tool name")
            self._tool_definitions = definitions
        except BaseException:
            self.close()
            raise

    @property
    def tool_definitions(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._tool_definitions)

    def request(self, method: str, params: Mapping[str, Any], timeout_s: float) -> Mapping[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise McpProtocolError("MCP server is not running")
            if timeout_s <= 0:
                raise TimeoutError("MCP request deadline expired")
            self._request_id += 1
            request_id = self._request_id
            metadata = {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": "mini-dsh", "version": "0.1.0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
            request = {
                "jsonrpc": "2.0", "id": request_id, "method": method,
                "params": {**params, "_meta": metadata},
            }
            try:
                process.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                process.stdin.flush()
            except OSError as exc:
                self.close()
                raise McpProtocolError(f"MCP server input closed: {exc}") from exc
            deadline = time.monotonic() + timeout_s
            try:
                while True:
                    frame = self._read_frame(deadline)
                    try:
                        response = json.loads(frame)
                    except json.JSONDecodeError as exc:
                        raise McpProtocolError("invalid MCP JSON frame") from exc
                    if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                        raise McpProtocolError("invalid MCP JSON-RPC envelope")
                    if response.get("id") is None:
                        continue  # Notification; no subscription was requested.
                    if response.get("id") != request_id:
                        raise McpProtocolError("unexpected MCP response id")
                    if "error" in response:
                        error = response["error"]
                        raise McpRemoteError(f"MCP {method} error: {error}")
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise McpProtocolError("MCP response has no result object")
                    return result
            except McpRemoteError:
                raise
            except (TimeoutError, McpProtocolError):
                self.close()
                raise

    def _read_frame(self, deadline: float) -> bytes:
        process = self._process
        assert process is not None and process.stdout is not None
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                frame = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return frame
            if len(self._buffer) >= self.max_frame_bytes:
                raise McpProtocolError("MCP response frame exceeds size limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP response timed out")
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("MCP response timed out")
            chunk = os.read(process.stdout.fileno(), min(65536, self.max_frame_bytes - len(self._buffer)))
            if not chunk:
                raise McpProtocolError("MCP server closed stdout")
            self._buffer.extend(chunk)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def __enter__(self) -> McpStdioClient:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class McpRemoteTool:
    is_external = True
    def __init__(self, client: McpStdioClient, definition: Mapping[str, Any], side_effect: str = "write"):
        self.client = client
        self.remote_name = str(definition["name"])
        self.name = f"mcp.{client.name}.{self.remote_name}"
        self.description = str(definition.get("description", ""))
        schema = definition.get("inputSchema", {"type": "object"})
        if not isinstance(schema, Mapping):
            raise McpProtocolError(f"invalid inputSchema for {self.remote_name}")
        self.schema = {**schema, "additionalProperties": schema.get("additionalProperties", True)}
        self.side_effect = side_effect

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> ToolResult:
        try:
            result = self.client.request(
                "tools/call", {"name": self.remote_name, "arguments": dict(arguments)}, timeout_s
            )
        except TimeoutError:
            return ToolResult("", "error", "MCP tool timed out", "timeout")
        except McpProtocolError as exc:
            return ToolResult("", "error", str(exc), "mcp_protocol_error")
        if result.get("resultType", "complete") != "complete":
            return ToolResult("", "error", "MCP tool requires unsupported interaction", "mcp_input_required")
        content = result.get("content", [])
        if not isinstance(content, list):
            return ToolResult("", "error", "invalid MCP content", "mcp_protocol_error")
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                return ToolResult("", "error", "MCP binary/resource content is not supported", "unsupported_content")
            text_parts.append(str(item.get("text", "")))
        if "structuredContent" in result:
            text_parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        output = "\n".join(text_parts)
        is_error = bool(result.get("isError", False))
        return ToolResult("", "error" if is_error else "ok", output, "mcp_tool_error" if is_error else None)
