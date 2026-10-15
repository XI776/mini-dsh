"""Rebuild experiment evidence from saved runs and traces."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

from .eval import _fingerprint
from .experiment import summarize
from .session import read_trace
from .trace_verify import verify_trace


_ROW_FIELDS = (
    "case_id", "strategy", "run_id", "baseline_hash", "success", "public_pass",
    "holdout_pass", "agent_status", "steps", "tool_calls", "tool_errors",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "total_tokens", "recorded_cost_usd", "unpriced_calls", "unknown_usage_calls",
    "agent_wall_ms", "evaluation_wall_ms",
)
_SETUP_FIELDS = (
    "baseline_hash", "image", "public_test", "holdout_test", "allowed_commands",
    "build_command", "test_timeout_s", "holdout_hashes",
)
_V2_ROW_FIELDS = (
    "estimated_cost_usd", "cache_hit_rate", "context_tokens_estimated", "model_identity",
    "context_config", "permission", "evaluation_version",
)


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def _trace_path(experiment: Path, row: dict[str, Any]) -> Path | None:
    raw = Path(str(row.get("trace", "")))
    run_id = str(row.get("run_id", ""))
    relative = row.get("trace_relpath")
    candidates = ([experiment / relative] if isinstance(relative, str) and relative else [])
    candidates += [raw, experiment / raw, experiment.parent.parent / run_id / "trace.jsonl"]
    return next((path for path in candidates if path.is_file()), None)


def _from_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    setup = next(event for event in events if event["type"] == "evaluation_setup")
    start = next(event for event in events if event["type"] == "run_started")
    terminal = next(event for event in events if event["type"] == "terminal")
    public = next(event for event in events if event["type"] == "public_test_result")
    holdout = next((event for event in events if event["type"] == "holdout_test_result"), None)
    evaluation = next(event for event in events if event["type"] == "evaluation_result")
    usage = terminal["payload"]["usage"]
    model_usage = [event["payload"].get("usage") for event in events if event["type"] in ("model_response", "model_error")]
    cache_known = bool(model_usage) and all(isinstance(item, dict) and item.get("cache_usage_known") is True for item in model_usage)
    result = {
        "case_id": setup["payload"]["case_id"],
        "strategy": start["payload"]["context"],
        "run_id": terminal["run_id"],
        "baseline_hash": setup["payload"]["baseline_hash"],
        "success": evaluation["payload"]["success"],
        "public_pass": public["payload"]["exit_code"] == 0,
        "holdout_pass": holdout["payload"]["exit_code"] == 0 if holdout else None,
        "agent_status": terminal["payload"]["status"],
        "steps": usage["steps"],
        "tool_calls": usage["tool_calls"],
        "tool_errors": sum(event["type"] == "tool_result" and event["payload"].get("status") != "ok" for event in events),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"],
        "total_tokens": usage["input_tokens"] + usage["output_tokens"],
        "recorded_cost_usd": usage["cost_usd"],
        "unpriced_calls": usage["unpriced_calls"],
        "unknown_usage_calls": usage["unknown_usage_calls"],
        "agent_wall_ms": terminal["monotonic_ms"],
        "evaluation_wall_ms": events[-1]["monotonic_ms"],
    }
    result.update({
        "estimated_cost_usd": usage["cost_usd"] if usage["unpriced_calls"] == 0 and usage["unknown_usage_calls"] == 0 else None,
        "cache_hit_rate": usage["cache_read_tokens"] / usage["input_tokens"] if cache_known and usage["input_tokens"] else None,
        "context_tokens_estimated": sum(event["payload"]["estimated_tokens"] for event in events if event["type"] == "context_view"),
        "model_identity": start["payload"].get("model"),
        "model_config": start["payload"].get("model_config", {}),
        "context_config": start["payload"].get("context_config"),
        "permission": start["payload"].get("permission"),
        "evaluation_version": setup["payload"].get("evaluation_version"),
    })
    return result


def verify_experiment(directory: Path) -> dict[str, Any]:
    directory = Path(directory)
    errors: list[dict[str, Any]] = []

    def fail(code: str, detail: str = "", run_id: str | None = None) -> None:
        errors.append({"code": code, "detail": detail, "run_id": run_id})

    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (directory / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail("unreadable_experiment", f"{type(exc).__name__}: {exc}")
        return {"ok": False, "experiment": str(directory), "runs": 0, "traces_verified": 0, "input_snapshots": False, "errors": errors}
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in (1, 2) or not isinstance(rows, list) or not isinstance(summary, dict):
        fail("invalid_experiment_schema")
        return {"ok": False, "experiment": str(directory), "runs": len(rows), "traces_verified": 0, "input_snapshots": False, "errors": errors}
    cases, strategies, repetitions = manifest.get("cases"), manifest.get("strategies"), manifest.get("repetitions")
    version = manifest["schema_version"]
    base_seed = manifest.get("base_seed")
    if (not isinstance(cases, list) or not cases or not isinstance(strategies, list) or not strategies
            or any(not isinstance(case, str) or not case for case in cases)
            or any(not isinstance(strategy, str) or not strategy for strategy in strategies)
            or len(set(strategies)) != len(strategies) or type(repetitions) is not int or repetitions < 1
            or type(base_seed) is not int or any(not isinstance(row, dict) for row in rows)):
        fail("invalid_experiment_schema")
        return {"ok": False, "experiment": str(directory), "runs": len(rows), "traces_verified": 0, "input_snapshots": False, "errors": errors}

    snapshots = manifest.get("case_snapshots")
    if version == 2:
        if not isinstance(snapshots, dict) or not snapshots:
            fail("missing_input_snapshots")
        if manifest.get("status") != "complete" or not manifest.get("started_at") or not manifest.get("completed_at"):
            fail("incomplete_experiment")
        if manifest.get("expected_runs") != len(cases) * len(strategies) * repetitions:
            fail("expected_runs_mismatch")
        if not isinstance(manifest.get("model_spec"), dict) or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("model_config_hash", ""))):
            fail("invalid_model_snapshot")
        elif hashlib.sha256(json.dumps(manifest["model_spec"], sort_keys=True).encode()).hexdigest() != manifest["model_config_hash"]:
            fail("model_config_hash_mismatch")
        spec = manifest.get("model_spec", {})
        if isinstance(spec, dict):
            sources = {"script": (spec.get("script_path"), spec.get("script_sha256"))} if spec.get("script_path") else {}
            script_paths = spec.get("script_paths", {})
            script_hashes = spec.get("scripts_sha256", {})
            if isinstance(script_paths, dict) and isinstance(script_hashes, dict):
                for name, path in script_paths.items():
                    sources[name] = (path, script_hashes.get(name))
            elif script_paths or script_hashes:
                fail("invalid_model_snapshot")
            for name, (source, expected_hash) in sources.items():
                if isinstance(source, str) and Path(source).is_file() and hashlib.sha256(Path(source).read_bytes()).hexdigest() != expected_hash:
                    fail("model_source_changed", f"script={name}")
        pricing = manifest.get("pricing_snapshot")
        price_hash = hashlib.sha256(json.dumps(pricing, sort_keys=True).encode()).hexdigest() if pricing is not None else None
        if price_hash != manifest.get("pricing_snapshot_sha256"):
            fail("pricing_snapshot_mismatch")
        context_configs = manifest.get("context_configs")
        if not isinstance(context_configs, dict) or set(context_configs) != set(strategies):
            fail("invalid_context_snapshot")
            context_configs = {}
        if manifest.get("strategy_schedule") != "rotate_by_repetition":
            fail("invalid_strategy_schedule")
    if snapshots is not None and not isinstance(snapshots, dict):
        fail("invalid_input_snapshots")
        snapshots = None
    if snapshots is not None:
        for case_id, snapshot in snapshots.items():
            if (not isinstance(snapshot, dict)
                    or not isinstance(snapshot.get("manifest_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", snapshot["manifest_sha256"]) is None):
                fail("invalid_input_snapshot_hash", f"case_id={case_id}")
    if len(rows) != len(cases) * len(strategies) * repetitions:
        fail("run_count", f"expected={len(cases) * len(strategies) * repetitions}, recorded={len(rows)}")

    case_ids: set[str] = set()
    combinations: set[tuple[str, str, int]] = set()
    run_ids: set[str] = set()
    observed_order: dict[tuple[str, int], list[str]] = {}
    baseline_by_case: dict[str, str] = {}
    model_by_trial: dict[tuple[str, int], str] = {}
    verified = 0
    for index, row in enumerate(rows):
        run_id = str(row.get("run_id", ""))
        case_id, strategy, repetition = row.get("case_id"), row.get("strategy"), row.get("repetition")
        if (not isinstance(case_id, str) or not case_id or not isinstance(strategy, str)
                or strategy not in strategies or type(repetition) is not int or not 0 <= repetition < repetitions):
            fail("invalid_run_coordinates", f"row={index}", run_id)
            continue
        case_ids.add(case_id)
        observed_order.setdefault((case_id, repetition), []).append(strategy)
        coordinate = (case_id, strategy, repetition)
        if coordinate in combinations:
            fail("duplicate_run", f"row={index}", run_id)
        combinations.add(coordinate)
        if run_id in run_ids:
            fail("duplicate_run_id", f"row={index}", run_id)
        run_ids.add(run_id)
        if row.get("seed") != base_seed + repetition:
            fail("seed_mismatch", f"row={index}", run_id)
        trace_path = _trace_path(directory, row)
        if trace_path is None:
            fail("missing_trace", f"row={index}", run_id)
            continue
        trace_report = verify_trace(trace_path)
        if not trace_report["ok"]:
            fail("invalid_trace", f"row={index}; errors={trace_report['errors']}", run_id)
            continue
        verified += 1
        events = read_trace(trace_path)
        try:
            expected = _from_trace(events)
        except (KeyError, StopIteration, TypeError) as exc:
            fail("incomplete_evaluation_trace", f"row={index}; {type(exc).__name__}: {exc}", run_id)
            continue
        if version == 2 and manifest.get("pricing_snapshot") is None:
            expected["estimated_cost_usd"] = None
        for field in _ROW_FIELDS + (_V2_ROW_FIELDS if version == 2 else ()):
            if not _same(row.get(field), expected[field]):
                fail("run_record_mismatch", f"row={index}; field={field}", run_id)
        if version == 2 and "model_config" in row and not _same(row["model_config"], expected["model_config"]):
            fail("run_record_mismatch", f"row={index}; field=model_config", run_id)
        setup = next(event["payload"] for event in events if event["type"] == "evaluation_setup")
        start = next(event["payload"] for event in events if event["type"] == "run_started")
        if version == 2:
            if start.get("context_config") != context_configs.get(strategy):
                fail("context_config_mismatch", f"row={index}", run_id)
            if setup.get("evaluation_version") != manifest.get("evaluation_version"):
                fail("evaluation_version_mismatch", f"row={index}", run_id)
            spec = manifest.get("model_spec", {})
            if isinstance(spec, dict) and spec.get("identity") and start.get("model") != spec["identity"]:
                fail("model_config_mismatch", f"row={index}", run_id)
            if isinstance(spec, dict) and spec.get("type") == "deepseek":
                model_config = start.get("model_config", {})
                if not isinstance(model_config, dict) or model_config.get("pricing") != manifest.get("pricing_snapshot"):
                    fail("model_config_mismatch", f"row={index}; pricing", run_id)
        previous_baseline = baseline_by_case.setdefault(case_id, expected["baseline_hash"])
        if previous_baseline != expected["baseline_hash"]:
            fail("baseline_changed_between_runs", f"case_id={case_id}", run_id)
        trial = (case_id, repetition)
        previous_model = model_by_trial.setdefault(trial, str(start.get("model", "")))
        if previous_model != start.get("model"):
            fail("model_changed_between_strategies", f"case_id={case_id}; repetition={repetition}", run_id)
        if snapshots is not None:
            snapshot = snapshots.get(case_id)
            if not isinstance(snapshot, dict):
                fail("missing_input_snapshot", f"case_id={case_id}", run_id)
            else:
                for field in _SETUP_FIELDS:
                    if not _same(snapshot.get(field), setup.get(field)):
                        fail("input_snapshot_mismatch", f"case_id={case_id}; field={field}", run_id)
                for field in ("task", "limits"):
                    if not _same(snapshot.get(field), start.get(field)):
                        fail("input_snapshot_mismatch", f"case_id={case_id}; field={field}", run_id)

    if len(case_ids) != len(cases):
        fail("case_count", f"expected={len(cases)}, recorded={len(case_ids)}")
    expected_coordinates = {(case_id, strategy, repetition) for case_id in case_ids for strategy in strategies for repetition in range(repetitions)}
    if combinations != expected_coordinates:
        fail("run_matrix_incomplete", f"missing={len(expected_coordinates - combinations)}; extra={len(combinations - expected_coordinates)}")
    if version == 2:
        for (case_id, repetition), order in observed_order.items():
            expected_order = strategies[repetition % len(strategies):] + strategies[:repetition % len(strategies)]
            if order != expected_order:
                fail("strategy_order_mismatch", f"case_id={case_id}; repetition={repetition}")
    if snapshots is not None and set(snapshots) != case_ids:
        fail("input_snapshot_cases", f"expected={sorted(case_ids)}, recorded={sorted(snapshots)}")
    if snapshots is not None and all(isinstance(item, dict) for item in snapshots.values()):
        sources = [item.get("source_path") for item in snapshots.values()]
        if any(not isinstance(source, str) for source in sources) or sorted(sources) != sorted(cases):
            fail("input_snapshot_paths")
        if version == 2:
            for case_id, item in snapshots.items():
                source = item.get("source_path")
                if not isinstance(source, str):
                    continue
                path = Path(source)
                if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != item.get("manifest_sha256"):
                    fail("input_source_changed", f"case_id={case_id}; manifest")
                baseline = None
                if path.is_file():
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        baseline = Path(data["baseline"])
                        if not baseline.is_absolute():
                            baseline = path.parent / baseline
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                if baseline is not None and baseline.is_dir() and _fingerprint(baseline) != item.get("baseline_hash"):
                    fail("input_source_changed", f"case_id={case_id}; baseline")
    try:
        reconstructed = summarize(rows, schema_version=version)
        if not _same(summary, reconstructed):
            fail("summary_mismatch")
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        fail("invalid_run_records", f"{type(exc).__name__}: {exc}")
    return {
        "ok": not errors, "experiment": str(directory), "runs": len(rows),
        "traces_verified": verified, "input_snapshots": snapshots is not None,
        "errors": errors,
    }
