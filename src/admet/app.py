"""Command line entry point.

There is no interface here beyond a terminal. It asks core what an engine can do,
or tells core to do one thing, and prints the answer as JSON -- the same surface
the MCP server drives, so what a person does by hand a program can do too,
through the same path.

Nothing here talks to an engine directly. Core owns the session and decides where
anything is written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from admet.core.service import Admet

ENGINES = ("acquisition", "opencv", "cellpose")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admet", description="admet headless control")
    sub = parser.add_subparsers(dest="command", required=True)

    describe = sub.add_parser("describe", help="what an engine can do")
    describe.add_argument("engine", choices=ENGINES)

    run = sub.add_parser("run", help="ask an engine to do one thing")
    run.add_argument("engine", choices=ENGINES)
    run.add_argument("action", help="an action id, as listed by describe")
    run.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a setting for the action; repeat for more",
    )
    run.add_argument(
        "--settings-json",
        help="all settings as one JSON object, for values a shell would mangle",
    )
    run.add_argument(
        "--project",
        help="project to run in; actions that write files need one",
    )
    run.add_argument(
        "--create-project",
        action="store_true",
        help="create the project named by --project if it is not there yet",
    )

    serve = sub.add_parser("serve", help="expose the engines over MCP on stdio")
    serve.add_argument(
        "--simulated",
        action="store_true",
        help="refuse any action that would reach real hardware",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "describe":
        print(json.dumps(Admet().describe(args.engine), indent=2, sort_keys=True))
        return 0
    if args.command == "run":
        return _run(args)
    if args.command == "serve":
        from admet.mcp.server import serve

        return serve(simulated=args.simulated)
    parser.error(f"unknown command {args.command!r}")
    return 2


def _run(args: argparse.Namespace) -> int:
    admet = Admet()
    if args.project:
        path = Path(args.project)
        if args.create_project and not (path / "manifest.json").is_file():
            admet.create_project(path)
        else:
            admet.open_project(path)

    settings: dict[str, Any] = {}
    if args.settings_json:
        settings.update(json.loads(args.settings_json))
    for pair in args.set:
        name, separator, value = pair.partition("=")
        if not separator:
            print(f"--set expects NAME=VALUE, got {pair!r}", file=sys.stderr)
            return 2
        settings[name] = _scalar(value)

    # Core reports failure by raising, so anything returned is a result and
    # anything raised is the message worth showing.
    try:
        result = admet.run(args.engine, args.action, settings, job_id=f"cli_{args.action}")
    except Exception as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": result.status,
                "warnings": list(result.warnings),
                "metadata": result.metadata,
            },
            default=str,
            indent=2,
        )
    )
    return 0


def _scalar(value: str) -> Any:
    """Read a command-line value as the type it looks like, and nothing cleverer."""
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def create_engine_api(engine_id: str):
    """An engine on its own, for tests and for the analysis runner.

    Everything an operator or a program does goes through Admet instead: an
    engine reached directly has no session, so anything it writes lands outside
    the project.
    """
    from admet.core.api import AdmetAPI

    return AdmetAPI(Admet().engine(engine_id))


if __name__ == "__main__":
    raise SystemExit(main())
