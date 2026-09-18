from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from importlib import import_module
from typing import Any, Callable, Iterable, Protocol

from admet.core.run import RunJob, RunResult


class ParamKind(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    FLOAT = "float"
    TEXT = "text"
    CHOICE = "choice"
    PATH = "path"


@dataclass(frozen=True)
class ParamOption:
    value: Any
    label: str


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    kind: ParamKind
    default: Any = None
    description: str = ""
    required: bool = False
    advanced: bool = False
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[ParamOption, ...] = ()

    def validate(self, value: Any) -> Any:
        if value is None:
            if self.required:
                raise ValueError(f"{self.name} is required")
            return value

        if self.kind is ParamKind.BOOLEAN and not isinstance(value, bool):
            raise TypeError(f"{self.name} must be a boolean")
        if self.kind is ParamKind.INTEGER:
            if isinstance(value, bool):
                raise TypeError(f"{self.name} must be an integer")
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if not isinstance(value, int):
                raise TypeError(f"{self.name} must be an integer")
        if self.kind is ParamKind.FLOAT:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{self.name} must be a number")
        if self.kind in {ParamKind.TEXT, ParamKind.PATH} and not isinstance(value, str):
            raise TypeError(f"{self.name} must be text")
        if self.kind is ParamKind.CHOICE and self.options:
            allowed = {option.value for option in self.options}
            if value not in allowed:
                raise ValueError(f"{self.name} must be one of {sorted(allowed)!r}")

        if isinstance(value, int | float) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.name} must be >= {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.name} must be <= {self.maximum}")

        return value


@dataclass(frozen=True)
class ParamSchema:
    params: tuple[Param, ...] = field(default_factory=tuple)

    def defaults(self) -> dict[str, Any]:
        return {param.name: param.default for param in self.params}

    def validate(self, values: dict[str, Any]) -> dict[str, Any]:
        normalized = self.defaults()
        normalized.update(values)
        by_name = {param.name: param for param in self.params}
        unknown = set(normalized) - set(by_name)
        if unknown:
            raise KeyError(f"unknown settings: {sorted(unknown)!r}")
        return {name: by_name[name].validate(value) for name, value in normalized.items()}


@dataclass(frozen=True)
class ActionSpec:
    """What an engine can be asked to do.

    An engine declares the files an action writes, but never where they go: it is
    handed paths and writes to them. Core decides the location, because only core
    knows which project is open, and an engine that chose its own paths could
    write outside the session nobody would then find it in.
    """

    id: str
    label: str
    category: str
    params: tuple[str, ...] = ()
    destructive: bool = False
    description: str = ""
    # Logical names of the files this action writes, filled in by core.
    outputs: tuple[str, ...] = ()
    # The kind of artifact this action leaves behind, registered by core when it
    # completes. Empty means the action produces nothing worth recording.
    artifact: str = ""


def action_spec(actions: Iterable[ActionSpec], action_id: str) -> ActionSpec:
    for action in actions:
        if action.id == action_id:
            return action
    raise ValueError(f"unsupported action: {action_id}")


def validate_action_settings(
    schema: ParamSchema,
    actions: Iterable[ActionSpec],
    action_id: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    action = action_spec(actions, action_id)
    by_name = {param.name: param for param in schema.params}

    unknown_settings = set(settings) - set(by_name)
    if unknown_settings:
        raise KeyError(f"unknown settings: {sorted(unknown_settings)!r}")

    undeclared_settings = set(settings) - set(action.params)
    if undeclared_settings:
        raise KeyError(
            f"settings not declared by action {action.id!r}: {sorted(undeclared_settings)!r}"
        )

    missing_params = set(action.params) - set(by_name)
    if missing_params:
        raise KeyError(f"action {action.id!r} references unknown settings: {sorted(missing_params)!r}")

    defaults = schema.defaults()
    return {
        name: by_name[name].validate(settings.get(name, defaults[name]))
        for name in action.params
    }


class Engine(Protocol):
    id: str
    name: str
    settings: ParamSchema
    actions: tuple[ActionSpec, ...]

    def run(self, job: RunJob) -> RunResult:
        ...


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
