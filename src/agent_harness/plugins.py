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
        for plugin in self.ordered():
            try:
                plugin.start()
                self._started.append(plugin)
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        while self._started:
            try:
                self._started.pop().close()
            except Exception:
                pass
