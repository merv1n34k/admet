from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Callable

from .base import Engine


EngineFactory = Callable[[], Engine]


@dataclass(frozen=True)
class LazyEngineSpec:
    engine_id: str
    module: str
    factory: str = "create_engine"


class EngineRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, EngineFactory] = {}
        self._lazy: dict[str, LazyEngineSpec] = {}
        self._unavailable: dict[str, str] = {}

    def register(self, engine_id: str, factory: EngineFactory) -> None:
        self._factories[engine_id] = factory
        self._lazy.pop(engine_id, None)
        self._unavailable.pop(engine_id, None)

    def register_lazy(self, spec: LazyEngineSpec) -> None:
        if spec.engine_id not in self._factories:
            self._lazy[spec.engine_id] = spec

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted({*self._factories, *self._lazy}))

    def available_ids(self) -> tuple[str, ...]:
        available = set(self._factories)
        for engine_id in tuple(self._lazy):
            if self._load_lazy(engine_id) is not None:
                available.add(engine_id)
        return tuple(sorted(available))

    def unavailable(self) -> dict[str, str]:
        for engine_id in tuple(self._lazy):
            self._load_lazy(engine_id)
        return dict(self._unavailable)

    def create(self, engine_id: str) -> Engine:
        factory = self._factories.get(engine_id)
        if factory is None:
            factory = self._load_lazy(engine_id)
        if factory is None:
            reason = self._unavailable.get(engine_id, "engine is not registered")
            raise LookupError(f"{engine_id!r} is unavailable: {reason}")
        return factory()

    def _load_lazy(self, engine_id: str) -> EngineFactory | None:
        spec = self._lazy.get(engine_id)
        if spec is None:
            return self._factories.get(engine_id)
        try:
            module = import_module(spec.module)
            factory = getattr(module, spec.factory)
        except Exception as exc:
            self._unavailable[engine_id] = str(exc)
            return None
        self.register(engine_id, factory)
        return factory

