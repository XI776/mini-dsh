"""Deterministic model adapter for offline runs and harness tests."""

from __future__ import annotations

import json
import os
import subprocess
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContextView, ModelFailure, ModelResult, ToolCall, Usage
from .process import run_process


def _parse_response(response: Mapping[str, Any]) -> ModelResult:
    raw_usage = response.get("usage")
    try:
        usage = Usage(**raw_usage) if isinstance(raw_usage, Mapping) else Usage(known=False)
    except (TypeError, ValueError) as exc:
        raise ModelFailure(f"invalid usage in model response: {exc}", Usage(known=False)) from exc
    try:
        if "error" in response:
            raise ModelFailure(str(response["error"]), usage)
        calls = tuple(ToolCall(**item) for item in response.get("tool_calls", []))
        return ModelResult(str(response.get("message", "")), calls, bool(response.get("finish", False)), usage)
    except (TypeError, ValueError, KeyError) as exc:
        raise ModelFailure(f"invalid model response: {exc}", usage) from exc


class ScriptedModel:
    def __init__(self, responses: Sequence[Mapping[str, Any]]):
        self.responses = list(responses)
        self.index = 0
        digest = hashlib.sha256(json.dumps(self.responses, sort_keys=True).encode()).hexdigest()
        self.identity = f"script:{digest}"

    def complete(self, view: ContextView, tool_schemas: Sequence[Mapping[str, Any]], timeout_s: float) -> ModelResult:
        if self.index >= len(self.responses):
            raise ModelFailure("script has no response for this step", Usage(known=False))
        response = self.responses[self.index]
        self.index += 1
        return _parse_response(response)

    def resume_from(self, completed_calls: int) -> None:
        if completed_calls < 0 or completed_calls > len(self.responses):
            raise ValueError("script cannot resume from this model call")
        self.index = completed_calls


class CommandModel:
    """Call a trusted external model bridge using one JSON request/response per process."""

    def __init__(
        self,
        argv: Sequence[str],
        cwd: Path,
        pass_environment: Sequence[str] = (),
        max_response_bytes: int = 2_000_000,
        seed: int | None = None,
    ):
        if not argv or max_response_bytes < 1:
            raise ValueError("model command and response limit are required")
        self.argv = tuple(argv)
        self.cwd = Path(cwd).resolve(strict=True)
        self.pass_environment = tuple(pass_environment)
        self.max_response_bytes = max_response_bytes
        self.seed = seed
        digest = hashlib.sha256(json.dumps({"argv": self.argv, "env_names": self.pass_environment, "seed": seed}, sort_keys=True).encode()).hexdigest()
        self.identity = f"command:{digest}"

    def complete(self, view: ContextView, tool_schemas: Sequence[Mapping[str, Any]], timeout_s: float) -> ModelResult:
        request = {
            "protocol_version": 1,
            "messages": list(view.messages),
            "tools": list(tool_schemas),
            "timeout_seconds": timeout_s,
        }
        if self.seed is not None:
            request["seed"] = self.seed
        environment = {
            name: os.environ[name]
            for name in ("PATH", "LANG", "LC_ALL", "HOME", *self.pass_environment)
            if name in os.environ
        }
        try:
            capture = run_process(
                self.argv, self.cwd, timeout_s, self.max_response_bytes,
                environment=environment,
                input_bytes=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                merge_stderr=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelFailure("model command timed out", Usage(known=False)) from exc
        except (OSError, ValueError) as exc:
            raise ModelFailure(f"model command failed: {exc}", Usage(known=False)) from exc
        if capture.truncated:
            raise ModelFailure("model response exceeded configured limit", Usage(known=False))
        try:
            response = json.loads(capture.output)
        except json.JSONDecodeError as exc:
            raise ModelFailure("model command returned invalid JSON", Usage(known=False)) from exc
        if not isinstance(response, dict):
            raise ModelFailure("model response must be a JSON object", Usage(known=False))
        result = _parse_response(response)
        if capture.exit_code != 0:
            raise ModelFailure(f"model command exited with {capture.exit_code}", result.usage)
        return result
