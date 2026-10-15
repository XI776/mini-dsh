"""Replaceable context views over immutable session events."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Sequence

from .contracts import ContextView, Event


def _history(events: Sequence[Event]) -> list[tuple[int, dict[str, str]]]:
    history: list[tuple[int, dict[str, str]]] = []
    for event in events:
        if event.type == "model_response":
            message = event.payload.get("message", "")
            calls = event.payload.get("tool_calls", [])
            if calls:
                call_text = "\n".join(
                    f"Called {call.get('name', '')}({call.get('arguments', {})})"
                    for call in calls if isinstance(call, dict)
                )
                message = f"{message}\n{call_text}".strip()
            if message:
                history.append((event.seq, {"role": "assistant", "content": str(message)}))
        elif event.type == "tool_result":
            history.append(
                (
                    event.seq,
                    {
                        "role": "tool",
                        "content": f"{event.payload.get('name')}: {event.payload.get('status')}: "
                        f"{event.payload.get('output', '')}",
                    },
                )
            )
    return history


def _fit(
    task: str, entries: list[tuple[int, dict[str, str]]], budget: int | None,
) -> tuple[tuple[dict[str, str], ...], tuple[int, ...], dict[str, Any]]:
    base = {"role": "user", "content": task}
    original_ids = [seq for seq, _ in entries if seq > 0]
    truncated_id = None
    if budget is not None:
        # A conservative local estimate, replaced by provider tokenization when available.
        allowed_chars = max(0, budget * 3 - len(task))
        kept: list[tuple[int, dict[str, str]]] = []
        for entry in reversed(entries):
            length = len(entry[1]["content"])
            if length <= allowed_chars:
                kept.append(entry)
                allowed_chars -= length
            elif allowed_chars > 16:
                clipped = {**entry[1], "content": "[truncated]\n" + entry[1]["content"][-(allowed_chars - 12) :]}
                kept.append((entry[0], clipped))
                truncated_id = entry[0] if entry[0] > 0 else None
                break
            else:
                break
        entries = list(reversed(kept))
    ids = tuple(entry[0] for entry in entries if entry[0] > 0)
    selection = {
        "retained_event_ids": list(ids),
        "dropped_event_ids": [seq for seq in original_ids if seq not in ids],
        "truncated_event_id": truncated_id,
        "reason": "token_budget" if truncated_id is not None or len(ids) < len(original_ids) else None,
    }
    return (base, *(entry[1] for entry in entries)), ids, selection


def _workspace_mutations(events: Sequence[Event], after_seq: int) -> list[int]:
    """Find tool executions that may have changed the indexed workspace."""
    effects: dict[str, str] = {}
    for event in events:
        if event.type == "run_started":
            for schema in event.payload.get("tools", []):
                if isinstance(schema, dict) and isinstance(schema.get("name"), str):
                    effects[schema["name"]] = schema.get("side_effect", "write")
    mutations = []
    for event in events:
        if event.seq <= after_seq or event.type != "tool_result":
            continue
        name = str(event.payload.get("name", ""))
        effect = effects.get(name, "read" if name in {"read_file", "git"} else "write")
        # A failed process may have written files before returning an error or timing out.
        if effect == "write" and event.payload.get("error_code") not in {
            "permission_denied", "invalid_arguments", "unknown_tool"
        }:
            mutations.append(event.seq)
    return mutations


def _workspace_files(root: Path, suffix: str | None = None) -> list[tuple[str, Path, tuple[int, int, int, int]]]:
    """Inventory visible regular files without traversing hidden or linked directories."""
    files = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(
            name for name in dirs
            if not name.startswith(".") and name != "__pycache__" and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(names):
            if name.startswith(".") or (suffix is not None and not name.endswith(suffix)):
                continue
            path = Path(directory) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            relative = path.relative_to(root).as_posix()
            files.append((relative, path, (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)))
    return files


def _refresh_index(index: Any, events: Sequence[Event]) -> dict[str, Any] | None:
    previous_run = getattr(index, "_last_run_id", None)
    run_id = events[-1].run_id if events else previous_run
    last_seq = events[-1].seq if events else index._last_seen_seq
    after_seq = index._last_seen_seq if previous_run == run_id else 0
    mutation_ids = _workspace_mutations(events, after_seq)
    files = index._scan_files()
    fingerprint = tuple((relative, stats) for relative, _, stats in files)
    workspace_changed = fingerprint != index._file_fingerprint
    new_run = previous_run is not None and run_id is not None and previous_run != run_id
    if not mutation_ids and not workspace_changed and not new_run:
        index._last_run_id = run_id
        index._last_seen_seq = last_seq
        return None
    previous_snapshot_id = index.snapshot_id
    index.rebuild(files)
    index._last_run_id = run_id
    index._last_seen_seq = last_seq
    return {
        "reason": "tool_result" if mutation_ids else "new_run" if new_run else "workspace_changed",
        "event_ids": mutation_ids,
        "previous_snapshot_id": previous_snapshot_id,
        "changed": previous_snapshot_id != index.snapshot_id,
    }


class FullHistory:
    name = "full_history"

    def configuration(self) -> dict[str, int]:
        return {}

    def build(self, events: Sequence[Event], task: str, token_budget: int | None) -> ContextView:
        messages, ids, selection = _fit(task, _history(events), token_budget)
        return ContextView(messages, ids, self.name, {"selection": selection})


class SlidingWindow:
    name = "sliding_window"

    def __init__(self, recent_events: int = 8):
        if recent_events < 1:
            raise ValueError("recent_events must be positive")
        self.recent_events = recent_events

    def configuration(self) -> dict[str, int]:
        return {"recent_events": self.recent_events}

    def build(self, events: Sequence[Event], task: str, token_budget: int | None) -> ContextView:
        history = _history(events)
        messages, ids, selection = _fit(task, history[-self.recent_events :], token_budget)
        dropped = history[:-self.recent_events]
        return ContextView(messages, ids, self.name, {
            "recent_events": self.recent_events, "selection": selection,
            "window_dropped_range": [dropped[0][0], dropped[-1][0]] if dropped else None,
        })


class SummaryContext:
    """Deterministic extractive summary for the first implementation."""

    name = "summary"

    def __init__(self, recent_events: int = 6, summary_chars: int = 1200):
        if recent_events < 1 or summary_chars < 1:
            raise ValueError("summary settings must be positive")
        self.recent_events = recent_events
        self.summary_chars = summary_chars

    def configuration(self) -> dict[str, int]:
        return {"recent_events": self.recent_events, "summary_chars": self.summary_chars}

    def build(self, events: Sequence[Event], task: str, token_budget: int | None) -> ContextView:
        history = _history(events)
        older = history[: -self.recent_events]
        recent = history[-self.recent_events :]
        summary = "\n".join(f"[{seq}] {message['content'][:240]}" for seq, message in older)
        if len(summary) > self.summary_chars:
            summary = summary[: self.summary_chars] + "…"
        entries = ([(older[-1][0], {"role": "system", "content": "Earlier events summary:\n" + summary})] if older else []) + recent
        messages, ids, selection = _fit(task, entries, token_budget)
        return ContextView(
            messages,
            ids,
            self.name,
            {"summarized_range": [older[0][0], older[-1][0]] if older else None,
             "reason": "event_window", "selection": selection},
        )


class RepositoryIndex:
    def __init__(self, root: Path, max_file_bytes: int = 100_000):
        self.root = Path(root).resolve(strict=True)
        self.max_file_bytes = max_file_bytes
        self._last_seen_seq = 0
        self._last_run_id: str | None = None
        self.rebuild()

    def _scan_files(self) -> list[tuple[str, Path, tuple[int, int, int, int]]]:
        return _workspace_files(self.root)

    def rebuild(self, files: list[tuple[str, Path, tuple[int, int, int, int]]] | None = None) -> None:
        files = self._scan_files() if files is None else files
        documents: list[tuple[str, int, str]] = []
        digest = hashlib.sha256()
        for relative, path, _ in files:
            try:
                if path.stat().st_size > self.max_file_bytes:
                    continue
                data = path.read_bytes()
                content = data.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            digest.update(relative.encode() + b"\0" + data)
            for line_no, line in enumerate(content.splitlines(), 1):
                if line.strip():
                    documents.append((relative, line_no, line[:500]))
        self.documents = documents
        self.snapshot_id = digest.hexdigest()
        self._file_fingerprint = tuple((relative, stats) for relative, _, stats in files)

    def search(self, query: str, limit: int = 8) -> list[tuple[str, int, str]]:
        terms = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", query.lower()))
        if not terms:
            return []
        scored = []
        for path, line_no, line in self.documents:
            haystack = (path + " " + line).lower()
            score = sum(term in haystack for term in terms)
            if score:
                scored.append((-score, path, line_no, line))
        scored.sort()
        return [(path, line_no, line) for _, path, line_no, line in scored[:limit]]


class RepositoryContext:
    name = "repository"

    def __init__(self, index: RepositoryIndex, recent_events: int = 6):
        self.index = index
        self.recent_events = recent_events

    def configuration(self) -> dict[str, int]:
        return {"recent_events": self.recent_events, "max_file_bytes": self.index.max_file_bytes}

    def build(self, events: Sequence[Event], task: str, token_budget: int | None) -> ContextView:
        refresh = _refresh_index(self.index, events)
        history = _history(events)
        query = task + " " + " ".join(message["content"] for _, message in history[-2:])
        hits = self.index.search(query)
        snippets = "\n".join(f"{path}:{line_no}: {line}" for path, line_no, line in hits)
        entries = ([(0, {"role": "system", "content": "Repository matches:\n" + snippets})] if hits else []) + history[-self.recent_events :]
        messages, ids, selection = _fit(task, entries, token_budget)
        dropped = history[:-self.recent_events]
        metadata: dict[str, Any] = {
            "snapshot_id": self.index.snapshot_id, "hits": [f"{p}:{n}" for p, n, _ in hits],
            "selection": selection,
            "window_dropped_range": [dropped[0][0], dropped[-1][0]] if dropped else None,
        }
        if refresh:
            metadata["index_refresh"] = refresh
        return ContextView(messages, ids, self.name, metadata)


class RetrievalSummaryContext:
    name = "retrieval_summary"

    def __init__(self, index: RepositoryIndex, recent_events: int = 6):
        self.retrieval = RepositoryContext(index, recent_events)
        self.summary = SummaryContext(recent_events)

    def configuration(self) -> dict[str, int]:
        return {**self.retrieval.configuration(), "summary_chars": self.summary.summary_chars}

    def build(self, events: Sequence[Event], task: str, token_budget: int | None) -> ContextView:
        summary_view = self.summary.build(events, task, None)
        retrieval_view = self.retrieval.build(events, task, None)
        extras = [(0, dict(message)) for message in summary_view.messages[1:-self.summary.recent_events]]
        extras += [(0, dict(message)) for message in retrieval_view.messages[1:] if message["role"] == "system"]
        extras += _history(events)[-self.summary.recent_events :]
        messages, ids, selection = _fit(task, extras, token_budget)
        return ContextView(messages, ids, self.name, {**summary_view.metadata, **retrieval_view.metadata, "selection": selection})


class PythonSymbolContext:
    name = "python_symbols"

    def __init__(self, root: Path, recent_events: int = 6):
        self.root = Path(root).resolve(strict=True)
        self.recent_events = recent_events
        self._last_seen_seq = 0
        self._last_run_id: str | None = None
        self.rebuild()

    def configuration(self) -> dict[str, int]:
        return {"recent_events": self.recent_events}

    def _scan_files(self) -> list[tuple[str, Path, tuple[int, int, int, int]]]:
        return _workspace_files(self.root, ".py")

    def rebuild(self, files: list[tuple[str, Path, tuple[int, int, int, int]]] | None = None) -> None:
        files = self._scan_files() if files is None else files
        symbols: list[tuple[str, str, int]] = []
        digest = hashlib.sha256()
        for relative, path, _ in files:
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            digest.update(relative.encode() + b"\0" + source.encode())
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append((node.name, relative, node.lineno))
        self.symbols = symbols
        self.snapshot_id = digest.hexdigest()
        self._file_fingerprint = tuple((relative, stats) for relative, _, stats in files)

    def build(self, events: Sequence[Event], task: str, token_budget: int | None) -> ContextView:
        refresh = _refresh_index(self, events)
        terms = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", task.lower()))
        hits = [(name, path, line) for name, path, line in self.symbols if name.lower() in terms][:12]
        entries = ([(0, {"role": "system", "content": "Python symbols:\n" + "\n".join(f"{p}:{n}: {s}" for s, p, n in hits)})] if hits else [])
        entries += _history(events)[-self.recent_events :]
        messages, ids, selection = _fit(task, entries, token_budget)
        history = _history(events)
        dropped = history[:-self.recent_events]
        metadata: dict[str, Any] = {
            "snapshot_id": self.snapshot_id, "hits": [f"{p}:{n}" for _, p, n in hits],
            "selection": selection,
            "window_dropped_range": [dropped[0][0], dropped[-1][0]] if dropped else None,
        }
        if refresh:
            metadata["index_refresh"] = refresh
        return ContextView(messages, ids, self.name, metadata)
