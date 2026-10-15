"""Process-group lifecycle and streamed output capture."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .contracts import SessionStore


@dataclass(frozen=True)
class ProcessCapture:
    exit_code: int
    output: str
    total_bytes: int
    artifact_id: str | None
    truncated: bool


def run_process(
    argv: Sequence[str],
    cwd: Path,
    timeout_s: float,
    output_limit: int,
    *,
    session: SessionStore | None = None,
    environment: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    on_timeout: Callable[[], None] | None = None,
    merge_stderr: bool = True,
) -> ProcessCapture:
    if not argv or timeout_s <= 0 or output_limit < 0:
        raise ValueError("invalid process arguments")
    with tempfile.TemporaryFile() as output_file, tempfile.TemporaryFile() as error_file:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(environment) if environment is not None else None,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=output_file,
            stderr=subprocess.STDOUT if merge_stderr else error_file,
            start_new_session=True,
        )
        try:
            process.communicate(input=input_bytes, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            if on_timeout is not None:
                on_timeout()
            raise
        output_file.seek(0, os.SEEK_END)
        total_bytes = output_file.tell()
        output_file.seek(0)
        excerpt = output_file.read(output_limit).decode("utf-8", errors="replace")
        truncated = total_bytes > output_limit
        artifact_id = None
        if truncated and session is not None:
            output_file.seek(0)
            artifact_id = session.save_artifact_stream(output_file)
        return ProcessCapture(process.returncode, excerpt, total_bytes, artifact_id, truncated)
