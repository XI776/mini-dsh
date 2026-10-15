from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from agent_harness.context import FullHistory, SummaryContext
from agent_harness.bundle import export_experiment, verify_bundle
from agent_harness.evidence import seal_experiment, verify_anchor
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

    def _build_experiment(self, model_factory=None, pricing_snapshot=None, image_resolver=None,
                          cost_guard=None) -> None:
        class FakeDockerRunner:
            def __init__(self, image, session):
                pass

            def run(self, argv, workspace, timeout_s, extra_mounts=()):
                return ProcessCapture(0, "ok", 2, None, False)

        plan = ExperimentPlan("answer-test", (self.case_path,), ("full_history", "summary"), repetitions=2, base_seed=42)
        with patch("agent_harness.eval.DockerRunner", FakeDockerRunner):
            run_experiment(
                plan,
                model_factory or (lambda *_: ScriptedModel([{"message": "done", "finish": True,
                                                             "usage": {"input_tokens": 7, "output_tokens": 1}}])),
                lambda name, _: FullHistory() if name == "full_history" else SummaryContext(),
                self.root / "runs", self.experiment, "script:test",
                hashlib.sha256(json.dumps(
                    {"label": "script:test", "max_output_tokens": 4096} if cost_guard else {"label": "script:test"},
                    sort_keys=True).encode()).hexdigest(),
                pricing_snapshot=pricing_snapshot,
                image_resolver=image_resolver,
                model_spec={"label": "script:test", "max_output_tokens": 4096} if cost_guard else None,
                max_estimated_cost_usd=cost_guard,
                max_output_tokens=4096 if cost_guard else None,
            )

    def test_pinned_image_identity_is_recorded_and_verified(self) -> None:
        image_id = "sha256:" + "a" * 64
        self._build_experiment(image_resolver=lambda name: image_id)
        manifest = json.loads((self.experiment / "manifest.json").read_text())
        self.assertEqual(manifest["image_resolution"], "local_image_id")
        self.assertEqual(manifest["case_snapshots"]["answer"]["image"], image_id)
        self.assertEqual(manifest["case_snapshots"]["answer"]["image_source"], "local-image:1")
        self.assertTrue(verify_experiment(self.experiment)["ok"])

    def test_cost_guard_rejects_plan_before_any_run(self) -> None:
        plan = ExperimentPlan("limited", (self.case_path,), ("full_history",), repetitions=1)
        with self.assertRaisesRegex(ValueError, "exceeds cap"):
            run_experiment(
                plan, lambda *_: self.fail("model must not be called"),
                lambda *_: FullHistory(), self.root / "runs", self.experiment,
                "deepseek:flash", "unused",
                pricing_snapshot={"cache_hit": 0.006, "cache_miss": 0.3, "output": 1.2},
                max_estimated_cost_usd=0.000001, max_output_tokens=4096,
            )
        self.assertFalse(self.experiment.exists())

    def test_cost_guard_reserves_for_unknown_usage(self) -> None:
        self._build_experiment(
            model_factory=lambda *_: ScriptedModel([{"message": "done", "finish": True}]),
            pricing_snapshot={"cache_hit": 0.006, "cache_miss": 0.3, "output": 1.2},
            cost_guard=1.0,
        )
        rows = [json.loads(line) for line in (self.experiment / "runs.jsonl").read_text().splitlines()]
        self.assertTrue(all(row["estimated_cost_usd"] is None for row in rows))
        self.assertTrue(all(row["cost_guard_accounted_usd"] > 0 for row in rows))
        self.assertTrue(verify_experiment(self.experiment)["ok"])
        rows[0]["cost_guard_accounted_usd"] = 0
        (self.experiment / "runs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertIn("cost_guard_record_mismatch", {error["code"] for error in verify_experiment(self.experiment)["errors"]})

    def test_independent_anchor_detects_manifest_and_trace_tampering(self) -> None:
        self._build_experiment()
        anchor = self.root / "evidence" / "answer.sha256.json"
        sealed = seal_experiment(self.experiment, anchor)
        self.assertGreaterEqual(sealed["files"], 7)
        self.assertTrue(verify_anchor(self.experiment, anchor)["ok"])
        trace = next((self.root / "runs").glob("*/trace.jsonl"))
        trace.write_text(trace.read_text() + "\n")
        report = verify_anchor(self.experiment, anchor)
        self.assertFalse(report["ok"])
        self.assertIn("evidence_hash_mismatch", {error["code"] for error in report["errors"]})
        with self.assertRaises(ValueError):
            seal_experiment(self.experiment, self.experiment / "inside.json")

    def test_portable_bundle_verifies_without_original_source(self) -> None:
        (self.root / "baseline" / "empty").mkdir()
        self._build_experiment()
        anchor = self.root / "evidence" / "answer.json"
        seal_experiment(self.experiment, anchor)
        archive = self.root / "exports" / "answer.zip"
        result = export_experiment(self.experiment, anchor, archive)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with zipfile.ZipFile(archive) as bundle:
            metadata = json.loads(bundle.read("bundle.json"))
        self.assertEqual(metadata["schema_version"], 2)
        self.assertTrue(any(path.endswith("baseline/empty")
                            for path in metadata["inputs"]["answer"]["directories"]))
        self.case_path.write_text("changed after export")
        report = verify_bundle(archive, anchor)
        self.assertTrue(report["ok"])
        self.assertEqual(report["inputs_checked"], 1)
        self.assertFalse(verify_experiment(self.experiment)["ok"])

    def test_bundle_detects_rehashed_input_tampering(self) -> None:
        self._build_experiment()
        anchor = self.root / "evidence" / "answer.json"
        seal_experiment(self.experiment, anchor)
        archive = self.root / "answer.zip"
        export_experiment(self.experiment, anchor, archive)
        modified = self.root / "modified-input.zip"
        with zipfile.ZipFile(archive) as source:
            metadata = json.loads(source.read("bundle.json"))
            baseline_name = next(name for name in metadata["files"] if name.endswith("baseline/answer.py"))
            altered = b"answer = 99\n"
            metadata["files"][baseline_name] = hashlib.sha256(altered).hexdigest()
            with zipfile.ZipFile(modified, "w") as output:
                for name in source.namelist():
                    if name == baseline_name:
                        output.writestr(name, altered)
                    elif name == "bundle.json":
                        output.writestr(name, json.dumps(metadata))
                    else:
                        output.writestr(name, source.read(name))
        report = verify_bundle(modified, anchor)
        self.assertFalse(report["ok"])
        self.assertIn("bundle_input_baseline_mismatch", {item["code"] for item in report["errors"]})

    def test_legacy_bundle_without_inputs_remains_verifiable(self) -> None:
        self._build_experiment()
        anchor = self.root / "evidence" / "answer.json"
        seal_experiment(self.experiment, anchor)
        archive = self.root / "answer.zip"
        export_experiment(self.experiment, anchor, archive)
        legacy = self.root / "legacy.zip"
        with zipfile.ZipFile(archive) as source:
            metadata = json.loads(source.read("bundle.json"))
            input_files = {metadata["inputs"]["answer"]["manifest"]}
            baseline_prefix = metadata["inputs"]["answer"]["baseline"] + "/"
            input_files.update(name for name in metadata["files"] if name.startswith(baseline_prefix))
            metadata["schema_version"] = 1
            metadata.pop("inputs")
            for name in input_files:
                metadata["files"].pop(name)
            with zipfile.ZipFile(legacy, "w") as output:
                for name in source.namelist():
                    if name in input_files:
                        continue
                    output.writestr(name, json.dumps(metadata) if name == "bundle.json" else source.read(name))
        report = verify_bundle(legacy, anchor)
        self.assertTrue(report["ok"])
        self.assertEqual(report["inputs_checked"], 0)

    def test_bundle_detects_changed_bytes_and_rejects_unsafe_member(self) -> None:
        self._build_experiment()
        anchor = self.root / "evidence" / "answer.json"
        seal_experiment(self.experiment, anchor)
        archive = self.root / "answer.zip"
        export_experiment(self.experiment, anchor, archive)
        modified = self.root / "modified.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(modified, "w") as output:
            for name in source.namelist():
                content = source.read(name)
                output.writestr(name, content + b"x" if name.endswith("runs.jsonl") else content)
        report = verify_bundle(modified, anchor)
        self.assertFalse(report["ok"])
        self.assertIn("bundle_file_hash_mismatch", {item["code"] for item in report["errors"]})
        unsafe = self.root / "unsafe.zip"
        with zipfile.ZipFile(unsafe, "w") as output:
            output.writestr("../escape", "bad")
            output.writestr("bundle.json", "{}")
        self.assertIn("invalid_bundle", {item["code"] for item in verify_bundle(unsafe)["errors"]})

    def test_price_estimate_is_never_recorded_as_provider_cost(self) -> None:
        self._build_experiment(
            model_factory=lambda *_: ScriptedModel([{"message": "done", "finish": True, "usage": {
                "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 4,
                "cache_usage_known": True, "cost_usd": 0.000012, "cost_source": "estimated",
            }}]),
            pricing_snapshot={"cache_hit": 0.1, "cache_miss": 1.0, "output": 2.0},
        )
        rows = [json.loads(line) for line in (self.experiment / "runs.jsonl").read_text().splitlines()]
        self.assertTrue(all(row["recorded_cost_usd"] == 0 for row in rows))
        self.assertTrue(all(row["estimated_cost_usd"] == 0.000012 for row in rows))
        self.assertTrue(all(row["uncached_input_tokens"] == 6 for row in rows))
        self.assertTrue(all(row["total_billable_tokens"] == 12 for row in rows))
        self.assertIsNone(summarize(rows, schema_version=2)["full_history"]["mean_cost_usd"])
        self.assertTrue(verify_experiment(self.experiment)["ok"])

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

    def test_missing_summary_keeps_partial_run_count(self) -> None:
        self._build_experiment()
        manifest_path = self.experiment / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["status"] = "running"
        manifest_path.write_text(json.dumps(manifest))
        (self.experiment / "summary.json").unlink()
        report = verify_experiment(self.experiment)
        self.assertEqual(report["runs"], 4)
        self.assertIn("incomplete_experiment", {error["code"] for error in report["errors"]})

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
        row["token_usage_complete"] = False
        metrics = summarize([row], schema_version=2)[row["strategy"]]
        self.assertEqual(metrics["fully_priced_runs"], 0)
        self.assertIsNone(metrics["mean_cost_usd"])
        self.assertIsNone(metrics["mean_total_tokens"])
        self.assertEqual(metrics["known_usage_runs"], 0)


if __name__ == "__main__":
    unittest.main()
