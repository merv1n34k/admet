from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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
