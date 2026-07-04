from __future__ import annotations

import argparse
import asyncio
import json

from admet.core.api import AdmetAPI
from admet.workflows import create_analyze_workflow

HEADLESS_ENGINES = ("acquisition", "opencv", "cellpose")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admet")
    parser.add_argument("target", nargs="?", choices=("analyze", "control"))
    parser.add_argument(
        "--engine",
        choices=HEADLESS_ENGINES,
        help="headless engine API mode; not valid with UI targets",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--native", action="store_true", help="launch in a native webview window")
    return parser


def main(argv: list[str] | None = None) -> None:
    try:
        _main(argv)
    except (KeyboardInterrupt, asyncio.CancelledError):
        return


def _main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.engine and args.target:
        parser.error("--engine is headless-only; use 'admet analyze' or 'admet control' for UI")
    if args.engine:
        api = create_engine_api(args.engine)
        print(json.dumps(api.describe(), sort_keys=True))
        return
    if args.target == "analyze":
        run_analyze_ui(args.port, native=args.native)
        return
    if args.target == "control":
        run_control_ui()
        return
    parser.error("choose 'analyze', 'control', or --engine")


def run_analyze_ui(port: int, *, native: bool = False) -> None:
    from nicegui import ui

    from admet.engines import create_engine_registry

    workflow = create_analyze_workflow()
    registry = create_engine_registry("analyze")

    from admet.ui.nicegui_app import render_workflow

    def root() -> None:
        render_workflow(workflow, workflow.initial_state(), registry=registry)

    ui.run(
        root=root,
        native=native,
        reload=False,
        show=False,
        port=port,
        title="admet analyze",
    )


def run_control_ui() -> None:
    from admet.ui.qt_app import run_control_app

    api = create_engine_api("acquisition")
    raise SystemExit(run_control_app(api))


def create_engine_api(engine_id: str) -> AdmetAPI:
    if engine_id == "acquisition":
        from admet.engines import create_engine_registry

        return AdmetAPI(create_engine_registry("control").create(engine_id))
    if engine_id in {"opencv", "cellpose"}:
        from admet.engines import create_engine_registry

        return AdmetAPI(create_engine_registry("analyze").create(engine_id))
    raise LookupError(f"unknown engine: {engine_id}")


if __name__ == "__main__":
    main()
