"""Stable contracts shared by the agent, context strategies, and tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from io import BufferedIOBase
from typing import Any, Mapping, Protocol, Sequence


class TerminalStatus(StrEnum):
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    known: bool = True
    cache_usage_known: bool = False
    cost_source: str = "reported"
    raw_usage: Mapping[str, Any] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens) < 0:
            raise ValueError("token counts must be nonnegative")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost must be nonnegative")
        if self.cost_source not in {"reported", "estimated", "synthetic"}:
            raise ValueError("cost_source must be reported, estimated, or synthetic")


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    call_id: str = ""


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    status: str
    output: str = ""
    error_code: str | None = None
    exit_code: int | None = None
    artifact_id: str | None = None
    truncated: bool = False
    duration_ms: int = 0


@dataclass(frozen=True)
class ModelResult:
    message: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish: bool = False
    usage: Usage = field(default_factory=Usage)
    attempts: int = 1

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")


class ModelFailure(Exception):
    """A failed model request with any usage reported by the provider."""

    def __init__(self, message: str, usage: Usage | None = None, attempts: int = 1):
        super().__init__(message)
        self.usage = usage or Usage(known=False)
        self.attempts = max(1, attempts)


@dataclass(frozen=True)
class ContextView:
    messages: tuple[Mapping[str, str], ...]
    event_ids: tuple[int, ...]
    strategy: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    run_id: str
    seq: int
    type: str
    step: int
    payload: Mapping[str, Any]
    timestamp: str
    monotonic_ms: int
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Model(Protocol):
    def complete(
        self, view: ContextView, tool_schemas: Sequence[Mapping[str, Any]], timeout_s: float
    ) -> ModelResult: ...


class ContextStrategy(Protocol):
    name: str

    def build(self, events: Sequence[Event], task: str, token_budget: int | None) -> ContextView: ...


class PermissionPolicy(Protocol):
    def check(self, tool_name: str, arguments: Mapping[str, Any], side_effect: str) -> bool: ...


class Tool(Protocol):
    name: str
    schema: Mapping[str, Any]
    side_effect: str

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> str | ToolResult: ...


class SessionStore(Protocol):
    run_id: str

    @property
    def events(self) -> tuple[Event, ...]: ...

    def append(self, event_type: str, step: int, payload: Mapping[str, Any]) -> Event: ...

    def save_artifact(self, content: bytes) -> str: ...

    def save_artifact_stream(self, stream: BufferedIOBase) -> str: ...


class ToolExecutor(Protocol):
    permission: PermissionPolicy
    tools: Mapping[str, Tool]

    def schemas(self) -> list[Mapping[str, Any]]: ...

    def execute(self, call: ToolCall, timeout_s: float) -> ToolResult: ...


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...
