from __future__ import annotations

from typing import Any


def structure_signature(*parts: Any) -> tuple[Any, ...]:
    return tuple(parts)


def structure_changed(current: tuple[Any, ...] | None, next_signature: tuple[Any, ...]) -> bool:
    return current != next_signature

