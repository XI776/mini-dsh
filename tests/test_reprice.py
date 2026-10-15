from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_harness.deepseek import DeepSeekPricing
from agent_harness.reprice import reprice_experiment
from agent_harness.session import JsonlSession


class RepriceTests(unittest.TestCase):
    def test_missing_usage_does_not_become_zero_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with JsonlSession(root / "runs") as session:
                session.append("run_started", 0, {"model": "deepseek:deepseek-flash:https://api.deepseek.com"})
                session.append("model_error", 1, {"usage": {"known": False}})
                session.append("terminal", 1, {"status": "failed"})
                record = {
                    "case_id": "case", "strategy": "full_history", "repetition": 0,
                    "run_id": session.run_id, "trace": str(session.root / "trace.jsonl"),
                    "success": False, "public_pass": False, "holdout_pass": False,
                    "agent_status": "failed", "tool_errors": 0,
                    "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                    "cache_write_tokens": 0, "total_tokens": 0,
                    "agent_wall_ms": 10, "evaluation_wall_ms": 20,
                    "recorded_cost_usd": 0.0, "unpriced_calls": 1, "unknown_usage_calls": 1,
                }
            experiment = root / "experiment"
            experiment.mkdir()
            (experiment / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "cases": ["case"], "strategies": ["full_history"],
                "repetitions": 1, "model_label": "deepseek:deepseek-flash",
            }))
            (experiment / "runs.jsonl").write_text(json.dumps(record) + "\n")
            summary = reprice_experiment(experiment, DeepSeekPricing(0.1, 1.0, 2.0), root / "priced")
            self.assertIsNone(summary["full_history"]["mean_estimated_cost_usd"])
            self.assertEqual(summary["full_history"]["pricing_complete_runs"], 0)

    def test_reprice_uses_cache_hits_and_failed_call_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            for strategy, hits, event_type in (
                ("full_history", 0, "model_response"),
                ("summary", 80, "model_error"),
            ):
                with JsonlSession(root / "runs") as session:
                    session.append("run_started", 0, {"model": "deepseek:deepseek-flash:https://api.deepseek.com"})
                    session.append(event_type, 1, {"usage": {
                        "input_tokens": 100, "output_tokens": 10,
                        "cache_read_tokens": hits, "known": True, "cost_usd": None,
                    }})
                    session.append("terminal", 1, {"status": "finished" if event_type == "model_response" else "failed"})
                    records.append({
                        "case_id": "case", "strategy": strategy, "repetition": 0,
                        "run_id": session.run_id, "trace": str(session.root / "trace.jsonl"),
                        "success": event_type == "model_response", "public_pass": event_type == "model_response",
                        "holdout_pass": event_type == "model_response", "agent_status": "finished",
                        "tool_errors": 0, "input_tokens": 100, "output_tokens": 10,
                        "cache_read_tokens": hits, "cache_write_tokens": 0, "total_tokens": 110,
                        "agent_wall_ms": 100, "evaluation_wall_ms": 150,
                        "recorded_cost_usd": 0.0, "unpriced_calls": 1, "unknown_usage_calls": 0,
                    })
            experiment = root / "experiment"
            experiment.mkdir()
            (experiment / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "cases": ["case"], "strategies": ["full_history", "summary"],
                "repetitions": 1, "model_label": "deepseek:deepseek-flash",
            }))
            runs_path = experiment / "runs.jsonl"
            original = "".join(json.dumps(record) + "\n" for record in records)
            runs_path.write_text(original)
            output = root / "repriced"
            summary = reprice_experiment(experiment, DeepSeekPricing(0.1, 1.0, 2.0), output)
            self.assertAlmostEqual(summary["full_history"]["mean_estimated_cost_usd"], 120 / 1_000_000)
            self.assertAlmostEqual(summary["summary"]["mean_estimated_cost_usd"], 48 / 1_000_000)
            self.assertAlmostEqual(summary["summary"]["estimated_cost_change_vs_full_history_pct"], -60)
            self.assertEqual(summary["summary"]["pricing_complete_runs"], 1)
            self.assertEqual(runs_path.read_text(), original)
            self.assertEqual(json.loads((output / "manifest.json").read_text())["cost_kind"], "estimate")

            with self.assertRaisesRegex(ValueError, "price model does not match"):
                reprice_experiment(experiment, DeepSeekPricing(0.1, 1.0, 2.0), root / "wrong-model",
                                   pricing_model="deepseek-v4-pro")

            records[0]["input_tokens"] = 101
            runs_path.write_text("".join(json.dumps(record) + "\n" for record in records))
            with self.assertRaisesRegex(ValueError, "usage disagree"):
                reprice_experiment(experiment, DeepSeekPricing(0.1, 1.0, 2.0), root / "invalid")
            self.assertFalse((root / "invalid").exists())


if __name__ == "__main__":
    unittest.main()
