"""Dependency-checked plugin startup with rollback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class Plugin(Protocol):
    name: str
    requires: tuple[str, ...]

    def start(self) -> None: ...

    def close(self) -> None: ...


@dataclass
class PluginGroup:
    plugins: Sequence[Plugin]

    def __post_init__(self) -> None:
        self._started: list[Plugin] = []
        self._active = False

    def ordered(self) -> list[Plugin]:
        by_name = {plugin.name: plugin for plugin in self.plugins}
        if len(by_name) != len(self.plugins):
            raise ValueError("duplicate plugin name")
        result: list[Plugin] = []
        active: set[str] = set()
        done: set[str] = set()

        def visit(name: str) -> None:
            if name not in by_name:
                raise ValueError(f"missing plugin dependency: {name}")
            if name in active:
                raise ValueError(f"plugin dependency cycle at {name}")
            if name in done:
                return
            active.add(name)
            for dependency in by_name[name].requires:
                visit(dependency)
            active.remove(name)
            done.add(name)
            result.append(by_name[name])

        for name in by_name:
            visit(name)
        return result

    def start(self) -> None:
        if self._active or self._started:
            raise RuntimeError("plugin group is already starting or running")
        for plugin in self.ordered():
            # start() may allocate resources before it raises. Include that
            # plugin in rollback as well as its successfully started peers.
            self._started.append(plugin)
            try:
                plugin.start()
            except BaseException as exc:
                try:
                    self.close()
                except Exception as cleanup_error:
                    exc.add_note(f"plugin rollback also failed: {cleanup_error}")
                raise
        self._active = True

    def close(self) -> None:
        errors: list[Exception] = []
        while self._started:
            try:
                self._started.pop().close()
            except Exception as exc:
                errors.append(exc)
        self._active = False
        if errors:
            raise ExceptionGroup("plugin cleanup failed", errors)
