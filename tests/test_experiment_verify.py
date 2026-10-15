from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.context import FullHistory, SummaryContext
from agent_harness.experiment import ExperimentPlan, run_experiment, summarize
from agent_harness.experiment_verify import verify_experiment
from agent_harness.model import ScriptedModel
from agent_harness.process import ProcessCapture


class ExperimentVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        baseline = self.root / "baseline"
        baseline.mkdir()
        (baseline / "answer.py").write_text("answer = 42\n")
        self.case_path = self.root / "case.json"
        self.case_path.write_text(json.dumps({
            "schema_version": 1, "case_id": "answer", "task": "check answer",
            "baseline": "baseline", "image": "local-image:1", "public_test": ["python", "-V"],
        }))
        self.experiment = self.root / "experiment"

    def _build_experiment(self) -> None:
        class FakeDockerRunner:
            def __init__(self, image, session):
                pass

            def run(self, argv, workspace, timeout_s, extra_mounts=()):
                return ProcessCapture(0, "ok", 2, None, False)

        plan = ExperimentPlan("answer-test", (self.case_path,), ("full_history", "summary"), repetitions=2, base_seed=42)
        with patch("agent_harness.eval.DockerRunner", FakeDockerRunner):
            run_experiment(
                plan,
                lambda *_: ScriptedModel([{"message": "done", "finish": True,
                                          "usage": {"input_tokens": 7, "output_tokens": 1}}]),
                lambda name, _: FullHistory() if name == "full_history" else SummaryContext(),
                self.root / "runs", self.experiment, "script:test",
                hashlib.sha256(json.dumps({"label": "script:test"}, sort_keys=True).encode()).hexdigest(),
            )

    def test_detects_source_changes_after_relocation(self) -> None:
        self._build_experiment()
        self.case_path.write_text("changed after experiment")
        relocated = self.root / "relocated"
        relocated.mkdir()
        self.experiment.rename(relocated / "experiment")
        (self.root / "runs").rename(relocated / "runs")
        report = verify_experiment(relocated / "experiment")
        self.assertFalse(report["ok"])
        self.assertIn("input_source_changed", {error["code"] for error in report["errors"]})
        self.assertEqual(report["runs"], 4)
        self.assertEqual(report["traces_verified"], 4)
        self.assertTrue(report["input_snapshots"])

    def test_detects_baseline_content_change(self) -> None:
        self._build_experiment()
        (self.root / "baseline" / "answer.py").write_text("answer = 99\n")
        codes = {error["code"] for error in verify_experiment(self.experiment)["errors"]}
        self.assertIn("input_source_changed", codes)

    def test_detects_row_tampering_even_if_summary_is_rewritten(self) -> None:
        self._build_experiment()
        runs_path = self.experiment / "runs.jsonl"
        rows = [json.loads(line) for line in runs_path.read_text().splitlines()]
        rows[0]["input_tokens"] += 1
        rows[0]["total_tokens"] += 1
        runs_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        (self.experiment / "summary.json").write_text(json.dumps(summarize(rows, schema_version=2)))
        report = verify_experiment(self.experiment)
        self.assertFalse(report["ok"])
        self.assertIn("run_record_mismatch", {error["code"] for error in report["errors"]})
        self.assertNotIn("summary_mismatch", {error["code"] for error in report["errors"]})

    def test_detects_changed_input_snapshot_and_missing_run(self) -> None:
        self._build_experiment()
        manifest_path = self.experiment / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["case_snapshots"]["answer"]["image"] = "other-image"
        manifest_path.write_text(json.dumps(manifest))
        self.assertIn("input_snapshot_mismatch", {error["code"] for error in verify_experiment(self.experiment)["errors"]})

        runs_path = self.experiment / "runs.jsonl"
        runs_path.write_text("\n".join(runs_path.read_text().splitlines()[:-1]) + "\n")
        codes = {error["code"] for error in verify_experiment(self.experiment)["errors"]}
        self.assertIn("run_count", codes)
        self.assertIn("run_matrix_incomplete", codes)

    def test_detects_rewritten_summary(self) -> None:
        self._build_experiment()
        summary_path = self.experiment / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["full_history"]["success_rate"] = 0.0
        summary_path.write_text(json.dumps(summary))
        self.assertIn("summary_mismatch", {error["code"] for error in verify_experiment(self.experiment)["errors"]})

    def test_detects_incomplete_and_changed_model_snapshot(self) -> None:
        self._build_experiment()
        path = self.experiment / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["status"] = "running"
        manifest["model_spec"]["label"] = "different"
        path.write_text(json.dumps(manifest))
        codes = {error["code"] for error in verify_experiment(self.experiment)["errors"]}
        self.assertIn("incomplete_experiment", codes)
        self.assertIn("model_config_hash_mismatch", codes)

    def test_rejects_missing_snapshots_and_invalid_context_config(self) -> None:
        self._build_experiment()
        path = self.experiment / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.pop("case_snapshots")
        manifest["context_configs"] = []
        path.write_text(json.dumps(manifest))
        codes = {error["code"] for error in verify_experiment(self.experiment)["errors"]}
        self.assertIn("missing_input_snapshots", codes)
        self.assertIn("invalid_context_snapshot", codes)

    def test_records_context_and_no_unproven_cache_rate(self) -> None:
        self._build_experiment()
        rows = [json.loads(line) for line in (self.experiment / "runs.jsonl").read_text().splitlines()]
        self.assertTrue(all(row["context_tokens_estimated"] > 0 for row in rows))
        self.assertTrue(all(row["cache_hit_rate"] is None for row in rows))
        self.assertTrue(verify_experiment(self.experiment)["ok"])

    def test_unknown_usage_never_counts_as_fully_priced(self) -> None:
        self._build_experiment()
        row = json.loads((self.experiment / "runs.jsonl").read_text().splitlines()[0])
        row["unpriced_calls"] = 0
        row["unknown_usage_calls"] = 1
        row["recorded_cost_usd"] = 0.0
        row["estimated_cost_usd"] = None
        metrics = summarize([row], schema_version=2)[row["strategy"]]
        self.assertEqual(metrics["fully_priced_runs"], 0)
        self.assertIsNone(metrics["mean_cost_usd"])


if __name__ == "__main__":
    unittest.main()
