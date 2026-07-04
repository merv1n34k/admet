from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from admet.core.session import AdmetSession, load_session, new_session, save_session, session_path


@dataclass(frozen=True)
class ProjectSelection:
    path: Path
    session: AdmetSession


def suggested_project_path(root: Path | None = None) -> Path:
    base = root or Path.cwd()
    return session_path(base / f"admet_{time.strftime('%Y%m%d_%H%M%S')}")


def create_project(path: str | Path) -> ProjectSelection:
    target = session_path(path)
    session = new_session(target.stem)
    saved = save_session(target, session)
    return ProjectSelection(saved, session)


def load_project(path: str | Path) -> ProjectSelection:
    target = session_path(path)
    return ProjectSelection(target, load_session(target))


def save_project(path: str | Path, session: AdmetSession) -> ProjectSelection:
    target = save_session(path, session)
    return ProjectSelection(target, session)
