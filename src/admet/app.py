"""Launch the control desktop, serve project analysis, or describe the Python API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admet", description="ADMET desktop and instrument control")
    sub = parser.add_subparsers(dest="command", required=True)

    control = sub.add_parser("control", help="open the control desktop, which drives the rig")
    control.add_argument("--project", help="existing .admetp project")

    analyze = sub.add_parser(
        "analyze", help="serve project analysis over the network; it reads project data and never drives the rig"
    )
    analyze.add_argument("--host", default="0.0.0.0", help="address to listen on (default: every interface)")
    analyze.add_argument("--port", type=int, default=8080)
    analyze.add_argument("--projects", help="folder holding the .admetp projects to serve")

    describe = sub.add_parser(
        "describe", help="what exists: every operation, or one operation or engine"
    )
    describe.add_argument(
        "target",
        nargs="?",
        default="",
        help="an operation or engine id; omit for everything",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "control":
        from admet.ui.app import main as desktop_main

        return desktop_main(["--project", args.project] if args.project else [])

    if args.command == "analyze":
        return run_analyze_server(args.host, args.port, projects=args.projects)

    if args.command == "describe":
        from admet.core.service import Admet

        return _attempt(lambda: Admet().describe(args.target))

    parser.error(f"unknown command {args.command!r}")
    return 2


def run_analyze_server(host: str, port: int, *, projects: str | None = None) -> int:
    """One server for every browser that connects; each visit gets its own view."""
    import os

    from nicegui import ui

    from admet.core.discovery import ENV_ROOT
    from admet.engines import create_engine_registry
    from admet.ui.analyze import render_workflow
    from admet.workflows import create_analyze_workflow

    if projects:
        os.environ[ENV_ROOT] = projects
    registry = create_engine_registry("analyze")

    def root() -> None:
        workflow = create_analyze_workflow()
        render_workflow(workflow, workflow.initial_state(), registry=registry)

    try:
        ui.run(root=root, host=host, port=port, reload=False, show=False, title="admet analyze")
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C is how the server is meant to be stopped, not a failure.
        pass
    _finish_running_analyses()
    print("admet analyze stopped")
    return 0


def _finish_running_analyses() -> None:
    """Let analyses that are running finish their current file and save, before the process ends.

    A second Ctrl+C stops them at once; files already finished are still saved.
    """
    import time

    from admet.workflows.analyze_runner import SHUTDOWN, running_batches

    SHUTDOWN.after_file.set()
    if not running_batches():
        return
    print(f"waiting for {running_batches()} running analysis to finish its current file and save; "
          "Ctrl+C again to stop now")
    while running_batches():
        try:
            time.sleep(0.2)
        except KeyboardInterrupt:
            if SHUTDOWN.now.is_set():
                raise
            SHUTDOWN.now.set()
            print("stopping now; saving the files already finished")


def _attempt(call: Any) -> int:
    """Run something that can refuse, and report the refusal as the answer."""
    try:
        return _print(call())
    except Exception as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return 1


def _print(payload: Any) -> int:
    print(json.dumps(payload, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
