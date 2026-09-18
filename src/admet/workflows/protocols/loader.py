"""Finding protocol documents and reading them safely.

Reading is deliberately dumb: it turns bytes into plain Python data and says
where they came from. It does not interpret them -- that is the validator's job,
and keeping the two apart means a malformed document fails with a readable
message instead of a stack trace from the middle of a parser.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from admet.workflows.protocols.model import ProtocolError

PROTOCOL_SUFFIXES = (".yaml", ".yml")

# Where protocols are looked for, nearest first. A project may override a user
# protocol, and a user protocol may override a built-in, so an operator can fix
# a shipped protocol for one experiment without editing the installation.
BUILTIN_DIR = Path(__file__).with_name("builtin")
ENV_PROTOCOL_PATH = "ADMET_PROTOCOL_PATH"


@dataclass(frozen=True)
class ProtocolSource:
    """One protocol document as it was read, before it is understood."""

    path: Path
    raw: dict[str, Any]
    content_hash: str
    origin: str  # "project", "user" or "builtin"

    @property
    def declared_id(self) -> str:
        value = self.raw.get("id")
        return value if isinstance(value, str) else ""


class SafeProtocolLoader(yaml.SafeLoader):
    """YAML with nothing in it but numbers, strings, booleans and containers.

    SafeLoader already refuses to construct arbitrary Python objects. Three of
    YAML 1.1's implicit conversions are dropped on top of that, because each one
    turns something an operator wrote into something else:

    - timestamps, so an unquoted date stays text rather than becoming a datetime
      no part of this system knows what to do with;
    - sexagesimal numbers, so `12:30` is the string it looks like and not 750;
    - `.inf` and `.nan`, which are not values a setpoint can take. Left as
      strings, they fail type checking with a message naming the field, instead
      of reaching the hardware as a number that is not one.
    """


_DROPPED_TAGS = (
    "tag:yaml.org,2002:timestamp",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
)
SafeProtocolLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in _DROPPED_TAGS]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
SafeProtocolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^[-+]?[0-9][0-9_]*$"),
    list("-+0123456789"),
)
SafeProtocolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(
        r"""^[-+]?(
            [0-9][0-9_]*\.[0-9_]*(?:[eE][-+]?[0-9]+)?
          | \.[0-9][0-9_]*(?:[eE][-+]?[0-9]+)?
          | [0-9][0-9_]*[eE][-+]?[0-9]+
        )$""",
        re.VERBOSE,
    ),
    list("-+.0123456789"),
)


def read_protocol(path: str | Path, *, origin: str = "user") -> ProtocolSource:
    """Read one document. Raises ProtocolError with the path on any failure."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProtocolError(f"{path}: cannot be read ({exc.strerror or exc})") from exc

    try:
        raw = yaml.load(text, Loader=SafeProtocolLoader)
    except yaml.YAMLError as exc:
        raise ProtocolError(f"{path}: is not valid YAML ({_yaml_reason(exc)})") from exc

    if raw is None:
        raise ProtocolError(f"{path}: is empty")
    if not isinstance(raw, dict):
        raise ProtocolError(f"{path}: must be a mapping, found {type(raw).__name__}")

    return ProtocolSource(
        path=path,
        raw=raw,
        content_hash=content_hash(text),
        origin=origin,
    )


def content_hash(text: str) -> str:
    """A protocol's identity as content, independent of where it is stored."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def search_path(
    *,
    project_dir: str | Path | None = None,
    user_dirs: Iterable[str | Path] = (),
) -> tuple[tuple[Path, str], ...]:
    """Directories to search, nearest first, each with where it came from."""
    entries: list[tuple[Path, str]] = []
    if project_dir is not None:
        entries.append((Path(project_dir) / "protocols", "project"))
    for directory in user_dirs:
        entries.append((Path(directory), "user"))
    for directory in _env_dirs():
        entries.append((directory, "user"))
    entries.append((BUILTIN_DIR, "builtin"))
    return tuple(entries)


def discover(
    *,
    project_dir: str | Path | None = None,
    user_dirs: Iterable[str | Path] = (),
) -> tuple[ProtocolSource, ...]:
    """Every readable protocol on the search path, nearest definition winning.

    A document that cannot be read does not stop discovery: the rest are still
    offered, and the broken one is reported when it is asked for by id.
    """
    found: dict[str, ProtocolSource] = {}
    for directory, origin in search_path(project_dir=project_dir, user_dirs=user_dirs):
        for path in _documents_in(directory):
            try:
                source = read_protocol(path, origin=origin)
            except ProtocolError:
                continue
            key = source.declared_id or str(path)
            found.setdefault(key, source)
    return tuple(sorted(found.values(), key=lambda source: source.declared_id or str(source.path)))


def find(
    protocol_id: str,
    *,
    project_dir: str | Path | None = None,
    user_dirs: Iterable[str | Path] = (),
) -> ProtocolSource:
    """The nearest protocol with this id, or an error naming what was searched."""
    for source in discover(project_dir=project_dir, user_dirs=user_dirs):
        if source.declared_id == protocol_id:
            return source
    searched = ", ".join(
        str(directory) for directory, _ in search_path(project_dir=project_dir, user_dirs=user_dirs)
    )
    raise ProtocolError(f"no protocol with id {protocol_id!r} was found in: {searched}")


def _documents_in(directory: Path) -> Iterator[Path]:
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in PROTOCOL_SUFFIXES:
            yield path


def _env_dirs() -> Iterator[Path]:
    raw = os.environ.get(ENV_PROTOCOL_PATH, "")
    for part in raw.split(os.pathsep):
        if part.strip():
            yield Path(part.strip()).expanduser()


def _yaml_reason(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc)
    if mark is not None:
        return f"line {mark.line + 1}, column {mark.column + 1}: {problem}"
    return str(problem)
