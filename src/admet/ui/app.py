"""Standalone desktop entry; no MCP owner or terminal client is involved."""

import argparse
from pathlib import Path


def desktop_lock(directory):
    from PySide6.QtCore import QLockFile

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / "desktop-owner.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise RuntimeError("Another ADMET Qt desktop owns this session; close it first")
    return lock


def main(argv=None):
    parser = argparse.ArgumentParser(description="ADMET Qt desktop")
    parser.add_argument("--project", help="Existing .admetp project")
    args = parser.parse_args(argv)
    from PySide6.QtCore import QStandardPaths
    from admet.ui.backend import DesktopBackend
    from admet.ui.control import run_control_app

    directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)) / "admet"
    try:
        lock = desktop_lock(directory)
    except RuntimeError as exc:
        parser.error(str(exc))
    try:
        backend = DesktopBackend()
        if args.project:
            backend.open_project(args.project)
        return run_control_app(backend)
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
