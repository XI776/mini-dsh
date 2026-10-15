"""Estimate DeepSeek costs from saved experiment traces without model calls."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

from .deepseek import DeepSeekPricing
from .experiment import summarize
from .session import read_trace


def _trace_path(experiment_dir: Path, record: Mapping[str, Any]) -> Path:
    raw = Path(record["trace"])
    relative = record.get("trace_relpath")
    candidates = [experiment_dir / relative] if isinstance(relative, str) and relative else []
    candidates.append(raw)
    if not raw.is_absolute():
        candidates.append(experiment_dir.parent.parent / str(record["run_id"]) / "trace.jsonl")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(f"missing trace for run {record['run_id']}")


def _usage_tokens(raw: Any, run_id: str) -> tuple[int, int, int, bool]:
    if not isinstance(raw, Mapping):
        return 0, 0, 0, False
    values = (raw.get("input_tokens", 0), raw.get("output_tokens", 0), raw.get("cache_read_tokens", 0))
    if not all(type(value) is int and value >= 0 for value in values) or values[2] > values[0]:
        raise ValueError(f"invalid usage in trace for run {run_id}")
    return *values, raw.get("known", True) is True


def _reprice_row(
    experiment_dir: Path, record: Mapping[str, Any], pricing: DeepSeekPricing,
    pricing_model: str | None,
) -> dict[str, Any]:
    run_id = str(record["run_id"])
    events = read_trace(_trace_path(experiment_dir, record))
    if not events or any(event.get("run_id") != run_id for event in events):
        raise ValueError(f"trace run ID mismatch for {run_id}")
    start = next((event for event in events if event["type"] == "run_started"), None)
    model_identity = str(start["payload"].get("model", "")) if start else ""
    if not model_identity.startswith("deepseek:"):
        raise ValueError(f"run {run_id} is not a DeepSeek trace")
    if pricing_model is not None and not model_identity.startswith(f"deepseek:{pricing_model}:"):
        raise ValueError(f"price model does not match run {run_id}")
    terminal = [event for event in events if event["type"] == "terminal"]
    if len(terminal) != 1:
        raise ValueError(f"run {run_id} must have one terminal event")
    usage_events = [event for event in events if event["type"] in ("model_response", "model_error")]
    totals = [0, 0, 0]
    unknown = 0
    estimate = 0.0
    for event in usage_events:
        input_tokens, output_tokens, cache_hits, known = _usage_tokens(event["payload"].get("usage"), run_id)
        totals[0] += input_tokens
        totals[1] += output_tokens
        totals[2] += cache_hits
        estimate += pricing.estimate(input_tokens, output_tokens, cache_hits)
        unknown += not known
    expected = (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"])
    if tuple(totals) != expected or unknown != record["unknown_usage_calls"]:
        raise ValueError(f"trace and experiment usage disagree for run {run_id}")
    priced = dict(record)
    priced["estimated_cost_usd"] = estimate if unknown == 0 else None
    priced["known_cost_lower_bound_usd"] = estimate
    priced["model_calls"] = len(usage_events)
    priced["pricing_complete"] = unknown == 0
    priced["model_identity"] = model_identity
    return priced


def reprice_experiment(
    experiment_dir: Path, pricing: DeepSeekPricing, output_dir: Path,
    pricing_source: str | None = None,
    pricing_model: str | None = None,
) -> dict[str, dict]:
    experiment_dir = Path(experiment_dir).resolve(strict=True)
    manifest_path = experiment_dir / "manifest.json"
    runs_path = experiment_dir / "runs.jsonl"
    manifest_bytes = manifest_path.read_bytes()
    runs_bytes = runs_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    records = [json.loads(line) for line in runs_bytes.splitlines() if line.strip()]
    if manifest.get("schema_version") not in (1, 2) or not records:
        raise ValueError("invalid or empty experiment")
    expected_runs = len(manifest["cases"]) * len(manifest["strategies"]) * manifest["repetitions"]
    if len(records) != expected_runs:
        raise ValueError("experiment run count is incomplete")
    priced = [_reprice_row(experiment_dir, record, pricing, pricing_model) for record in records]
    if len({row["model_identity"] for row in priced}) != 1:
        raise ValueError("experiment mixes DeepSeek models")
    if len({(row["case_id"], row["strategy"], row["repetition"]) for row in priced}) != len(priced):
        raise ValueError("duplicate experiment runs")
    summary = summarize(priced)
    for strategy, metrics in summary.items():
        rows = [row for row in priced if row["strategy"] == strategy]
        complete = [row for row in rows if row["pricing_complete"]]
        metrics["pricing_complete_runs"] = len(complete)
        metrics["known_cost_lower_bound_usd_total"] = sum(row["known_cost_lower_bound_usd"] for row in rows)
        metrics["mean_estimated_cost_usd"] = (
            mean(row["estimated_cost_usd"] for row in rows) if len(complete) == len(rows) else None
        )
    baseline = summary.get("full_history", {}).get("mean_estimated_cost_usd")
    for metrics in summary.values():
        value = metrics["mean_estimated_cost_usd"]
        metrics["estimated_cost_change_vs_full_history_pct"] = (
            100 * (value / baseline - 1) if baseline is not None and value is not None and baseline else None
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    report_manifest = {
        "schema_version": 1,
        "cost_kind": "estimate",
        "price_unit": "USD_per_million_tokens",
        "source_experiment": str(experiment_dir),
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_runs_sha256": hashlib.sha256(runs_bytes).hexdigest(),
        "model_label": manifest["model_label"],
        "model_identity": priced[0]["model_identity"],
        "pricing": asdict(pricing),
        "pricing_source": pricing_source,
    }
    (output_dir / "manifest.json").write_text(json.dumps(report_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "runs.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in priced), encoding="utf-8"
    )
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
