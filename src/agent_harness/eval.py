"""Isolated task evaluation with post-run public and held-out tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from .agent import AgentLoop, BudgetLimits, RunResult
from .contracts import ContextStrategy, Model
from .docker_runtime import DockerCommandTool, DockerRunner, DockerShellTool, ReadOnlyMount
from .process import ProcessCapture
from .runtime import GitTool, PatchFileTool, ReadFileTool, ToolRuntime, WorkspaceFiles, WorkspacePolicy, WriteFileTool
from .session import JsonlSession


EVALUATION_VERSION = 2


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    task: str
    baseline: Path
    image: str
    public_test: tuple[str, ...]
    holdout_test: tuple[str, ...] = ()
    holdout_files: Mapping[str, str] | None = None
    allowed_commands: tuple[str, ...] = ()
    build_command: tuple[str, ...] = ()
    limits: BudgetLimits = BudgetLimits()
    test_timeout_s: float = 60.0

    def __post_init__(self) -> None:
        if not self.case_id or not self.task or not self.image or not self.public_test:
            raise ValueError("case id, task, image and public test are required")
        for command in (self.public_test, self.holdout_test, self.build_command):
            if not all(isinstance(part, str) and part for part in command):
                raise ValueError("test and build commands must contain nonempty strings")
        if self.holdout_files is not None and not all(
            isinstance(name, str) and isinstance(content, str) for name, content in self.holdout_files.items()
        ):
            raise ValueError("holdout files must map paths to text")
        if bool(self.holdout_test) != bool(self.holdout_files):
            raise ValueError("holdout test and files must be provided together")
        if self.test_timeout_s <= 0:
            raise ValueError("test timeout must be positive")


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    run: RunResult
    success: bool
    public_exit_code: int
    holdout_exit_code: int | None
    trace: Path
    baseline_hash: str


def load_case(path: Path) -> EvaluationCase:
    path = Path(path).resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported evaluation case schema")
    for key in ("public_test", "holdout_test", "allowed_commands", "build_command"):
        if key in data and (not isinstance(data[key], list) or not all(isinstance(part, str) for part in data[key])):
            raise ValueError(f"{key} must be a JSON array of strings")
    if "holdout_files" in data and not isinstance(data["holdout_files"], dict):
        raise ValueError("holdout_files must be a JSON object")
    baseline = Path(data["baseline"])
    if not baseline.is_absolute():
        baseline = path.parent / baseline
    limits = BudgetLimits(**data.get("limits", {}))
    return EvaluationCase(
        case_id=data["case_id"],
        task=data["task"],
        baseline=baseline.resolve(strict=True),
        image=data["image"],
        public_test=tuple(data["public_test"]),
        holdout_test=tuple(data.get("holdout_test", [])),
        holdout_files=data.get("holdout_files"),
        allowed_commands=tuple(data.get("allowed_commands", [])),
        build_command=tuple(data.get("build_command", [])),
        limits=limits,
        test_timeout_s=float(data.get("test_timeout_s", 60.0)),
    )


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"directory\0")
    return digest.hexdigest()


def _write_holdout(root: Path, files: Mapping[str, str]) -> None:
    for name, content in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or not relative.parts or any(part in ("..", ".") for part in relative.parts):
            raise ValueError(f"invalid holdout path: {name}")
        target = root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _run_test(runner: DockerRunner, argv: tuple[str, ...], workspace: Path, timeout_s: float, mounts=()) -> ProcessCapture:
    try:
        return runner.run(argv, workspace, timeout_s, extra_mounts=mounts)
    except subprocess.TimeoutExpired:
        return ProcessCapture(124, "test timed out", len("test timed out"), None, False)
    except OSError as exc:
        message = f"test infrastructure error: {type(exc).__name__}: {exc}"
        return ProcessCapture(125, message, len(message.encode()), None, False)


def evaluate(
    case: EvaluationCase,
    model: Model,
    context_factory: Callable[[Path], ContextStrategy],
    traces_root: Path,
) -> EvaluationResult:
    if case.holdout_files:
        hidden_content = {content.encode("utf-8") for content in case.holdout_files.values() if content}
        for path in case.baseline.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.read_bytes() in hidden_content:
                raise ValueError(f"holdout test content is present in baseline: {path}")
    with tempfile.TemporaryDirectory(prefix="mini-dsh-eval-") as directory:
        temporary = Path(directory)
        workspace = temporary / "workspace"
        shutil.copytree(case.baseline, workspace, symlinks=True)
        baseline_hash = _fingerprint(workspace)
        with JsonlSession(traces_root, secrets=getattr(model, "trace_secrets", ())) as session:
            session.append("evaluation_setup", 0, {
                "case_id": case.case_id, "baseline_hash": baseline_hash,
                "evaluation_version": EVALUATION_VERSION,
                "image": case.image, "public_test": list(case.public_test),
                "holdout_test": list(case.holdout_test),
                "allowed_commands": list(case.allowed_commands),
                "build_command": list(case.build_command),
                "test_timeout_s": case.test_timeout_s,
                "holdout_hashes": {
                    name: hashlib.sha256(content.encode()).hexdigest()
                    for name, content in (case.holdout_files or {}).items()
                },
            })
            runner = DockerRunner(case.image, session)
            files = WorkspaceFiles(workspace)
            tools = [
                ReadFileTool(files), WriteFileTool(files), PatchFileTool(files),
                DockerCommandTool("run_tests", case.public_test, runner, workspace),
            ]
            if (workspace / ".git").exists():
                tools.append(GitTool(workspace, session))
            if case.allowed_commands:
                tools.append(DockerShellTool(runner, workspace, set(case.allowed_commands)))
            if case.build_command:
                tools.append(DockerCommandTool("build", case.build_command, runner, workspace))
            runtime = ToolRuntime(tools, WorkspacePolicy(allow_writes=True), session)
            run = AgentLoop(model, context_factory(workspace), runtime, session, case.limits).run(case.task)

            public = _run_test(runner, case.public_test, workspace, case.test_timeout_s)
            session.append("public_test_result", run.usage.steps, asdict(public))
            holdout_exit = None
            if case.holdout_test:
                holdout_dir = temporary / "holdout"
                holdout_dir.mkdir()
                _write_holdout(holdout_dir, case.holdout_files or {})
                held = _run_test(
                    runner, case.holdout_test, workspace, case.test_timeout_s,
                    mounts=[ReadOnlyMount(holdout_dir, "/holdout")],
                )
                holdout_exit = held.exit_code
                session.append("holdout_test_result", run.usage.steps, asdict(held))
            success = public.exit_code == 0 and (holdout_exit is None or holdout_exit == 0)
            session.append("evaluation_result", run.usage.steps, {
                "case_id": case.case_id, "success": success,
                "agent_status": run.status.value,
                "public_exit_code": public.exit_code,
                "holdout_exit_code": holdout_exit,
            })
            return EvaluationResult(
                case.case_id, run, success, public.exit_code, holdout_exit,
                session.root / "trace.jsonl", baseline_hash,
            )
