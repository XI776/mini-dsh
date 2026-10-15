"""Audit saved traces without rerunning the model or tools."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .contracts import TerminalStatus


_ARTIFACT_ID = re.compile(r"[0-9a-f]{64}\Z")
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def verify_trace(path: Path) -> dict[str, Any]:
    """Return machine-readable consistency errors for one complete run trace."""
    path = Path(path)
    errors: list[dict[str, Any]] = []

    def fail(code: str, seq: int | None = None, detail: str = "") -> None:
        errors.append({"code": code, "seq": seq, "detail": detail})

    try:
        with path.open(encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail("unreadable_trace", detail=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "trace": str(path), "run_id": None, "events": 0, "errors": errors}

    if not events:
        fail("empty_trace")
        return {"ok": False, "trace": str(path), "run_id": None, "events": 0, "errors": errors}
    run_id = events[0].get("run_id") if isinstance(events[0], dict) else None
    for expected_seq, event in enumerate(events, 1):
        if not isinstance(event, dict) or not all(key in event for key in ("seq", "run_id", "type", "step", "payload")):
            fail("invalid_event", expected_seq)
            continue
        if type(event["seq"]) is not int or event["seq"] != expected_seq:
            fail("sequence_gap", expected_seq, f"recorded={event['seq']!r}")
        if event["run_id"] != run_id or not isinstance(run_id, str) or not run_id:
            fail("run_id_mismatch", expected_seq)
        if event.get("schema_version") != 1:
            fail("schema_version", expected_seq)
        if not isinstance(event["type"], str) or type(event["step"]) is not int or event["step"] < 0 or not isinstance(event["payload"], dict):
            fail("invalid_event", expected_seq)
    if errors:
        return {"ok": False, "trace": str(path), "run_id": run_id, "events": len(events), "errors": errors}

    starts = [event for event in events if event["type"] == "run_started"]
    terminals = [event for event in events if event["type"] == "terminal"]
    if len(starts) != 1:
        fail("run_started_count", detail=f"found={len(starts)}")
    if len(terminals) != 1:
        fail("terminal_count", detail=f"found={len(terminals)}")
    terminal = terminals[0] if len(terminals) == 1 else None
    if terminal and terminal["payload"].get("status") not in {status.value for status in TerminalStatus}:
        fail("terminal_status", terminal["seq"])
    setups = [event for event in events if event["type"] == "evaluation_setup"]
    if len(setups) > 1 or (setups and (not starts or setups[0]["seq"] >= starts[0]["seq"])):
        fail("evaluation_setup_order", setups[0]["seq"] if setups else None)

    views: dict[int, dict[str, Any]] = {}
    model_pending: dict[str, Any] | None = None
    tool_pending: dict[str, Any] | None = None
    planned_calls: dict[int, list[dict[str, Any]]] = {}
    request_count: dict[int, int] = {}
    totals = {field: 0 for field in _USAGE_FIELDS}
    totals.update(steps=0, tool_calls=0, unknown_usage_calls=0, unpriced_calls=0)
    cost = 0.0
    model_calls = 0
    model_requests = 0
    seen_artifacts: set[str] = set()

    def check_artifacts(value: Any, seq: int) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "artifact_id" and item is not None:
                    if not isinstance(item, str) or not _ARTIFACT_ID.fullmatch(item):
                        fail("invalid_artifact_id", seq, str(item))
                    elif item not in seen_artifacts:
                        seen_artifacts.add(item)
                        artifact = path.parent / "artifacts" / item
                        try:
                            digest = hashlib.sha256()
                            with artifact.open("rb") as stream:
                                while chunk := stream.read(1024 * 1024):
                                    digest.update(chunk)
                            if digest.hexdigest() != item:
                                fail("artifact_hash_mismatch", seq, item)
                        except OSError:
                            fail("missing_artifact", seq, item)
                else:
                    check_artifacts(item, seq)
        elif isinstance(value, list):
            for item in value:
                check_artifacts(item, seq)

    for event in events:
        seq, step, kind, payload = event["seq"], event["step"], event["type"], event["payload"]
        if kind in {"tool_result", "public_test_result", "holdout_test_result"}:
            check_artifacts(payload, seq)
        if terminal and seq > terminal["seq"] and kind in {
            "run_started", "context_view", "context_error", "model_request", "model_response",
            "model_error", "tool_request", "tool_result", "tool_state", "workspace_checkpoint",
            "run_resumed", "terminal",
        }:
            fail("agent_event_after_terminal", seq, kind)
        if kind == "tool_state":
            state = payload.get("state")
            previous = events[seq - 2] if seq > 1 else None
            expected_previous = {
                "pending": {"model_response", "tool_state"},
                "running": {"tool_request"},
                "completed": {"tool_result"},
                "failed": {"tool_result"},
                "unknown": {"tool_result"},
            }
            if state not in expected_previous or previous is None or previous["type"] not in expected_previous[state]:
                fail("invalid_tool_state", seq)
            elif state in ("completed", "failed", "unknown"):
                result_status = previous["payload"].get("status")
                expected_state = "completed" if result_status == "ok" else "unknown" if result_status == "unknown" else "failed"
                if state != expected_state or payload.get("name") != previous["payload"].get("name"):
                    fail("invalid_tool_state", seq)
        if kind == "workspace_checkpoint":
            if payload.get("format_version") not in (None, 2) or not isinstance(payload.get("digest"), str) or not _ARTIFACT_ID.fullmatch(payload["digest"]):
                fail("invalid_workspace_checkpoint", seq)
        if kind == "context_view":
            totals["steps"] += 1
            if step != totals["steps"]:
                fail("context_step_sequence", seq, f"expected={totals['steps']}, recorded={step}")
            views[seq] = event
            references = payload.get("event_ids")
            if not isinstance(references, list) or any(type(ref) is not int or ref < 1 or ref >= seq for ref in references):
                fail("invalid_context_references", seq)
        elif kind == "model_request":
            model_requests += 1
            view = views.get(payload.get("view_id")) if type(payload.get("view_id")) is int else None
            if view is None or view["step"] != step:
                fail("invalid_view_id", seq)
            if model_pending is not None:
                fail("model_request_unpaired", seq)
            model_pending = event
        elif kind in {"model_response", "model_error"}:
            model_calls += 1
            if model_pending is None or model_pending["step"] != step:
                fail("model_result_unpaired", seq)
            model_pending = None
            if kind == "model_response":
                calls = payload.get("tool_calls")
                if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
                    fail("invalid_tool_calls", seq)
                else:
                    planned_calls[step] = calls
            raw = payload.get("usage")
            usage_unavailable = raw is None and kind == "model_error" and payload.get("usage_known") is False
            if usage_unavailable:
                raw = {"known": False}
            if not isinstance(raw, dict):
                fail("missing_model_usage", seq)
                raw = {"known": False}
            elif not usage_unavailable and (any(field not in raw for field in _USAGE_FIELDS) or "known" not in raw or "cost_usd" not in raw):
                fail("incomplete_model_usage", seq)
            for field in _USAGE_FIELDS:
                value = raw.get(field, 0)
                if type(value) is not int or value < 0:
                    fail("invalid_model_usage", seq, field)
                else:
                    totals[field] += value
            if raw.get("known", True) is not True:
                totals["unknown_usage_calls"] += 1
            price = raw.get("cost_usd")
            if price is None:
                totals["unpriced_calls"] += 1
            elif isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
                fail("invalid_model_usage", seq, "cost_usd")
            else:
                cost += price
        elif kind == "tool_request":
            totals["tool_calls"] += 1
            if tool_pending is not None:
                fail("tool_request_unpaired", seq)
            index = request_count.get(step, 0)
            request_count[step] = index + 1
            planned = planned_calls.get(step, [])
            if index >= len(planned) or payload.get("name") != planned[index].get("name") or payload.get("arguments") != planned[index].get("arguments"):
                fail("tool_request_not_planned", seq)
            tool_pending = event
        elif kind == "tool_result":
            if tool_pending is None:
                fail("tool_result_unpaired", seq)
            else:
                requested = tool_pending["payload"]
                if tool_pending["step"] != step or requested.get("name") != payload.get("name"):
                    fail("tool_result_mismatch", seq)
                if requested.get("call_id") and requested["call_id"] != payload.get("call_id"):
                    fail("tool_call_id_mismatch", seq)
                tool_pending = None
    if model_pending is not None:
        fail("model_request_unpaired", model_pending["seq"])
    if model_requests != len(views):
        fail("context_request_count", detail=f"views={len(views)}, requests={model_requests}")
    if tool_pending is not None:
        fail("tool_request_unpaired", tool_pending["seq"])
    if terminal:
        recorded = terminal["payload"].get("usage")
        if not isinstance(recorded, dict):
            fail("missing_terminal_usage", terminal["seq"])
        else:
            for field, expected in totals.items():
                if type(recorded.get(field)) is not int or recorded[field] != expected:
                    fail("usage_mismatch", terminal["seq"], f"{field}: expected={expected}, recorded={recorded.get(field)!r}")
            recorded_cost = recorded.get("cost_usd")
            if isinstance(recorded_cost, bool) or not isinstance(recorded_cost, (int, float)) or not math.isclose(recorded_cost, cost, rel_tol=1e-9, abs_tol=1e-12):
                fail("usage_mismatch", terminal["seq"], f"cost_usd: expected={cost}, recorded={recorded_cost!r}")

    evaluation = [event for event in events if event["type"] == "evaluation_result"]
    if setups and "holdout_test" in setups[0]["payload"]:
        expected_holdout = bool(setups[0]["payload"].get("holdout_test"))
        recorded_holdout = any(event["type"] == "holdout_test_result" for event in events)
        if expected_holdout != recorded_holdout:
            fail("holdout_result_count", detail=f"expected={int(expected_holdout)}, recorded={int(recorded_holdout)}")
    if not evaluation and any(event["type"] in {"evaluation_setup", "public_test_result", "holdout_test_result"} for event in events):
        fail("evaluation_result_count", detail="found=0")
    if evaluation:
        if len(evaluation) != 1:
            fail("evaluation_result_count", detail=f"found={len(evaluation)}")
        for result in evaluation:
            if setups and result["payload"].get("case_id") != setups[0]["payload"].get("case_id"):
                fail("evaluation_case_mismatch", result["seq"])
            public = [event for event in events if event["type"] == "public_test_result"]
            holdout = [event for event in events if event["type"] == "holdout_test_result"]
            valid_order = (
                len(public) == 1 and len(holdout) <= 1 and terminal is not None
                and terminal["seq"] < public[0]["seq"] < result["seq"]
            )
            if not valid_order:
                fail("evaluation_order", result["seq"])
                continue
            if holdout and not public[0]["seq"] < holdout[0]["seq"] < result["seq"]:
                fail("evaluation_order", result["seq"])
            expected_public = public[0]["payload"].get("exit_code")
            expected_holdout = holdout[0]["payload"].get("exit_code") if holdout else None
            if type(expected_public) is not int or (holdout and type(expected_holdout) is not int):
                fail("invalid_test_result", result["seq"])
            expected_success = expected_public == 0 and (expected_holdout is None or expected_holdout == 0)
            if any((result["payload"].get("public_exit_code") != expected_public,
                    result["payload"].get("holdout_exit_code") != expected_holdout,
                    result["payload"].get("success") is not expected_success,
                    result["payload"].get("agent_status") != terminal["payload"].get("status"))):
                fail("evaluation_mismatch", result["seq"])

    return {
        "ok": not errors, "trace": str(path), "run_id": run_id, "events": len(events),
        "model_calls": model_calls, "tool_calls": totals["tool_calls"],
        "artifacts_checked": len(seen_artifacts), "errors": errors,
    }
