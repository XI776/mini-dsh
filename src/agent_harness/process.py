"""Process-group lifecycle and streamed output capture."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
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
    output_limit_exceeded: bool = False


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
    max_capture_bytes: int = 16 * 1024 * 1024,
) -> ProcessCapture:
    if not argv or timeout_s <= 0 or output_limit < 0 or max_capture_bytes < 1:
        raise ValueError("invalid process arguments")
    with tempfile.TemporaryFile() as output_file, tempfile.TemporaryFile() as error_file:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(environment) if environment is not None else None,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            start_new_session=True,
        )
        captured_bytes = 0
        output_exceeded = threading.Event()
        lock = threading.Lock()

        def stop_group() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def collect(source, destination) -> None:
            nonlocal captured_bytes
            try:
                while chunk := source.read(64 * 1024):
                    with lock:
                        room = max(0, max_capture_bytes - captured_bytes)
                        destination.write(chunk[:room])
                        captured_bytes += min(len(chunk), room)
                        if len(chunk) > room:
                            output_exceeded.set()
                            stop_group()
            finally:
                source.close()

        readers = [threading.Thread(target=collect, args=(process.stdout, output_file), daemon=True)]
        if not merge_stderr:
            readers.append(threading.Thread(target=collect, args=(process.stderr, error_file), daemon=True))
        for reader in readers:
            reader.start()

        def write_input() -> None:
            if process.stdin is not None:
                try:
                    process.stdin.write(input_bytes or b"")
                    process.stdin.flush()
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()

        writer = None
        if input_bytes is not None:
            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            stop_group()
            process.wait()
            if on_timeout is not None:
                on_timeout()
            raise
        finally:
            for reader in readers:
                reader.join(timeout=1)
            if any(reader.is_alive() for reader in readers):
                stop_group()
                for reader in readers:
                    reader.join(timeout=1)
            if writer is not None:
                writer.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            raise OSError("process output pipe remained open after group cleanup")
        if output_exceeded.is_set() and on_timeout is not None:
            on_timeout()
        output_file.seek(0, os.SEEK_END)
        total_bytes = output_file.tell()
        output_file.seek(0)
        excerpt = output_file.read(output_limit).decode("utf-8", errors="replace")
        truncated = total_bytes > output_limit or output_exceeded.is_set()
        artifact_id = None
        if truncated and session is not None:
            output_file.seek(0)
            artifact_id = session.save_artifact_stream(output_file)
        return ProcessCapture(process.returncode, excerpt, total_bytes, artifact_id, truncated, output_exceeded.is_set())
