"""Budgeted Observe -> Plan -> Act -> Execute -> Observe loop."""

from __future__ import annotations

import time
import threading
import hashlib
import json
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from typing import Callable

from .contracts import ContextStrategy, Event, EventSink, Model, ModelFailure, SessionStore, TerminalStatus, ToolExecutor, Usage


@dataclass(frozen=True)
class BudgetLimits:
    steps: int = 20
    tokens: int = 50_000
    tool_calls: int = 40
    wall_seconds: float = 300.0

    def __post_init__(self) -> None:
        if min(self.steps, self.tokens, self.tool_calls, self.wall_seconds) < 0:
            raise ValueError("budget limits must be nonnegative")


@dataclass
class BudgetUsage:
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    reported_cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    unreported_cost_calls: int = 0
    unestimated_cost_calls: int = 0
    unknown_usage_calls: int = 0
    unpriced_calls: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add_model(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        if usage.cost_usd is not None:
            self.cost_usd += usage.cost_usd
            if usage.cost_source == "reported":
                self.reported_cost_usd += usage.cost_usd
                self.unestimated_cost_calls += 1
            elif usage.cost_source == "estimated":
                self.estimated_cost_usd += usage.cost_usd
                self.unreported_cost_calls += 1
            else:
                self.unreported_cost_calls += 1
                self.unestimated_cost_calls += 1
        else:
            self.unpriced_calls += 1
            self.unreported_cost_calls += 1
            self.unestimated_cost_calls += 1
        if not usage.known:
            self.unknown_usage_calls += 1


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: TerminalStatus
    reason: str
    usage: BudgetUsage
    answer: str = ""


class AgentLoop:
    def __init__(
        self,
        model: Model,
        context: ContextStrategy,
        runtime: ToolExecutor,
        session: SessionStore,
        limits: BudgetLimits | None = None,
        cancel: threading.Event | None = None,
        checkpoint: Callable[[], str] | None = None,
        event_sink: EventSink | None = None,
    ):
        self.model = model
        self.context = context
        self.runtime = runtime
        self.session = session
        self.limits = limits or BudgetLimits()
        self.cancel = cancel or threading.Event()
        self.checkpoint = checkpoint
        self.event_sink = event_sink

    def _append(self, event_type: str, step: int, payload: dict) -> Event:
        event = self.session.append(event_type, step, payload)
        if self.event_sink is not None:
            try:
                self.event_sink.emit(event)
            except Exception as exc:
                self.event_sink = None
                self.session.append("event_sink_error", step, {"error_type": type(exc).__name__})
        return event

    def run(self, task: str | None = None, *, resume: bool = False) -> RunResult:
        if resume:
            if self.checkpoint is None:
                raise ValueError("resume requires workspace checkpointing")
            starts = [event for event in self.session.events if event.type == "run_started"]
            if len(starts) != 1 or any(event.type == "terminal" for event in self.session.events):
                raise ValueError("run cannot be resumed from this trace")
            original = starts[0]
            task = original.payload["task"]
            usage = BudgetUsage()
            for event in self.session.events:
                if event.type == "context_view":
                    usage.steps += 1
                elif event.type == "tool_request":
                    usage.tool_calls += 1
                elif event.type in ("model_response", "model_error"):
                    raw = event.payload.get("usage")
                    usage.add_model(Usage(**raw) if isinstance(raw, dict) else Usage(known=False))
            elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(original.timestamp)).total_seconds())
            start = time.monotonic() - elapsed
        else:
            if not isinstance(task, str) or not task:
                raise ValueError("task is required")
            usage = BudgetUsage()
            start = time.monotonic()
        deadline = start + self.limits.wall_seconds
        tool_schemas = self.runtime.schemas()
        configuration = {
            "task": task, "limits": asdict(self.limits), "context": self.context.name,
            "context_config": getattr(self.context, "configuration", lambda: {})(),
            "model": getattr(self.model, "identity", type(self.model).__name__),
            "model_config": getattr(self.model, "configuration", lambda: {})(),
            "tools": tool_schemas,
            "permission": getattr(self.runtime.permission, "configuration", lambda: {})(),
        }
        if not resume:
            self._append("run_started", 0, configuration)
            if self.checkpoint is not None:
                self._append("workspace_checkpoint", 0, {
                    "format_version": 2, "digest": self.checkpoint(),
                })

        def finish(status: TerminalStatus, reason: str, answer: str = "") -> RunResult:
            if redact_text := getattr(self.session, "redact_text", None):
                answer = redact_text(answer)
            self._append(
                "terminal",
                usage.steps,
                {"status": status.value, "reason": reason, "answer": answer, "usage": asdict(usage)},
            )
            return RunResult(self.session.run_id, status, reason, usage, answer)

        if resume:
            reason = None
            if any(original.payload.get(key) != value for key, value in configuration.items()):
                reason = "configuration_changed"
            events = self.session.events
            model_requests = sum(event.type == "model_request" for event in events)
            model_results = sum(event.type in ("model_response", "model_error") for event in events)
            tool_requests = sum(event.type == "tool_request" for event in events)
            tool_results = sum(event.type == "tool_result" for event in events)
            planned = {
                event.step: len(event.payload.get("tool_calls", []))
                for event in events if event.type == "model_response"
            }
            if any(sum(event.type == "tool_request" and event.step == step for event in events) != count
                   for step, count in planned.items()):
                reason = "pending_tool_calls"
            if model_requests != model_results:
                reason = "unknown_model_result"
                self._append("model_error", usage.steps, {
                    "error": "model result unknown after interruption", "usage": asdict(Usage(known=False)),
                })
                usage.add_model(Usage(known=False))
            if tool_requests != tool_results:
                reason = "unknown_tool_result"
                pending = next(event for event in reversed(events) if event.type == "tool_request")
                self._append("tool_result", pending.step, {
                    "name": pending.payload["name"], "call_id": pending.payload["call_id"],
                    "status": "unknown", "error_code": "interrupted", "output": "tool result unknown after interruption",
                })
                self._append("tool_state", pending.step, {
                    "name": pending.payload["name"], "call_id": pending.payload["call_id"], "state": "unknown",
                })
            if any(
                event.type == "tool_result" and event.payload.get("status") != "ok"
                and getattr(self.runtime.tools.get(event.payload.get("name")), "is_external", False)
                for event in events
            ):
                reason = reason or "external_tool_result_uncertain"
            checkpoints = [event for event in events if event.type == "workspace_checkpoint"]
            if not checkpoints or checkpoints[-1].seq != events[-1].seq:
                reason = reason or "unsafe_checkpoint"
            elif checkpoints[-1].payload.get("format_version") != 2:
                reason = reason or "checkpoint_format_unsupported"
            elif self.checkpoint() != checkpoints[-1].payload.get("digest"):
                reason = reason or "workspace_changed"
            if reason is None:
                responses = sum(event.type == "model_response" for event in events)
                resume_from = getattr(self.model, "resume_from", None)
                if not callable(resume_from):
                    reason = "model_not_resumable"
                else:
                    try:
                        resume_from(responses)
                    except ValueError:
                        reason = "model_state_invalid"
            if reason is not None:
                self._append("recovery_blocked", usage.steps, {"reason": reason})
                return finish(TerminalStatus.FAILED, reason)
            self._append("run_resumed", usage.steps, {
                "remaining_steps": max(0, self.limits.steps - usage.steps),
                "remaining_tokens": max(0, self.limits.tokens - usage.tokens),
                "remaining_tool_calls": max(0, self.limits.tool_calls - usage.tool_calls),
                "remaining_wall_seconds": max(0.0, deadline - time.monotonic()),
            })

        while True:
            if self.cancel.is_set():
                return finish(TerminalStatus.CANCELLED, "external_cancel")
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                return finish(TerminalStatus.BUDGET_EXHAUSTED, "wall_seconds")
            if usage.steps >= self.limits.steps:
                return finish(TerminalStatus.BUDGET_EXHAUSTED, "steps")
            remaining_tokens = self.limits.tokens - usage.tokens
            if remaining_tokens <= 0:
                return finish(TerminalStatus.BUDGET_EXHAUSTED, "tokens")

            try:
                view = self.context.build(self.session.events, task, remaining_tokens)
                prepare_view = getattr(self.model, "prepare_view", None)
                if callable(prepare_view):
                    view = prepare_view(view)
                if redact_context := getattr(self.session, "redact_context", None):
                    view = redact_context(view)
            except Exception as exc:
                self._append("context_error", usage.steps, {"error": f"{type(exc).__name__}: {exc}"})
                return finish(TerminalStatus.FAILED, "context_error")
            estimated_tokens = sum(max(1, (len(message["content"]) + 2) // 3) for message in view.messages)
            if estimated_tokens > remaining_tokens:
                return finish(TerminalStatus.BUDGET_EXHAUSTED, "estimated_input_tokens")

            usage.steps += 1
            view_event = self._append(
                "context_view",
                usage.steps,
                {
                    "strategy": view.strategy,
                    "messages": list(view.messages),
                    "view_version": 1,
                    "messages_sha256": hashlib.sha256(
                        json.dumps(list(view.messages), ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "event_ids": list(view.event_ids),
                    "metadata": dict(view.metadata),
                    "estimated_tokens": estimated_tokens,
                },
            )
            self._append("model_request", usage.steps, {"view_id": view_event.seq})
            try:
                result = self.model.complete(view, tool_schemas, remaining_time)
            except ModelFailure as exc:
                usage.add_model(exc.usage)
                self._append("model_error", usage.steps, {"error": str(exc), "usage": asdict(exc.usage)})
                return finish(TerminalStatus.FAILED, "model_error")
            except Exception as exc:
                usage.add_model(Usage(known=False))
                self._append("model_error", usage.steps, {"error": f"{type(exc).__name__}: {exc}", "usage_known": False})
                return finish(TerminalStatus.FAILED, "model_error")

            usage.add_model(result.usage)
            self._append(
                "model_response",
                usage.steps,
                {
                    "message": result.message,
                    "finish": result.finish,
                    "tool_calls": [asdict(call) for call in result.tool_calls],
                    "usage": asdict(result.usage),
                },
            )
            for index, call in enumerate(result.tool_calls):
                self._append("tool_state", usage.steps, {
                    "name": call.name, "call_id": call.call_id, "index": index, "state": "pending",
                })
            if self.cancel.is_set():
                return finish(TerminalStatus.CANCELLED, "external_cancel")
            if time.monotonic() >= deadline:
                return finish(TerminalStatus.BUDGET_EXHAUSTED, "wall_seconds")
            if usage.tokens > self.limits.tokens:
                return finish(TerminalStatus.BUDGET_EXHAUSTED, "tokens")
            if result.finish and result.tool_calls:
                return finish(TerminalStatus.FAILED, "finish_with_tool_calls")
            if result.finish:
                return finish(TerminalStatus.FINISHED, "model_finished", result.message)
            if not result.tool_calls:
                return finish(TerminalStatus.FAILED, "model_no_action")

            for call in result.tool_calls:
                if self.cancel.is_set():
                    return finish(TerminalStatus.CANCELLED, "external_cancel")
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    return finish(TerminalStatus.BUDGET_EXHAUSTED, "wall_seconds")
                if usage.tool_calls >= self.limits.tool_calls:
                    return finish(TerminalStatus.BUDGET_EXHAUSTED, "tool_calls")
                usage.tool_calls += 1
                self._append("tool_request", usage.steps, {"name": call.name, "arguments": dict(call.arguments), "call_id": call.call_id})
                self._append("tool_state", usage.steps, {
                    "name": call.name, "call_id": call.call_id, "state": "running",
                })
                tool_result = self.runtime.execute(call, remaining_time)
                self._append(
                    "tool_result",
                    usage.steps,
                    {"name": call.name, **asdict(tool_result)},
                )
                self._append("tool_state", usage.steps, {
                    "name": call.name, "call_id": tool_result.call_id,
                    "state": "completed" if tool_result.status == "ok" else "failed",
                })
                if self.checkpoint is not None:
                    self._append("workspace_checkpoint", usage.steps, {
                        "format_version": 2, "digest": self.checkpoint(),
                    })
