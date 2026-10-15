"""Paired, per-case comparison of a saved experiment.

Aggregate means hide which task a difference came from, and a single unknown
Usage call can make a whole strategy's mean ``null``. This module compares each
strategy against a baseline strategy *within each case* and reports how many
cases agree, so a small sample cannot be read as a general ranking.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .session import read_trace

# metric -> row field, whether missing values are dropped, and the comparison kind
_METRICS = {
    "total_tokens": ("total_tokens", "pct"),
    "estimated_cost_usd": ("estimated_cost_usd", "pct"),
    "cache_hit_rate": ("cache_hit_rate", "points"),
    "steps": ("steps", "pct"),
    "context_tokens_estimated": ("context_tokens_estimated", "pct"),
}


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _row_values(
    records: Iterable[Mapping[str, Any]], strategy: str, case_id: str, field: str
) -> list[float]:
    values = []
    for row in records:
        if row["strategy"] != strategy or row["case_id"] != case_id:
            continue
        if not row.get("token_usage_complete", True):
            continue
        value = row.get(field)
        if value is None:
            continue
        values.append(float(value))
    return values


def _model_call_counts(records: Iterable[Mapping[str, Any]]) -> dict[str, tuple[int, int]]:
    """Return run_id -> (model calls, extra attempts) from the saved traces.

    Traces written before the retry change carry no `attempts` field, which
    counts as a single attempt.
    """
    counts: dict[str, tuple[int, int]] = {}
    for row in records:
        trace = row.get("trace")
        if not isinstance(trace, str):
            continue
        try:
            events = read_trace(Path(trace))
        except (OSError, ValueError):
            continue
        results = [event for event in events if isinstance(event, Mapping) and event.get("type") in ("model_response", "model_error")]
        attempts = 0
        for event in results:
            payload = event.get("payload")
            value = payload.get("attempts") if isinstance(payload, Mapping) else None
            attempts += int(value) if isinstance(value, int) and value > 0 else 1
        counts[str(row.get("run_id"))] = (len(results), attempts - len(results))
    return counts


def _strategy_metrics(records: list[Mapping[str, Any]], strategies: Sequence[str], cases: Sequence[str]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    calls = _model_call_counts(records)
    for strategy in strategies:
        rows = [row for row in records if row["strategy"] == strategy]
        complete = [row for row in rows if row.get("token_usage_complete", True)]
        priced = [row for row in rows if row.get("estimated_cost_usd") is not None]
        cached = [row for row in rows if row.get("cache_hit_rate") is not None]
        # Tokens and cost must come from the same runs for a meaningful unit price.
        priced_complete = [row for row in complete if row.get("estimated_cost_usd") is not None]
        observed = [calls[str(row.get("run_id"))] for row in rows if str(row.get("run_id")) in calls]
        per_case = {
            case_id: sum(row["case_id"] == case_id for row in rows) for case_id in cases
        }
        summary[strategy] = {
            "runs": len(rows),
            "successes": sum(bool(row["success"]) for row in rows),
            "public_pass_runs": sum(bool(row["public_pass"]) for row in rows),
            "runs_with_complete_usage": len(complete),
            "unknown_usage_runs": len(rows) - len(complete),
            "runs_with_estimated_cost": len(priced),
            "runs_with_cache_breakdown": len(cached),
            "budget_exhausted_runs": sum(row["agent_status"] == "budget_exhausted" for row in rows),
            "median_total_tokens": _median([float(row["total_tokens"]) for row in complete]),
            "median_steps": _median([float(row["steps"]) for row in rows]),
            "median_context_tokens_estimated": _median(
                [float(row["context_tokens_estimated"]) for row in rows]
            ),
            "estimated_cost_usd_total": sum(float(row["estimated_cost_usd"]) for row in priced) if priced else None,
            "median_cache_hit_rate": _median([float(row["cache_hit_rate"]) for row in cached]),
            "priced_total_tokens": sum(float(row["total_tokens"]) for row in priced_complete) if priced_complete else None,
            "priced_estimated_cost_usd": sum(float(row["estimated_cost_usd"]) for row in priced_complete) if priced_complete else None,
            "estimated_usd_per_million_tokens": (
                sum(float(row["estimated_cost_usd"]) for row in priced_complete)
                / sum(float(row["total_tokens"]) for row in priced_complete) * 1_000_000
                if priced_complete and sum(float(row["total_tokens"]) for row in priced_complete) else None
            ),
            "runs_per_case": per_case,
            "minimum_runs_per_case": min(per_case.values()) if per_case else 0,
            "traces_read": len(observed),
            "model_calls": sum(item[0] for item in observed) if observed else None,
            "model_retries": sum(item[1] for item in observed) if observed else None,
        }
    return summary


def _paired_metrics(
    records: list[Mapping[str, Any]],
    strategies: Sequence[str],
    cases: Sequence[str],
    baseline: str,
) -> dict[str, dict]:
    paired: dict[str, dict] = {}
    for strategy in strategies:
        if strategy == baseline:
            continue
        entry: dict[str, Any] = {}
        for name, (field, kind) in _METRICS.items():
            case_deltas: dict[str, float] = {}
            for case_id in cases:
                base = _median(_row_values(records, baseline, case_id, field))
                other = _median(_row_values(records, strategy, case_id, field))
                if base is None or other is None:
                    continue
                if kind == "pct":
                    if not base:
                        continue
                    case_deltas[case_id] = 100 * (other / base - 1)
                else:
                    case_deltas[case_id] = 100 * (other - base)
            values = list(case_deltas.values())
            entry[name] = {
                "unit": "percent" if kind == "pct" else "percentage_points",
                "cases_compared": len(values),
                "cases_positive": sum(value > 0 for value in values),
                "cases_negative": sum(value < 0 for value in values),
                "cases_unchanged": sum(value == 0 for value in values),
                "median_delta": _median(values),
                "min_delta": min(values) if values else None,
                "max_delta": max(values) if values else None,
                "case_deltas": case_deltas,
            }
        paired[strategy] = entry
    return paired


def _caveats(manifest: Mapping[str, Any], records: list[Mapping[str, Any]], strategies: Sequence[str]) -> list[str]:
    notes: list[str] = []
    repetitions = int(manifest.get("repetitions", 0))
    if repetitions < 3:
        notes.append(f"only {repetitions} repetition(s) per case; case-level pairing cannot separate noise")
    unknown = sum(1 for row in records if not row.get("token_usage_complete", True))
    if unknown:
        notes.append(f"{unknown} run(s) lack complete provider usage and are excluded from token and cost summaries")
    missing_cost = sum(1 for row in records if row.get("estimated_cost_usd") is None)
    if missing_cost:
        notes.append(f"{missing_cost} run(s) have no price-snapshot cost estimate")
    exhausted = sum(1 for row in records if row["agent_status"] == "budget_exhausted")
    if exhausted:
        notes.append(f"{exhausted} run(s) ended at a budget limit rather than a model decision")
    if len(strategies) > 1:
        notes.append("case order is fixed and strategy order rotates by repetition; provider-side caching still varies in time")
    notes.append("pairing shows whether a difference is consistent across cases, not why it happened")
    return notes


def analyze_experiment(experiment_dir: Path, baseline_strategy: str = "full_history") -> dict[str, Any]:
    """Compare every strategy against `baseline_strategy` inside each case."""
    experiment_dir = Path(experiment_dir).resolve(strict=True)
    manifest = json.loads((experiment_dir / "manifest.json").read_text(encoding="utf-8"))
    runs_path = experiment_dir / "runs.jsonl"
    records = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records:
        raise ValueError("experiment has no runs")
    strategies = sorted({str(row["strategy"]) for row in records})
    cases = sorted({str(row["case_id"]) for row in records})
    if baseline_strategy not in strategies:
        raise ValueError(f"baseline strategy {baseline_strategy!r} is not part of the experiment")
    return {
        "schema_version": 1,
        "experiment": str(experiment_dir),
        "experiment_id": manifest.get("experiment_id"),
        "model_label": manifest.get("model_label"),
        "baseline_strategy": baseline_strategy,
        "strategies": strategies,
        "cases": cases,
        "runs": len(records),
        "by_strategy": _strategy_metrics(records, strategies, cases),
        "paired_vs_baseline": _paired_metrics(records, strategies, cases, baseline_strategy),
        "caveats": _caveats(manifest, records, strategies),
    }


def _cell(value: float | None, digits: int) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_report(report: Mapping[str, Any]) -> str:
    """Render the analyzer output as a compact text report."""
    lines = [
        f"experiment: {report['experiment_id']} ({report['runs']} runs, model {report['model_label']})",
        f"baseline strategy: {report['baseline_strategy']}",
        "",
        f"{'strategy':<20}{'success':>9}{'unk.usage':>11}{'med.tokens':>12}{'med.steps':>11}"
        f"{'est.cost$':>10}{'cache hit':>11}{'est.$/M':>9}{'retries':>9}",
    ]
    for strategy, metrics in report["by_strategy"].items():
        success = f"{metrics['successes']}/{metrics['runs']}"
        lines.append(
            f"{strategy:<20}{success:>9}{metrics['unknown_usage_runs']:>11}"
            f"{_cell(metrics['median_total_tokens'], 0):>12}"
            f"{_cell(metrics['median_steps'], 1):>11}"
            f"{_cell(metrics['estimated_cost_usd_total'], 5):>10}"
            f"{_cell(metrics['median_cache_hit_rate'], 3):>11}"
            f"{_cell(metrics['estimated_usd_per_million_tokens'], 2):>9}"
            f"{_cell(metrics['model_retries'], 0):>9}"
        )
    lines.append("")
    lines.append("paired per-case deltas vs baseline (case-level medians):")
    for strategy, metrics in report["paired_vs_baseline"].items():
        lines.append(f"  {strategy}")
        for name, values in metrics.items():
            if not values["cases_compared"]:
                lines.append(f"    {name:<26} no comparable case")
                continue
            unit = "pp" if values["unit"] == "percentage_points" else "%"
            lines.append(
                f"    {name:<26} median {values['median_delta']:+.2f}{unit} "
                f"({values['cases_positive']} above / {values['cases_negative']} below / "
                f"{values['cases_unchanged']} equal of {values['cases_compared']} cases)"
            )
    lines.append("")
    lines.append("caveats:")
    lines.extend(f"  - {note}" for note in report["caveats"])
    return "\n".join(lines)
