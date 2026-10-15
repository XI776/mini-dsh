"""Locate the first observable divergence between two agent traces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .session import read_trace


_CATEGORIES = {
    "context_view": "context_view",
    "context_error": "context_view",
    "model_response": "model_decision",
    "model_error": "model_decision",
    "tool_request": "tool_request",
    "tool_result": "tool_result",
    "terminal": "terminal",
    "public_test_result": "evaluation",
    "holdout_test_result": "evaluation",
    "evaluation_result": "evaluation",
}


def _first(events: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((event for event in events if event["type"] == kind), None)


def _config_difference(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any] | None:
    left_start, right_start = _first(left, "run_started"), _first(right, "run_started")
    if left_start is None or right_start is None:
        if left_start is not right_start:
            return {"index": -1, "reason": "config_mismatch", "field": "run_started",
                    "left": left_start, "right": right_start}
    else:
        # Context names intentionally differ in an ablation. Tool schemas and
        # descriptions are part of the model input and must match when present.
        for field in ("task", "limits", "model", "tools"):
            if left_start["payload"].get(field) != right_start["payload"].get(field):
                return {"index": -1, "reason": "config_mismatch", "field": field,
                        "left": left_start, "right": right_start}
    left_setup, right_setup = _first(left, "evaluation_setup"), _first(right, "evaluation_setup")
    if left_setup is None or right_setup is None:
        if left_setup is not right_setup:
            return {"index": -1, "reason": "config_mismatch", "field": "evaluation_setup",
                    "left": left_setup, "right": right_setup}
    elif left_setup["payload"] != right_setup["payload"]:
        return {"index": -1, "reason": "config_mismatch", "field": "evaluation_setup",
                "left": left_setup, "right": right_setup}
    return None


def _slot(event: dict[str, Any], counters: dict[tuple[int, str], int]) -> tuple[int, int, int, int]:
    step = int(event["step"])
    kind = event["type"]
    if kind in ("context_view", "context_error"):
        return step, 0, 0, 0
    if kind in ("model_response", "model_error"):
        return step, 1, 0, 0
    if kind in ("tool_request", "tool_result"):
        key = step, kind
        index = counters.get(key, 0)
        counters[key] = index + 1
        return step, 2, index, 0 if kind == "tool_request" else 1
    if kind == "terminal":
        return step, 3, 0, 0
    evaluation_rank = {"public_test_result": 0, "holdout_test_result": 1, "evaluation_result": 2}
    return step, 4, evaluation_rank[kind], 0


def _indexed(events: list[dict[str, Any]]) -> dict[tuple[int, int, int, int], dict[str, Any]]:
    counters: dict[tuple[int, str], int] = {}
    result = {}
    for event in events:
        if event["type"] in _CATEGORIES:
            slot = _slot(event, counters)
            if slot in result:
                raise ValueError(f"duplicate trace event slot: {slot}")
            result[slot] = event
    return result


def _behavior(event: dict[str, Any] | None) -> Any:
    if event is None:
        return None
    kind = event["type"]
    payload = event["payload"]
    if kind == "context_view":
        return kind, payload.get("messages")
    if kind == "model_response":
        calls = [
            {"name": call.get("name"), "arguments": call.get("arguments")}
            for call in payload.get("tool_calls", [])
        ]
        return kind, {"message": payload.get("message"), "finish": payload.get("finish"), "tool_calls": calls}
    ignored = {
        "model_error": {"usage", "usage_known"},
        "tool_request": {"call_id"},
        "tool_result": {"call_id", "duration_ms"},
        "terminal": {"usage"},
    }.get(kind, set())
    return kind, {key: value for key, value in payload.items() if key not in ignored}


def _reason(left: dict[str, Any] | None, right: dict[str, Any] | None) -> str:
    kinds = {event["type"] for event in (left, right) if event is not None}
    if "tool_result" in kinds and any(
        event is not None and event["payload"].get("error_code") == "permission_denied"
        for event in (left, right)
    ):
        return "permission"
    if len(kinds) == 1:
        return _CATEGORIES[next(iter(kinds))]
    if kinds <= {"model_response", "model_error"}:
        return "model_decision"
    if kinds <= {"context_view", "context_error"}:
        return "context_view"
    return "event_sequence"


def _usage(event: dict[str, Any] | None) -> Any:
    if event is None:
        return None
    payload = event["payload"]
    return payload.get("usage"), payload.get("usage_known")


def first_difference(left: Path, right: Path) -> dict[str, Any] | None:
    left_trace, right_trace = read_trace(left), read_trace(right)
    if mismatch := _config_difference(left_trace, right_trace):
        return mismatch
    left_events, right_events = _indexed(left_trace), _indexed(right_trace)
    slots = sorted(left_events.keys() | right_events.keys())
    for index, slot in enumerate(slots):
        left_event, right_event = left_events.get(slot), right_events.get(slot)
        if _behavior(left_event) != _behavior(right_event):
            return {
                "index": index, "step": slot[0], "reason": _reason(left_event, right_event),
                "left_seq": left_event["seq"] if left_event else None,
                "right_seq": right_event["seq"] if right_event else None,
                "left": left_event, "right": right_event,
            }
    # Accounting differences cannot explain an earlier decision, but matter
    # for budget and cost comparisons once behavior is identical.
    for index, slot in enumerate(slots):
        left_event, right_event = left_events.get(slot), right_events.get(slot)
        if left_event and right_event and left_event["type"] in ("model_response", "model_error", "terminal"):
            if _usage(left_event) != _usage(right_event):
                return {
                    "index": index, "step": slot[0], "reason": "usage_accounting",
                    "left_seq": left_event["seq"], "right_seq": right_event["seq"],
                    "left": left_event, "right": right_event,
                }
    return None
