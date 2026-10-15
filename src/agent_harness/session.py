"""Append-only JSONL sessions and content-addressed tool artifacts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from io import BufferedIOBase
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContextView, Event


class JsonlSession:
    def __init__(
        self, root: Path, run_id: str | None = None, secrets: Sequence[str] = (),
        max_artifact_bytes: int = 512 * 1024 * 1024,
    ):
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self.run_id = run_id or uuid.uuid4().hex
        if not self.run_id.isalnum():
            raise ValueError("run_id must be alphanumeric")
        self.root = Path(root) / self.run_id
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        (self.root / "artifacts").mkdir(mode=0o700)
        self.max_artifact_bytes = max_artifact_bytes
        self._artifact_bytes = 0
        self._events: list[Event] = []
        self._secrets = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))
        self._secret_bytes = tuple(secret.encode("utf-8") for secret in self._secrets)
        self._start = time.monotonic()
        trace_fd = os.open(self.root / "trace.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._trace = os.fdopen(trace_fd, "w", encoding="utf-8")

    @classmethod
    def open_existing(
        cls, root: Path, run_id: str, secrets: Sequence[str] = (),
        max_artifact_bytes: int = 512 * 1024 * 1024,
    ) -> JsonlSession:
        if not run_id.isalnum():
            raise ValueError("run_id must be alphanumeric")
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        directory = Path(root) / run_id
        trace = directory / "trace.jsonl"
        raw = trace.read_bytes()
        if not raw or not raw.endswith(b"\n"):
            raise ValueError("trace is empty or has an incomplete final line")
        records = read_trace(trace)
        if any(item.get("run_id") != run_id or item.get("seq") != index
               for index, item in enumerate(records, 1)):
            raise ValueError("trace sequence or run ID is invalid")
        if any(item.get("type") == "terminal" for item in records):
            raise ValueError("cannot resume a terminal run")
        if any(item.get("type") == "evaluation_setup" for item in records):
            raise ValueError("evaluation sessions cannot be resumed with run resume")
        session = cls.__new__(cls)
        session.run_id = run_id
        session.root = directory
        session.max_artifact_bytes = max_artifact_bytes
        session._artifact_bytes = sum(path.stat().st_size for path in (directory / "artifacts").iterdir() if path.is_file())
        session._events = [Event(**item) for item in records]
        session._secrets = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))
        session._secret_bytes = tuple(secret.encode("utf-8") for secret in session._secrets)
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(records[0]["timestamp"])).total_seconds()
        session._start = time.monotonic() - max(0.0, elapsed)
        session._trace = trace.open("a", encoding="utf-8")
        return session

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def append(self, event_type: str, step: int, payload: Mapping[str, Any]) -> Event:
        event = Event(
            run_id=self.run_id,
            seq=len(self._events) + 1,
            type=event_type,
            step=step,
            payload=self._redact(dict(payload)),
            timestamp=datetime.now(timezone.utc).isoformat(),
            monotonic_ms=int((time.monotonic() - self._start) * 1000),
        )
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        self._trace.write(line + "\n")
        self._trace.flush()
        self._events.append(event)
        return event

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, Mapping):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        return value

    def redact_context(self, view: ContextView) -> ContextView:
        if not self._secrets:
            return view
        return ContextView(
            tuple(self._redact(message) for message in view.messages),
            view.event_ids,
            view.strategy,
            self._redact(view.metadata),
        )

    def redact_text(self, value: str) -> str:
        return self._redact(value)

    def _redacted_chunks(self, stream: BufferedIOBase):
        if not self._secret_bytes:
            while chunk := stream.read(1024 * 1024):
                yield chunk
            return
        carry = b""
        longest = max(map(len, self._secret_bytes))
        while chunk := stream.read(1024 * 1024):
            data = carry + chunk
            safe_end = max(0, len(data) - longest + 1)
            output = bytearray()
            index = 0
            while index < safe_end:
                match = next((secret for secret in self._secret_bytes if data.startswith(secret, index)), None)
                if match is not None:
                    output.extend(b"[REDACTED]")
                    index += len(match)
                else:
                    output.append(data[index])
                    index += 1
            carry = data[index:]
            if output:
                yield bytes(output)
        if carry:
            output = carry
            for secret in self._secret_bytes:
                output = output.replace(secret, b"[REDACTED]")
            yield output

    def save_artifact(self, content: bytes) -> str:
        return self.save_artifact_stream(io.BytesIO(content))

    def save_artifact_stream(self, stream: BufferedIOBase) -> str:
        digest = hashlib.sha256()
        artifacts = self.root / "artifacts"
        with tempfile.NamedTemporaryFile(dir=artifacts, delete=False) as target:
            temporary = Path(target.name)
            try:
                for chunk in self._redacted_chunks(stream):
                    if self._artifact_bytes + target.tell() + len(chunk) > self.max_artifact_bytes:
                        raise ValueError("session artifact storage quota exceeded")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        destination = artifacts / digest.hexdigest()
        if destination.exists():
            temporary.unlink()
        else:
            os.replace(temporary, destination)
            self._artifact_bytes += destination.stat().st_size
        return digest.hexdigest()

    def close(self) -> None:
        self._trace.close()

    def __enter__(self) -> JsonlSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def read_trace(path: Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]
