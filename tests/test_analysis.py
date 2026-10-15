from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_harness.analysis import analyze_experiment, render_report


def _row(case_id, strategy, repetition, *, tokens, success=True, complete=True, cost=0.001,
         cache=0.5, steps=5, status="finished", context_tokens=1000):
    return {
        "case_id": case_id,
        "strategy": strategy,
        "repetition": repetition,
        "success": success,
        "public_pass": success,
        "holdout_pass": success,
        "agent_status": status,
        "steps": steps,
        "tool_calls": 2,
        "tool_errors": 0,
        "input_tokens": tokens,
        "output_tokens": 0,
        "cache_read_tokens": int(tokens * (cache or 0)),
        "cache_write_tokens": 0,
        "total_tokens": tokens,
        "estimated_cost_usd": cost if complete else None,
        "cache_hit_rate": cache if complete else None,
        "context_tokens_estimated": context_tokens,
        "unknown_usage_calls": 0 if complete else 1,
        "token_usage_complete": complete,
    }


def _write_experiment(directory: Path, rows, repetitions=2) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps({
        "schema_version": 2, "experiment_id": "unit", "model_label": "test",
        "repetitions": repetitions, "strategies": sorted({row["strategy"] for row in rows}),
        "cases": sorted({row["case_id"] for row in rows}), "status": "completed",
    }), encoding="utf-8")
    (directory / "runs.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return directory


class AnalyzeTests(unittest.TestCase):
    def test_pairs_each_case_against_the_baseline(self) -> None:
        rows = []
        for repetition in range(2):
            rows.append(_row("case-a", "full_history", repetition, tokens=1000))
            rows.append(_row("case-a", "summary", repetition, tokens=600))
            rows.append(_row("case-b", "full_history", repetition, tokens=2000))
            rows.append(_row("case-b", "summary", repetition, tokens=1800))
        with tempfile.TemporaryDirectory() as temp:
            experiment = _write_experiment(Path(temp) / "exp", rows)
            report = analyze_experiment(experiment)
        paired = report["paired_vs_baseline"]["summary"]["total_tokens"]
        self.assertEqual(paired["cases_compared"], 2)
        self.assertEqual(paired["cases_negative"], 2)
        self.assertEqual(paired["cases_positive"], 0)
        self.assertAlmostEqual(paired["case_deltas"]["case-a"], -40.0)
        self.assertAlmostEqual(paired["case_deltas"]["case-b"], -10.0)
        self.assertAlmostEqual(paired["median_delta"], -25.0)
        self.assertEqual(report["by_strategy"]["summary"]["runs"], 4)

    def test_runs_without_complete_usage_are_excluded_and_reported(self) -> None:
        rows = [
            _row("case-a", "full_history", 0, tokens=1000),
            _row("case-a", "full_history", 1, tokens=1200),
            _row("case-a", "summary", 0, tokens=500),
            _row("case-a", "summary", 1, tokens=100, complete=False),
        ]
        with tempfile.TemporaryDirectory() as temp:
            report = analyze_experiment(_write_experiment(Path(temp) / "exp", rows))
        metrics = report["by_strategy"]["summary"]
        self.assertEqual(metrics["unknown_usage_runs"], 1)
        self.assertEqual(metrics["runs_with_complete_usage"], 1)
        self.assertEqual(metrics["median_total_tokens"], 500)
        self.assertEqual(metrics["runs_with_estimated_cost"], 1)
        self.assertIn("lack complete provider usage", " ".join(report["caveats"]))

    def test_cache_hit_difference_uses_percentage_points(self) -> None:
        rows = [
            _row("case-a", "full_history", 0, tokens=1000, cache=0.8),
            _row("case-a", "summary", 0, tokens=1000, cache=0.5),
        ]
        with tempfile.TemporaryDirectory() as temp:
            report = analyze_experiment(_write_experiment(Path(temp) / "exp", rows, repetitions=1))
        cache = report["paired_vs_baseline"]["summary"]["cache_hit_rate"]
        self.assertEqual(cache["unit"], "percentage_points")
        self.assertAlmostEqual(cache["median_delta"], -30.0)
        self.assertIn("-30.00pp", render_report(report))

    def test_price_snapshot_cost_is_labeled_as_estimated(self) -> None:
        rows = [_row("case-a", "full_history", 0, tokens=1000, cost=0.002)]
        with tempfile.TemporaryDirectory() as temp:
            report = analyze_experiment(_write_experiment(Path(temp) / "exp", rows, repetitions=1))
        metrics = report["by_strategy"]["full_history"]
        self.assertEqual(metrics["priced_estimated_cost_usd"], 0.002)
        self.assertEqual(metrics["estimated_usd_per_million_tokens"], 2.0)
        self.assertNotIn("billed_cost_usd", metrics)
        self.assertIn("est.cost$", render_report(report))

    def test_a_strategy_without_comparable_cases_is_reported_as_such(self) -> None:
        rows = [
            _row("case-a", "full_history", 0, tokens=1000),
            _row("case-a", "summary", 0, tokens=500, complete=False),
        ]
        with tempfile.TemporaryDirectory() as temp:
            report = analyze_experiment(_write_experiment(Path(temp) / "exp", rows))
        paired = report["paired_vs_baseline"]["summary"]["total_tokens"]
        self.assertEqual(paired["cases_compared"], 0)
        self.assertIsNone(paired["median_delta"])
        self.assertIn("no comparable case", render_report(report))

    def test_single_repetition_is_flagged(self) -> None:
        rows = [_row("case-a", "full_history", 0, tokens=1000), _row("case-a", "summary", 0, tokens=900)]
        with tempfile.TemporaryDirectory() as temp:
            report = analyze_experiment(_write_experiment(Path(temp) / "exp", rows, repetitions=1))
        self.assertIn("1 repetition", " ".join(report["caveats"]))

    def test_unknown_baseline_is_rejected(self) -> None:
        rows = [_row("case-a", "summary", 0, tokens=900)]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                analyze_experiment(_write_experiment(Path(temp) / "exp", rows), "full_history")

    def test_render_report_lists_every_strategy(self) -> None:
        rows = [_row("case-a", "full_history", 0, tokens=1000), _row("case-a", "repository", 0, tokens=700)]
        with tempfile.TemporaryDirectory() as temp:
            text = render_report(analyze_experiment(_write_experiment(Path(temp) / "exp", rows)))
        self.assertIn("full_history", text)
        self.assertIn("repository", text)
        self.assertIn("paired per-case deltas", text)


if __name__ == "__main__":
    unittest.main()
