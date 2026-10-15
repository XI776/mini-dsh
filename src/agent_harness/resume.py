"""Conservative workspace checkpoints for local AgentLoop recovery."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def workspace_digest(root: Path, excluded: Path | None = None, max_files: int = 10_000,
                     max_bytes: int = 512_000_000) -> str:
    """Hash names, types and bytes; reject workspaces too large to check safely."""
    root = Path(root).resolve(strict=True)
    excluded = Path(excluded).resolve() if excluded is not None else None
    if excluded is not None and (excluded == root or excluded in root.parents):
        raise ValueError("trace directory must not cover workspace")
    digest = hashlib.sha256()
    count = total = 0
    def on_walk_error(error: OSError) -> None:
        raise error

    for directory, children, files in os.walk(root, topdown=True, followlinks=False, onerror=on_walk_error):
        parent = Path(directory)
        visible_dirs = []
        symlink_dirs = []
        for name in sorted(children):
            path = parent / name
            if excluded is not None and (path == excluded or excluded in path.parents):
                continue
            (symlink_dirs if path.is_symlink() else visible_dirs).append(name)
        children[:] = visible_dirs
        for name in sorted((*children, *symlink_dirs, *files)):
            path = parent / name
            if excluded is not None and (path == excluded or excluded in path.parents):
                continue
            count += 1
            if count > max_files:
                raise ValueError("workspace checkpoint file limit exceeded")
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0" + os.readlink(path).encode() + b"\0")
            elif path.is_dir():
                digest.update(b"directory\0")
            elif path.is_file():
                digest.update(b"file\0")
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("workspace checkpoint byte limit exceeded")
                        digest.update(chunk)
            else:
                raise ValueError(f"unsupported workspace entry: {path}")
    return digest.hexdigest()
