"""Optional Docker execution boundary for agent commands and held-out tests."""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import SessionStore, ToolResult
from .process import ProcessCapture, run_process


_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def resolve_image_id(image: str) -> str:
    """Resolve a local image once so all evaluation runs use identical bytes."""
    if not image or image.startswith("-"):
        raise ValueError("a Docker image name is required")
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, check=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot inspect local Docker image {image!r}: {exc}") from exc
    image_id = result.stdout.strip()
    if not _IMAGE_ID.fullmatch(image_id):
        raise RuntimeError(f"Docker returned an invalid image ID for {image!r}")
    return image_id


@dataclass(frozen=True)
class ReadOnlyMount:
    source: Path
    target: str


class DockerRunner:
    def __init__(self, image: str, session: SessionStore, output_limit: int = 8192):
        if not image or output_limit < 0:
            raise ValueError("Docker image and output limit are required")
        self.image = image
        self.session = session
        self.output_limit = output_limit

    def run(
        self,
        argv: Sequence[str],
        workspace: Path,
        timeout_s: float,
        extra_mounts: Sequence[ReadOnlyMount] = (),
    ) -> ProcessCapture:
        workspace = Path(workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace must be a directory")
        name = "mini-dsh-" + uuid.uuid4().hex
        command = [
            "docker", "run", "--rm", "--pull", "never", "--name", name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "1g",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--workdir", "/workspace",
            "--mount", _mount_arg(workspace, "/workspace", readonly=False),
        ]
        for mount in extra_mounts:
            if not mount.target.startswith("/") or mount.target == "/workspace" or mount.target.startswith("/workspace/"):
                raise ValueError("invalid extra mount target")
            command += ["--mount", _mount_arg(Path(mount.source).resolve(strict=True), mount.target, readonly=True)]
        command += [self.image, *argv]

        def cleanup() -> None:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)

        return run_process(command, workspace, timeout_s, self.output_limit, session=self.session, on_timeout=cleanup)


def _mount_arg(source: Path, target: str, readonly: bool) -> str:
    if "," in str(source) or "," in target:
        raise ValueError("mount path cannot contain comma")
    option = ",readonly" if readonly else ""
    return f"type=bind,src={source},dst={target}{option}"


def _as_result(capture: ProcessCapture) -> ToolResult:
    return ToolResult(
        "", "ok" if capture.exit_code == 0 and not capture.output_limit_exceeded else "error", capture.output,
        "output_limit_exceeded" if capture.output_limit_exceeded else None if capture.exit_code == 0 else "nonzero_exit", capture.exit_code,
        capture.artifact_id, capture.truncated,
    )


class DockerShellTool:
    name = "shell"
    is_external = True
    side_effect = "write"
    description = "Run an allowed argv command in the workspace Docker container. Shell syntax is not interpreted."
    schema = {
        "type": "object",
        "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
        "required": ["argv"],
    }

    def __init__(self, runner: DockerRunner, workspace: Path, allowed_commands: set[str]):
        self.runner = runner
        self.workspace = workspace
        self.allowed_commands = allowed_commands

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> ToolResult:
        argv = arguments["argv"]
        if not argv or argv[0] not in self.allowed_commands or "/" in argv[0]:
            raise ValueError("command is not allowed")
        return _as_result(self.runner.run(argv, self.workspace, timeout_s))


class DockerCommandTool:
    is_external = True
    side_effect = "write"
    description = "Run the preconfigured command in the workspace Docker container; no arguments are needed."
    schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, argv: Sequence[str], runner: DockerRunner, workspace: Path):
        if not name or not argv:
            raise ValueError("command name and argv are required")
        self.name = name
        self.argv = tuple(argv)
        self.runner = runner
        self.workspace = workspace

    def execute(self, arguments: Mapping[str, Any], timeout_s: float) -> ToolResult:
        return _as_result(self.runner.run(self.argv, self.workspace, timeout_s))
