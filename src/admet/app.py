"""Launch the Qt desktop or describe the direct Python API."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admet", description="ADMET desktop and instrument control")
    sub = parser.add_subparsers(dest="command", required=True)

    qt = sub.add_parser("qt", help="open the standalone Qt desktop")
    qt.add_argument("--project", help="existing .admetp project")

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

    if args.command == "qt":
        from admet.ui.app import main as desktop_main

        return desktop_main(["--project", args.project] if args.project else [])

    if args.command == "describe":
        from admet.core.service import Admet

        return _attempt(lambda: Admet().describe(args.target))

    parser.error(f"unknown command {args.command!r}")
    return 2


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
