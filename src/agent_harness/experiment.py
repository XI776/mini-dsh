"""Repeatable context ablations and aggregation from per-run traces."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import mean, median
from typing import Callable, Mapping

from .contracts import ContextStrategy, Model
from .eval import EVALUATION_VERSION, EvaluationCase, _fingerprint, evaluate, load_case
from .session import read_trace


@dataclass(frozen=True)
class ExperimentPlan:
    experiment_id: str
    cases: tuple[Path, ...]
    strategies: tuple[str, ...]
    repetitions: int = 1
    base_seed: int = 0

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.cases or not self.strategies:
            raise ValueError("experiment id, cases and strategies are required")
        if self.repetitions < 1 or len(set(self.strategies)) != len(self.strategies):
            raise ValueError("repetitions must be positive and strategies unique")


def load_plan(path: Path) -> ExperimentPlan:
    path = Path(path).resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported experiment schema")
    if not isinstance(data.get("cases"), list) or not isinstance(data.get("strategies"), list):
        raise ValueError("cases and strategies must be arrays")
    if not all(isinstance(case, str) and case for case in data["cases"]):
        raise ValueError("cases must contain nonempty paths")
    if not all(isinstance(strategy, str) and strategy for strategy in data["strategies"]):
        raise ValueError("strategies must contain nonempty names")
    return ExperimentPlan(
        experiment_id=data["experiment_id"],
        cases=tuple((path.parent / case).resolve(strict=True) for case in data["cases"]),
        strategies=tuple(data["strategies"]),
        repetitions=int(data.get("repetitions", 1)),
        base_seed=int(data.get("base_seed", 0)),
    )


def _record(case: EvaluationCase, strategy: str, repetition: int, seed: int, result) -> dict:
    events = read_trace(result.trace)
    terminal = next(event for event in events if event["type"] == "terminal")
    usage = result.run.usage
    start = next(event["payload"] for event in events if event["type"] == "run_started")
    model_usage = [event["payload"].get("usage") for event in events if event["type"] in ("model_response", "model_error")]
    cache_known = bool(model_usage) and all(isinstance(item, dict) and item.get("cache_usage_known") is True for item in model_usage)
    return {
        "case_id": case.case_id,
        "strategy": strategy,
        "repetition": repetition,
        "seed": seed,
        "run_id": result.run.run_id,
        "trace": str(result.trace),
        "baseline_hash": result.baseline_hash,
        "success": result.success,
        "public_pass": result.public_exit_code == 0,
        "holdout_pass": None if result.holdout_exit_code is None else result.holdout_exit_code == 0,
        "agent_status": result.run.status.value,
        "steps": usage.steps,
        "tool_calls": usage.tool_calls,
        "tool_errors": sum(event["type"] == "tool_result" and event["payload"].get("status") != "ok" for event in events),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "total_tokens": usage.tokens,
        "recorded_cost_usd": usage.reported_cost_usd,
        "estimated_cost_usd": (
            usage.estimated_cost_usd
            if usage.unestimated_cost_calls == 0 and usage.unknown_usage_calls == 0 else None
        ),
        "unreported_cost_calls": usage.unreported_cost_calls,
        "unestimated_cost_calls": usage.unestimated_cost_calls,
        "uncached_input_tokens": usage.input_tokens - usage.cache_read_tokens if cache_known else None,
        "total_billable_tokens": usage.tokens if usage.unknown_usage_calls == 0 else None,
        "cache_hit_rate": usage.cache_read_tokens / usage.input_tokens if cache_known and usage.input_tokens else None,
        "context_tokens_estimated": sum(event["payload"]["estimated_tokens"] for event in events if event["type"] == "context_view"),
        "model_identity": start["model"],
        "model_config": start.get("model_config", {}),
        "context_config": start["context_config"],
        "permission": start["permission"],
        "evaluation_version": EVALUATION_VERSION,
        "unpriced_calls": usage.unpriced_calls,
        "unknown_usage_calls": usage.unknown_usage_calls,
        "token_usage_complete": usage.unknown_usage_calls == 0,
        "agent_wall_ms": terminal["monotonic_ms"],
        "evaluation_wall_ms": events[-1]["monotonic_ms"],
    }


def summarize(records: list[Mapping[str, object]], schema_version: int = 1) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for strategy in sorted({str(record["strategy"]) for record in records}):
        rows = [record for record in records if record["strategy"] == strategy]
        tokens_complete = all(row.get("token_usage_complete", True) for row in rows)
        hard = [row for row in rows if row["holdout_pass"] is not None]
        fully_priced = [
            row for row in rows
            if row.get("unreported_cost_calls", row["unpriced_calls"]) == 0
            and (schema_version == 1 or row["unknown_usage_calls"] == 0)
        ]
        result[strategy] = {
            "runs": len(rows),
            "successes": sum(bool(row["success"]) for row in rows),
            "success_rate": mean(bool(row["success"]) for row in rows),
            "public_pass_rate": mean(bool(row["public_pass"]) for row in rows),
            "holdout_pass_rate": mean(bool(row["holdout_pass"]) for row in hard) if hard else None,
            "mean_input_tokens": mean(int(row["input_tokens"]) for row in rows) if tokens_complete else None,
            "mean_output_tokens": mean(int(row["output_tokens"]) for row in rows) if tokens_complete else None,
            "mean_total_tokens": mean(int(row["total_tokens"]) for row in rows) if tokens_complete else None,
            "median_total_tokens": median(int(row["total_tokens"]) for row in rows) if tokens_complete else None,
            "mean_cache_read_tokens": mean(int(row["cache_read_tokens"]) for row in rows) if tokens_complete else None,
            "mean_cache_write_tokens": mean(int(row["cache_write_tokens"]) for row in rows) if tokens_complete else None,
            "mean_agent_wall_ms": mean(int(row["agent_wall_ms"]) for row in rows),
            "mean_evaluation_wall_ms": mean(int(row["evaluation_wall_ms"]) for row in rows),
            "budget_exhausted_rate": mean(row["agent_status"] == "budget_exhausted" for row in rows),
            "tool_error_count": sum(int(row["tool_errors"]) for row in rows),
            "fully_priced_runs": len(fully_priced),
            "mean_cost_usd": (
                mean(float(row["recorded_cost_usd"]) for row in rows)
                if len(fully_priced) == len(rows) else None
            ),
            "recorded_cost_usd_total": sum(float(row["recorded_cost_usd"]) for row in rows),
            "unknown_usage_calls": sum(int(row["unknown_usage_calls"]) for row in rows),
        }
        if schema_version >= 2:
            metrics = result[strategy]
            minimum_repetitions = min(
                sum(row["case_id"] == case_id for row in rows)
                for case_id in {row["case_id"] for row in rows}
            )
            metrics.update({
                "sample_size": len(rows),
                "minimum_repetitions_per_case": minimum_repetitions,
                "insufficient_repetitions": minimum_repetitions < 3,
                "mean_context_tokens_estimated": mean(int(row["context_tokens_estimated"]) for row in rows),
                "mean_tool_calls": mean(int(row["tool_calls"]) for row in rows),
                "mean_tool_errors": mean(int(row["tool_errors"]) for row in rows),
                "mean_steps": mean(int(row["steps"]) for row in rows),
                "min_total_tokens": min(int(row["total_tokens"]) for row in rows) if tokens_complete else None,
                "max_total_tokens": max(int(row["total_tokens"]) for row in rows) if tokens_complete else None,
                "min_evaluation_wall_ms": min(int(row["evaluation_wall_ms"]) for row in rows),
                "max_evaluation_wall_ms": max(int(row["evaluation_wall_ms"]) for row in rows),
                "mean_estimated_cost_usd": (
                    mean(float(row["estimated_cost_usd"]) for row in rows)
                    if all(row["estimated_cost_usd"] is not None for row in rows) else None
                ),
                "mean_cache_hit_rate": (
                    mean(float(row["cache_hit_rate"]) for row in rows)
                    if all(row["cache_hit_rate"] is not None for row in rows) else None
                ),
            })
            if any("token_usage_complete" in row for row in rows):
                metrics["known_usage_runs"] = sum(bool(row.get("token_usage_complete", True)) for row in rows)
    baseline = result.get("full_history")
    if baseline:
        for strategy, metrics in result.items():
            baseline_tokens = baseline["mean_total_tokens"]
            metrics["token_change_vs_full_history_pct"] = (
                100 * (metrics["mean_total_tokens"] / baseline_tokens - 1)
                if baseline_tokens and metrics["mean_total_tokens"] is not None else None
            )
            baseline_cost = baseline["mean_cost_usd"]
            strategy_cost = metrics["mean_cost_usd"]
            metrics["cost_change_vs_full_history_pct"] = (
                100 * (strategy_cost / baseline_cost - 1)
                if baseline_cost is not None and strategy_cost is not None and baseline_cost else None
            )
    return result


def run_experiment(
    plan: ExperimentPlan,
    model_factory: Callable[[EvaluationCase, str, int, int], Model],
    context_factory: Callable[[str, Path], ContextStrategy],
    traces_root: Path,
    output_dir: Path,
    model_label: str,
    model_config_hash: str,
    model_spec: Mapping[str, object] | None = None,
    pricing_snapshot: Mapping[str, object] | None = None,
    image_resolver: Callable[[str], str] | None = None,
    max_estimated_cost_usd: float | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, dict]:
    source_cases = [load_case(path) for path in plan.cases]
    resolved_images = {case.image: image_resolver(case.image) for case in source_cases} if image_resolver else {}
    cases = [replace(case, image=resolved_images[case.image]) for case in source_cases] if image_resolver else source_cases
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("experiment case IDs must be unique")
    if max_estimated_cost_usd is not None:
        if (not isinstance(max_estimated_cost_usd, (int, float)) or isinstance(max_estimated_cost_usd, bool)
                or not math.isfinite(max_estimated_cost_usd) or max_estimated_cost_usd <= 0):
            raise ValueError("max_estimated_cost_usd must be positive and finite")
        if pricing_snapshot is None or max_output_tokens is None:
            raise ValueError("cost guard requires a price snapshot and max output tokens")
        highest_price = max(float(pricing_snapshot[key]) for key in ("cache_hit", "cache_miss", "output"))
        per_case_reserve = {
            case.case_id: (2 * case.limits.tokens + max_output_tokens) * highest_price / 1_000_000
            for case in cases
        }
        planned_upper_estimate = sum(
            per_case_reserve[case.case_id] for case in cases
        ) * len(plan.strategies) * plan.repetitions
        if planned_upper_estimate > max_estimated_cost_usd:
            raise ValueError(f"planned conservative cost estimate ${planned_upper_estimate:.6f} exceeds cap ${max_estimated_cost_usd:.6f}")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(timezone.utc).isoformat()
    context_configs = {name: getattr(context_factory(name, cases[0].baseline), "configuration", lambda: {})() for name in plan.strategies}
    manifest = {
        "schema_version": 2,
        "evaluation_version": EVALUATION_VERSION,
        "status": "running",
        "started_at": started_at,
        "completed_at": None,
        "expected_runs": len(cases) * len(plan.strategies) * plan.repetitions,
        "experiment_id": plan.experiment_id,
        "cases": [str(path) for path in plan.cases],
        "strategies": list(plan.strategies),
        "repetitions": plan.repetitions,
        "base_seed": plan.base_seed,
        "model_label": model_label,
        "model_config_hash": model_config_hash,
        "model_spec": dict(model_spec or {"label": model_label}),
        "pricing_snapshot": dict(pricing_snapshot) if pricing_snapshot else None,
        "pricing_snapshot_sha256": (
            hashlib.sha256(json.dumps(pricing_snapshot, sort_keys=True).encode()).hexdigest()
            if pricing_snapshot else None
        ),
        "context_configs": context_configs,
        "strategy_schedule": "rotate_by_repetition",
        "image_resolution": "local_image_id" if image_resolver else "unresolved",
        "cost_guard": ({"max_estimated_cost_usd": max_estimated_cost_usd,
                        "planned_conservative_estimate_usd": planned_upper_estimate,
                        "per_case_reserve_usd": per_case_reserve,
                        "method": "known_estimate_or_unknown_usage_reserve"}
                       if max_estimated_cost_usd is not None else None),
        "metric_sources": {
            "tokens": "model_usage_or_script_usage", "context_tokens_estimated": "local_context_view_estimate",
            "recorded_cost": "provider_reported_only",
            "estimated_cost": "price_snapshot_estimate_if_all_calls_priced",
            "success": "post_run_tests",
            "cache_hit_rate": "provider_breakdown_only",
        },
        "case_snapshots": {
            case.case_id: {
                "source_path": str(path),
                "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "baseline_hash": _fingerprint(case.baseline),
                "task": case.task,
                "limits": asdict(case.limits),
                "image": case.image,
                "image_source": source_case.image,
                "public_test": list(case.public_test),
                "holdout_test": list(case.holdout_test),
                "allowed_commands": list(case.allowed_commands),
                "build_command": list(case.build_command),
                "test_timeout_s": case.test_timeout_s,
                "holdout_hashes": {
                    name: hashlib.sha256(content.encode()).hexdigest()
                    for name, content in (case.holdout_files or {}).items()
                },
            }
            for path, case, source_case in zip(plan.cases, cases, source_cases)
        },
        "cost_accounting_version": 2,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    records: list[dict] = []
    with (output_dir / "runs.jsonl").open("x", encoding="utf-8") as stream:
        for case in cases:
            for repetition in range(plan.repetitions):
                seed = plan.base_seed + repetition
                ordered_strategies = plan.strategies[repetition % len(plan.strategies):] + plan.strategies[:repetition % len(plan.strategies)]
                for strategy in ordered_strategies:
                    model = model_factory(case, strategy, repetition, seed)
                    result = evaluate(
                        case, model, lambda workspace, name=strategy: context_factory(name, workspace), traces_root
                    )
                    record = _record(case, strategy, repetition, seed, result)
                    if pricing_snapshot is None:
                        record["estimated_cost_usd"] = None
                    if max_estimated_cost_usd is not None:
                        record["cost_guard_accounted_usd"] = (
                            record["estimated_cost_usd"] if record["estimated_cost_usd"] is not None
                            else per_case_reserve[case.case_id]
                        )
                    record["trace_relpath"] = os.path.relpath(result.trace.resolve(), output_dir.resolve())
                    records.append(record)
                    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    stream.flush()
                    if max_estimated_cost_usd is not None:
                        if sum(row["cost_guard_accounted_usd"] for row in records) > max_estimated_cost_usd:
                            raise RuntimeError("cost guard stopped: accounted spend exceeded the cap")
    summary = summarize(records, schema_version=2)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["status"] = "complete"
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
