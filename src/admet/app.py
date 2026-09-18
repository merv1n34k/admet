"""Command line entry point.

What this offers is what the system offers: operations, and pipelines built from
them. Both are the workflow layer -- the same surface an MCP client sees, so what
a person does by hand a program can do too, through the same path and the same
guards.

Engine actions are not here. They are hardware primitives with no opinion about
whether now is a sensible moment, and an operation is that opinion. Nothing stops
anyone reaching them from Python if they need to:

    from admet.core.service import Admet
    Admet().run("acquisition", "set_channel_flow", {"channel_index": 0, ...})

That is deliberate. It is there for whoever goes looking, and it is not the way in.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from admet.core.service import Admet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admet", description="admet headless control")
    parser.add_argument("--project", help="project to work in")
    parser.add_argument(
        "--create-project",
        action="store_true",
        help="create the project named by --project if it is not there yet",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    describe = sub.add_parser(
        "describe",
        help="what exists: the layers together, or one operation, pipeline or engine",
    )
    describe.add_argument(
        "target",
        nargs="?",
        default="",
        help="an operation, pipeline or engine; omit for all three layers",
    )

    operations = sub.add_parser(
        "operations", help="everything that can be asked for, and what it needs first"
    )
    operations.add_argument(
        "--target",
        choices=("general", "control", "analyze"),
        default="",
        help="only the session, the instrument, or the analysis",
    )

    do = sub.add_parser("do", help="carry out one operation")
    do.add_argument("operation")
    do.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    do.add_argument("--settings-json", help="settings as one JSON object")

    sub.add_parser("pipelines", help="runs built from operations")

    plan = sub.add_parser("plan", help="what a pipeline would do, without doing it")
    plan.add_argument("pipeline")
    plan.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")

    run = sub.add_parser("run", help="run a pipeline")
    run.add_argument("pipeline")
    run.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    run.add_argument("--settings-json", help="settings as one JSON object")
    run.add_argument("--from-stage", type=int, default=0, help="pick up from this stage")
    run.add_argument("--wait", type=float, default=600.0, help="seconds to wait per protocol")

    sub.add_parser("status", help="what the instrument and the session are doing")

    serve = sub.add_parser("serve", help="expose the workflow layer over MCP on stdio")
    serve.add_argument(
        "--simulated",
        action="store_true",
        help="connect the simulator and refuse to be talked out of it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        from admet.mcp.server import serve

        return serve(simulated=args.simulated, project=args.project)

    admet = _open(args)

    if args.command == "describe":
        return _attempt(lambda: admet.describe(args.target))
    if args.command == "operations":
        return _print(admet.operations(args.target))
    if args.command == "pipelines":
        return _print(admet.pipelines())
    if args.command == "status":
        return _print({**admet.state(), "session": admet.describe_project()})
    if args.command == "plan":
        return _attempt(lambda: admet.plan(args.pipeline, _settings(args)))
    if args.command == "do":
        return _attempt(lambda: admet.do(args.operation, _settings(args)))
    if args.command == "run":
        return _attempt(
            lambda: admet.run_pipeline(
                args.pipeline,
                _settings(args),
                from_stage=args.from_stage,
                wait_s=args.wait,
            )
        )

    parser.error(f"unknown command {args.command!r}")
    return 2


def _open(args: argparse.Namespace) -> Admet:
    admet = Admet()
    if args.project:
        path = Path(args.project)
        if args.create_project and not (path / "manifest.json").is_file():
            admet.create_project(path)
        else:
            admet.open_project(path)
    return admet


def _settings(args: argparse.Namespace) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if getattr(args, "settings_json", None):
        settings.update(json.loads(args.settings_json))
    for pair in getattr(args, "set", []):
        name, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"--set expects NAME=VALUE, got {pair!r}")
        settings[name] = _scalar(value)
    return settings


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


if __name__ == "__main__":
    raise SystemExit(main())
