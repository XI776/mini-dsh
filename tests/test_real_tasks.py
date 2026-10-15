"""Solvability checks for the real-model task fixtures.

Every case under ``benchmarks/real`` must start from a failing public test
suite, pass public and holdout tests once the matching ``reference/`` directory
is copied over the baseline, and keep its holdout content out of the baseline
the agent is given.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REAL_ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "real"
HOLDOUT_MOUNT = "/holdout"


def _run(command: list[str], workspace: Path, holdout: Path | None = None) -> subprocess.CompletedProcess:
    argv = [sys.executable if part == "python" else part for part in command]
    if holdout is not None:
        argv = [str(holdout) if part == HOLDOUT_MOUNT else part for part in argv]
    return subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=120)


class RealTaskTests(unittest.TestCase):
    def test_every_real_case_fails_first_and_passes_after_the_reference_fix(self) -> None:
        manifests = sorted(REAL_ROOT.glob("*.json"))
        self.assertEqual([path.name for path in manifests], [
            "config_migration.json", "duration.json", "inventory.json",
            "logtriage.json", "pipeline.json", "textrefactor.json",
        ])
        for manifest_path in manifests:
            with self.subTest(case=manifest_path.name):
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                baseline = (manifest_path.parent / data["baseline"]).resolve()
                reference = manifest_path.parent / manifest_path.stem / "reference"
                self.assertTrue(reference.is_dir(), f"missing reference fix for {manifest_path.name}")
                with tempfile.TemporaryDirectory() as directory:
                    temporary = Path(directory)
                    workspace = temporary / "workspace"
                    shutil.copytree(baseline, workspace)
                    holdout = temporary / "holdout"
                    holdout.mkdir()
                    for name, content in data["holdout_files"].items():
                        (holdout / name).write_text(content, encoding="utf-8")
                    before = _run(data["public_test"], workspace)
                    self.assertNotEqual(
                        before.returncode, 0,
                        f"{manifest_path.name} public tests already pass on the baseline",
                    )
                    shutil.copytree(reference, workspace, dirs_exist_ok=True)
                    after = _run(data["public_test"], workspace)
                    self.assertEqual(
                        after.returncode, 0,
                        f"{manifest_path.name} public tests fail after the reference fix:\n{after.stderr}",
                    )
                    held = _run(data["holdout_test"], workspace, holdout)
                    self.assertEqual(
                        held.returncode, 0,
                        f"{manifest_path.name} holdout tests fail after the reference fix:\n{held.stderr}",
                    )

    def test_holdout_content_is_absent_from_the_baseline(self) -> None:
        for manifest_path in sorted(REAL_ROOT.glob("*.json")):
            with self.subTest(case=manifest_path.name):
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                baseline = (manifest_path.parent / data["baseline"]).resolve()
                contents = [content for content in data["holdout_files"].values() if content]
                for path in baseline.rglob("*"):
                    if not path.is_file():
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    for content in contents:
                        self.assertNotIn(content, text, f"holdout content leaked into {path}")


if __name__ == "__main__":
    unittest.main()
