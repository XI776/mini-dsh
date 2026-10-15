"""Schema, permission, timeout and bounded-result handling for all tools."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import PermissionPolicy, SessionStore, Tool, ToolCall, ToolResult
from .process import run_process


class WorkspacePolicy:
    def __init__(self, allow_writes: bool = False, allowed_tools: set[str] | None = None):
        self.allow_writes = allow_writes
        self.allowed_tools = allowed_tools

    def check(self, tool_name: str, arguments: Mapping[str, Any], side_effect: str) -> bool:
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            return False
        return side_effect == "read" or self.allow_writes

    def configuration(self) -> dict[str, Any]:
        return {
            "allow_writes": self.allow_writes,
            "allowed_tools": sorted(self.allowed_tools) if self.allowed_tools is not None else None,
        }


def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    if not isinstance(arguments, Mapping):
        raise ValueError("tool arguments must be an object")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("invalid tool schema")
    missing = set(schema.get("required", [])) - set(arguments)
    if missing:
        raise ValueError(f"missing required arguments: {', '.join(sorted(missing))}")
    if schema.get("additionalProperties", False) is False:
        extra = set(arguments) - set(properties)
        if extra:
            raise ValueError(f"unknown arguments: {', '.join(sorted(extra))}")
    kinds = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}
    for key, value in arguments.items():
        spec = properties.get(key, {})
        kind = spec.get("type")
        if kind in kinds:
            valid = isinstance(value, kinds[kind])
            if kind == "integer":
                valid = valid and not isinstance(value, bool)
            if not valid:
                raise ValueError(f"invalid type for {key}: expected {kind}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"invalid value for {key}")
        if kind == "array" and "items" in spec:
            item_kind = spec["items"].get("type")
            if item_kind in kinds and not all(isinstance(item, kinds[item_kind]) for item in value):
                raise ValueError(f"invalid array item for {key}")


class ToolRuntime:
    def __init__(
        self,
        tools: Sequence[Tool],
        permission: PermissionPolicy,
        session: SessionStore,
        max_output_bytes: int = 8192,
    ):
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise ValueError("duplicate tool name")
        if max_output_bytes < 0:
            raise ValueError("max_output_bytes must be nonnegative")
        self.permission = permission
        self.session = session
        self.max_output_bytes = max_output_bytes

    def schemas(self) -> list[Mapping[str, Any]]:
        schemas = []
        for tool in self.tools.values():
            schema = {"name": tool.name, "parameters": tool.schema, "side_effect": tool.side_effect}
            if description := getattr(tool, "description", None):
                schema["description"] = description
            if argv := getattr(tool, "argv", None):
                schema["configured_argv"] = list(argv)
            if allowed := getattr(tool, "allowed_commands", None):
                schema["allowed_commands"] = sorted(allowed)
            schemas.append(schema)
        return schemas

    def execute(self, call: ToolCall, timeout_s: float) -> ToolResult:
        started = time.monotonic()
        call_id = call.call_id or uuid.uuid4().hex

        def error(code: str, message: str) -> ToolResult:
            return ToolResult(call_id, "error", message, code, duration_ms=int((time.monotonic() - started) * 1000))

        tool = self.tools.get(call.name)
        if tool is None:
            return error("unknown_tool", f"unknown tool: {call.name}")
        try:
            validate_arguments(tool.schema, call.arguments)
        except ValueError as exc:
            return error("invalid_arguments", str(exc))
        if not self.permission.check(tool.name, call.arguments, tool.side_effect):
            return error("permission_denied", f"permission denied: {tool.name}")
        if timeout_s <= 0:
            return error("timeout", "tool deadline has expired")
        try:
            raw = tool.execute(call.arguments, timeout_s)
        except subprocess.TimeoutExpired:
            return error("timeout", "tool timed out")
        except (OSError, ValueError) as exc:
            return error("execution_error", str(exc))
        except Exception as exc:
            return error("execution_error", f"{type(exc).__name__}: {exc}")
        result = raw if isinstance(raw, ToolResult) else ToolResult(call_id, "ok", str(raw))
        output_bytes = result.output.encode("utf-8", errors="replace")
        artifact_id = result.artifact_id
        truncated = result.truncated
        output = result.output
        if len(output_bytes) > self.max_output_bytes:
            artifact_id = self.session.save_artifact(output_bytes)
            output = output_bytes[: self.max_output_bytes].decode("utf-8", errors="replace")
            truncated = True
        return ToolResult(
            call_id=call_id,
            status=result.status,
            output=output,
            error_code=result.error_code,
            exit_code=result.exit_code,
            artifact_id=artifact_id,
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class WorkspaceFiles:
    """Open workspace files through directory FDs, refusing symlinks and credentials."""

    def __init__(self, root: Path, protected_paths: Sequence[Path] = (), max_file_bytes: int = 1_000_000):
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        self.root = Path(root).resolve(strict=True)
        self.protected_paths = frozenset(Path(path).resolve() for path in protected_paths)
        self.max_file_bytes = max_file_bytes

    def open(self, relative: str, flags: int, mode: int = 0o600) -> int:
        path = Path(relative)
        if path.is_absolute() or not path.parts or any(part in ("..", ".") for part in path.parts):
            raise ValueError("path must stay inside workspace")
        if any(
            part == ".env" or (part.startswith(".env.") and part not in (".env.example", ".env.sample", ".env.template"))
            for part in path.parts
        ) or self.root.joinpath(*path.parts) in self.protected_paths:
            raise ValueError("credential file is not accessible to agent tools")
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            return os.open(path.parts[-1], flags | os.O_NOFOLLOW, mode, dir_fd=directory)
        finally:
            os.close(directory)

    def read_text(self, relative: str) -> str:
        fd = self.open(relative, os.O_RDONLY)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes:
            raise ValueError(f"file exceeds {self.max_file_bytes} byte read limit")
        return data.decode("utf-8")

    def write_text(self, relative: str, content: str) -> str:
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ValueError(f"content exceeds {self.max_file_bytes} byte write limit")
        fd = self.open(relative, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        return f"wrote {relative} ({len(encoded)} bytes)"


class ReadFileTool:
    name = "read_file"
    side_effect = "read"
    description = "Read a UTF-8 file. The path must be relative to the workspace root; absolute paths such as /workspace/file.py are invalid."
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def __init__(self, files: WorkspaceFiles):
        self.files = files

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> str:
        return self.files.read_text(arguments["path"])


class WriteFileTool:
    name = "write_file"
    side_effect = "write"
    description = "Create or replace a UTF-8 file. Use a path relative to the workspace root."
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def __init__(self, files: WorkspaceFiles):
        self.files = files

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> str:
        return self.files.write_text(arguments["path"], arguments["content"])


class PatchFileTool:
    name = "patch_file"
    side_effect = "write"
    description = "Replace exactly one occurrence of old text in a workspace file. Use a relative path."
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    }

    def __init__(self, files: WorkspaceFiles):
        self.files = files

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> str:
        old = arguments["old"]
        if not old:
            raise ValueError("old text must not be empty")
        content = self.files.read_text(arguments["path"])
        if content.count(old) != 1:
            raise ValueError("old text must occur exactly once")
        return self.files.write_text(arguments["path"], content.replace(old, arguments["new"], 1))


class ShellTool:
    name = "shell"
    is_external = True
    side_effect = "write"
    description = "Run an allowed command with an argv array in the workspace. Shell syntax is not interpreted."
    schema = {
        "type": "object",
        "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
        "required": ["argv"],
    }

    def __init__(self, workspace: Path, allowed_commands: set[str], session: SessionStore, output_limit: int = 8192):
        self.workspace = Path(workspace).resolve(strict=True)
        self.allowed_commands = allowed_commands
        self.session = session
        self.output_limit = output_limit

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> ToolResult:
        argv = arguments["argv"]
        if not argv or argv[0] not in self.allowed_commands or "/" in argv[0]:
            raise ValueError("command is not allowed")
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        capture = run_process(
            argv, self.workspace, timeout_s, self.output_limit, session=self.session, environment=environment
        )
        return ToolResult(
            call_id="",
            status="ok" if capture.exit_code == 0 else "error",
            output=capture.output,
            error_code=None if capture.exit_code == 0 else "nonzero_exit",
            exit_code=capture.exit_code,
            artifact_id=capture.artifact_id,
            truncated=capture.truncated,
        )


class GitTool:
    name = "git"
    side_effect = "read"
    description = "Read Git status, diff, or recent log for the workspace repository."
    schema = {
        "type": "object",
        "properties": {"operation": {"type": "string", "enum": ["status", "diff", "log"]}},
        "required": ["operation"],
    }

    def __init__(self, workspace: Path, session: SessionStore, output_limit: int = 8192):
        self.workspace = Path(workspace).resolve(strict=True)
        self.session = session
        self.output_limit = output_limit

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> ToolResult:
        suffix = {
            "status": ["status", "--short"],
            "diff": ["diff", "--no-ext-diff"],
            "log": ["log", "-5", "--oneline"],
        }[arguments["operation"]]
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "HOME") if key in os.environ}
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        capture = run_process(
            ["git", "-C", str(self.workspace), *suffix], self.workspace, timeout_s, self.output_limit,
            session=self.session, environment=environment,
        )
        return ToolResult(
            "", "ok" if capture.exit_code == 0 else "error", capture.output,
            None if capture.exit_code == 0 else "nonzero_exit", capture.exit_code,
            capture.artifact_id, capture.truncated,
        )


class ConfiguredCommandTool:
    """A named test/build command; the model cannot alter its argv."""

    is_external = True
    side_effect = "write"
    description = "Run the preconfigured command in the workspace; no arguments are needed."
    schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, argv: Sequence[str], workspace: Path, session: SessionStore, output_limit: int = 8192):
        if not name or not argv:
            raise ValueError("command name and argv are required")
        self.name = name
        self.argv = tuple(argv)
        self.workspace = Path(workspace).resolve(strict=True)
        self.session = session
        self.output_limit = output_limit

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> ToolResult:
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "HOME") if key in os.environ}
        capture = run_process(self.argv, self.workspace, timeout_s, self.output_limit, session=self.session, environment=environment)
        return ToolResult(
            "", "ok" if capture.exit_code == 0 else "error", capture.output,
            None if capture.exit_code == 0 else "nonzero_exit", capture.exit_code,
            capture.artifact_id, capture.truncated,
        )


class McpToolAdapter:
    """Wrap an injected MCP callable; transport deadlines belong to that callable."""

    def __init__(self, name: str, schema: Mapping[str, Any], side_effect: str, invoke):
        self.name = name
        self.schema = schema
        self.side_effect = side_effect
        self.invoke = invoke

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> str | ToolResult:
        return self.invoke(arguments, timeout_s)
