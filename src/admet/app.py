"""Command line entry point.

Launch the standalone desktop, or discover, serve and control a headless owner.

    admet qt [--project PATH]
    admet describe [TARGET]
    admet serve (--simulated | --live) [--project PATH] [--runtime PATH]
    admet control --runtime PATH [--once]

Running an experiment is deliberately not here. It happens through MCP or
through the Python binding, both of which go through the same guarded
operations; a terminal command per operation would be a third way to do the
same thing, and the one that drifts.

    from admet.core.service import Admet
    admet = Admet()
    admet.do("connect_fluidics", {"simulated": True})

Engine actions stay on that binding too -- Admet.engine_action -- as the expert
escape hatch. They have no guards, which is what they are for.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admet", description="ADMET desktop and instrument control")
    parser.add_argument("--project", help="project to work in")
    parser.add_argument(
        "--create-project",
        action="store_true",
        help="create the project named by --project if it is not there yet",
    )
    parser.add_argument("--runtime", help="directory to publish runtime telemetry into")
    sub = parser.add_subparsers(dest="command", required=True)

    qt = sub.add_parser("qt", help="open the standalone Qt desktop")
    qt.add_argument("--project", default=argparse.SUPPRESS, help="existing .admetp project")

    describe = sub.add_parser(
        "describe", help="what exists: every operation, or one operation or engine"
    )
    describe.add_argument(
        "target",
        nargs="?",
        default="",
        help="an operation or engine id; omit for everything",
    )

    serve = sub.add_parser("serve", help="the one process that owns the instrument")
    hardware = serve.add_mutually_exclusive_group(required=True)
    hardware.add_argument(
        "--simulated",
        action="store_true",
        help="permit only the simulator, and refuse to be talked out of it",
    )
    hardware.add_argument(
        "--live",
        action="store_true",
        help="permit real hardware",
    )
    serve.add_argument("--runtime", default=argparse.SUPPRESS)
    serve.add_argument("--project", default=argparse.SUPPRESS)
    serve.add_argument("--create-project", action="store_true", default=argparse.SUPPRESS)

    control = sub.add_parser("control", help="attach to the owner to review and control runs")
    control.add_argument("--runtime", required=True, help="the serving process's runtime directory")
    control.add_argument("--once", action="store_true", help="draw one frame and exit")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "qt":
        if args.runtime or args.create_project:
            parser.error("qt does not use --runtime or --create-project; create projects in the app")
        from admet.ui.app import main as desktop_main

        return desktop_main(["--project", args.project] if args.project else [])

    if args.command == "control":
        from admet.core.control import control

        return control(args.runtime, once=args.once)

    if args.command == "serve":
        from admet.mcp.server import serve

        return serve(
            simulated=args.simulated,
            project=args.project,
            create_project=args.create_project,
            runtime=getattr(args, "runtime", None),
        )

    if args.command == "describe":
        # The terminal attaches over MCP and must not load the service or engines.
        from admet.core.service import Admet

        return _attempt(lambda: _open(Admet(), args).describe(args.target))

    parser.error(f"unknown command {args.command!r}")
    return 2


def _open(admet: Any, args: argparse.Namespace) -> Any:
    if args.project:
        path = Path(args.project)
        if args.create_project and not (path / "manifest.json").is_file():
            admet.create_project(path)
        else:
            admet.open_project(path)
    return admet


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
