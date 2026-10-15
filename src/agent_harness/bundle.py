"""Portable, self-contained copies of an experiment's saved evidence."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .evidence import _sha256, verify_anchor
from .eval import _fingerprint, load_case


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FILE = 512 * 1024 * 1024
_MAX_TOTAL = 2 * 1024 * 1024 * 1024


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and "\\" not in name and ":" not in name
            and all(part not in ("", ".", "..") for part in name.split("/")) and str(path) == name)


def export_experiment(experiment: Path, anchor: Path, archive: Path) -> dict[str, Any]:
    """Copy the anchor and every anchored file without modifying original evidence."""
    experiment = Path(experiment).resolve(strict=True)
    anchor = Path(anchor).resolve(strict=True)
    report = verify_anchor(experiment, anchor)
    if not report["ok"]:
        raise ValueError(f"experiment evidence is invalid: {report['errors']}")
    document = json.loads(anchor.read_text(encoding="utf-8"))
    source_paths = {anchor, *((anchor.parent / name).resolve(strict=True) for name in document["files"])}
    experiment_manifest = json.loads((experiment / "manifest.json").read_text(encoding="utf-8"))
    snapshots = experiment_manifest.get("case_snapshots")
    inputs: dict[str, dict[str, Any]] = {}
    baseline_dirs: set[Path] = set()
    if isinstance(snapshots, dict):
        for case_id, snapshot in snapshots.items():
            source = Path(snapshot["source_path"])
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"case source is unavailable or a symlink: {case_id}")
            source = source.resolve(strict=True)
            source_data = json.loads(source.read_text(encoding="utf-8"))
            baseline_source = Path(source_data["baseline"])
            if not baseline_source.is_absolute():
                baseline_source = source.parent / baseline_source
            if any(part.is_symlink() for part in (baseline_source, *baseline_source.parents)):
                raise ValueError(f"baseline path contains a symlink: {case_id}")
            case = load_case(source)
            baseline = case.baseline
            if _sha256(source) != snapshot["manifest_sha256"] or _fingerprint(baseline) != snapshot["baseline_hash"]:
                raise ValueError(f"case source changed: {case_id}")
            source_paths.add(source)
            baseline_dirs.add(baseline)
            directories = [baseline]
            for path in baseline.rglob("*"):
                if path.is_symlink():
                    raise ValueError(f"symlink in baseline cannot be archived faithfully: {path}")
                if path.is_dir():
                    directories.append(path)
                    baseline_dirs.add(path)
                elif path.is_file():
                    source_paths.add(path)
            inputs[case_id] = {"manifest": source, "baseline": baseline, "directories": directories}
    root = Path(os.path.commonpath(source_paths | baseline_dirs))
    members = {path.relative_to(root).as_posix(): path for path in source_paths}
    if any(not _safe_member(name) for name in members):
        raise ValueError("evidence paths cannot be represented safely in an archive")
    if len(members) > 100_000 or any(path.stat().st_size > _MAX_FILE for path in source_paths) or sum(
        path.stat().st_size for path in source_paths
    ) > _MAX_TOTAL:
        raise ValueError("experiment evidence exceeds bundle size limits")
    metadata = {
        "schema_version": 2 if inputs else 1,
        "anchor": anchor.relative_to(root).as_posix(),
        "experiment": experiment.relative_to(root).as_posix(),
        "files": {name: _sha256(path) for name, path in sorted(members.items())},
    }
    if inputs:
        metadata["inputs"] = {
            case_id: {
                "manifest": item["manifest"].relative_to(root).as_posix(),
                "baseline": item["baseline"].relative_to(root).as_posix(),
                "directories": sorted(path.relative_to(root).as_posix() for path in item["directories"]),
            }
            for case_id, item in inputs.items()
        }
    archive = Path(archive).resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
            for name, path in sorted(members.items()):
                bundle.write(path, name)
            bundle.writestr("bundle.json", json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"archive": str(archive), "sha256": _sha256(archive), "files": len(members),
            "experiment": str(experiment)}


def verify_bundle(archive: Path, trusted_anchor: Path | None = None) -> dict[str, Any]:
    """Verify a portable archive without following archive paths outside a temp dir."""
    archive = Path(archive).resolve()
    errors: list[dict[str, str]] = []
    file_count = 0
    independent = trusted_anchor is not None
    schema = None
    inputs = None
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if (len(names) != len(set(names)) or not names or len(names) > 100_001
                    or any(not _safe_member(name) or info.is_dir() or info.file_size > _MAX_FILE
                           for name, info in zip(names, infos))
                    or sum(info.file_size for info in infos) > _MAX_TOTAL):
                raise ValueError("unsafe archive paths or size")
            if "bundle.json" not in names or bundle.getinfo("bundle.json").file_size > 4 * 1024 * 1024:
                raise ValueError("missing or oversized bundle manifest")
            metadata = json.loads(bundle.read("bundle.json"))
            if not isinstance(metadata, dict):
                raise ValueError("invalid bundle manifest")
            files = metadata.get("files")
            schema = metadata.get("schema_version")
            if (schema not in (1, 2) or not isinstance(files, dict)
                    or set(files) != set(names) - {"bundle.json"}
                    or any(not isinstance(name, str) or not _safe_member(name)
                           or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                           for name, digest in files.items())
                    or metadata.get("anchor") not in files
                    or not _safe_member(str(metadata.get("experiment", "")))):
                raise ValueError("invalid bundle manifest")
            inputs = metadata.get("inputs")
            if schema == 2 and (not isinstance(inputs, dict) or not inputs):
                raise ValueError("invalid bundle inputs")
            if schema == 1 and inputs is not None:
                raise ValueError("unexpected bundle inputs")
            with tempfile.TemporaryDirectory(prefix="mini-dsh-bundle-") as directory:
                root = Path(directory)
                for name, digest in files.items():
                    target = root.joinpath(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(name) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    file_count += 1
                    if _sha256(target) != digest:
                        errors.append({"code": "bundle_file_hash_mismatch", "path": name})
                bundled_anchor = root.joinpath(*PurePosixPath(metadata["anchor"]).parts)
                if trusted_anchor is not None and _sha256(bundled_anchor) != _sha256(Path(trusted_anchor)):
                    errors.append({"code": "trusted_anchor_mismatch", "path": metadata["anchor"]})
                experiment = root.joinpath(*PurePosixPath(metadata["experiment"]).parts)
                report = verify_anchor(experiment, bundled_anchor, check_external_sources=False)
                if not report["ok"]:
                    errors.append({"code": "bundle_experiment_invalid", "path": metadata["experiment"]})
                if schema == 2:
                    snapshots = json.loads((experiment / "manifest.json").read_text(encoding="utf-8")).get(
                        "case_snapshots")
                    if not isinstance(snapshots, dict) or set(inputs) != set(snapshots):
                        raise ValueError("bundle inputs differ from experiment cases")
                    for case_id, item in inputs.items():
                        if not isinstance(item, dict):
                            raise ValueError("invalid bundle input entry")
                        manifest_name = item.get("manifest")
                        baseline_name = item.get("baseline")
                        directories = item.get("directories")
                        if (not isinstance(manifest_name, str) or manifest_name not in files
                                or not isinstance(baseline_name, str) or not _safe_member(baseline_name)
                                or not isinstance(directories, list) or baseline_name not in directories
                                or len(directories) != len(set(directories))
                                or any(not isinstance(name, str) or not _safe_member(name)
                                       or not (name == baseline_name or name.startswith(baseline_name + "/"))
                                       or name in files for name in directories)):
                            raise ValueError("invalid bundle input paths")
                        baseline = root.joinpath(*PurePosixPath(baseline_name).parts)
                        for name in directories:
                            root.joinpath(*PurePosixPath(name).parts).mkdir(parents=True, exist_ok=True)
                        manifest = root.joinpath(*PurePosixPath(manifest_name).parts)
                        snapshot = snapshots[case_id]
                        if _sha256(manifest) != snapshot["manifest_sha256"]:
                            errors.append({"code": "bundle_input_manifest_mismatch", "path": manifest_name})
                        if _fingerprint(baseline) != snapshot["baseline_hash"]:
                            errors.append({"code": "bundle_input_baseline_mismatch", "path": baseline_name})
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, EOFError,
            UnicodeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        errors.append({"code": "invalid_bundle", "path": f"{type(exc).__name__}: {exc}"})
    return {"ok": not errors, "archive": str(archive), "files_checked": file_count,
            "inputs_checked": len(inputs) if not errors and schema == 2 else 0,
            "independent_anchor_checked": independent, "errors": errors}
