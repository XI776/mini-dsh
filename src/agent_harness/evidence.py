"""Independent SHA-256 anchor for completed experiment files."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .experiment_verify import _trace_path, verify_experiment


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_files(experiment: Path) -> set[Path]:
    files = {(experiment / name).resolve(strict=True) for name in ("manifest.json", "runs.jsonl", "summary.json")}
    rows = [json.loads(line) for line in (experiment / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        trace = _trace_path(experiment, row)
        if trace is None:
            raise ValueError(f"missing trace for run {row.get('run_id')}")
        root = trace.parent
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"symlink in run evidence: {path}")
            if path.is_file():
                files.add(path.resolve(strict=True))
    return files


def seal_experiment(experiment: Path, anchor: Path) -> dict[str, Any]:
    """Write an anchor outside the experiment directory after full verification."""
    experiment = Path(experiment).resolve(strict=True)
    anchor = Path(anchor).resolve()
    if anchor.is_relative_to(experiment):
        raise ValueError("anchor must be outside the experiment directory")
    report = verify_experiment(experiment)
    if not report["ok"]:
        raise ValueError(f"experiment verification failed: {report['errors']}")
    anchor.parent.mkdir(parents=True, exist_ok=True)
    files = _evidence_files(experiment)
    document = {
        "schema_version": 1,
        "experiment": os.path.relpath(experiment, anchor.parent),
        "files": {
            os.path.relpath(path, anchor.parent): _sha256(path)
            for path in sorted(files)
        },
    }
    with anchor.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return {"anchor": str(anchor), "files": len(files), "experiment": str(experiment)}


def verify_anchor(experiment: Path, anchor: Path, *, check_external_sources: bool = True) -> dict[str, Any]:
    """Check file bytes against a separately stored anchor and the trace audit."""
    experiment = Path(experiment).resolve()
    anchor = Path(anchor).resolve(strict=True)
    errors: list[dict[str, str]] = []
    try:
        document = json.loads(anchor.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not isinstance(document.get("files"), dict):
            raise ValueError("invalid anchor schema")
        if (anchor.parent / document["experiment"]).resolve() != experiment:
            errors.append({"code": "experiment_path_mismatch", "path": str(experiment)})
        expected = document["files"]
        actual_paths = _evidence_files(experiment)
        actual = {os.path.relpath(path, anchor.parent): _sha256(path) for path in actual_paths}
        for path in sorted(expected.keys() | actual.keys()):
            if path not in actual:
                errors.append({"code": "missing_evidence_file", "path": path})
            elif path not in expected:
                errors.append({"code": "unanchored_evidence_file", "path": path})
            elif actual[path] != expected[path]:
                errors.append({"code": "evidence_hash_mismatch", "path": path})
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        errors.append({"code": "invalid_anchor", "path": f"{type(exc).__name__}: {exc}"})
    report = verify_experiment(experiment, check_external_sources=check_external_sources)
    return {"ok": not errors and report["ok"], "anchor": str(anchor), "experiment": str(experiment),
            "files_checked": len(actual) if 'actual' in locals() else 0, "errors": errors, "experiment_report": report}
