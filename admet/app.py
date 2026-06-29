from __future__ import annotations

import argparse
import json

from admet.core.api import AdmetAPI
from admet.workflows import create_analyze_workflow

HEADLESS_ENGINES = ("fluidics", "opencv", "cellpose")


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

    workflow = create_analyze_workflow()
    api = create_engine_api("opencv")

    from admet.ui.analyze import render_workflow

    def root() -> None:
        render_workflow(workflow, workflow.initial_state(), api.settings, api=api)

    ui.run(
        root=root,
        native=native,
        reload=False,
        show=False,
        port=port,
        title="admet analyze",
    )


def run_control_ui() -> None:
    from admet.ui.control import run_control_app

    api = create_engine_api("fluidics")
    raise SystemExit(run_control_app(api))


def create_engine_api(engine_id: str) -> AdmetAPI:
    if engine_id == "fluidics":
        from admet.engines.control.registry import create_control_registry

        return AdmetAPI(create_control_registry().create(engine_id))
    if engine_id in {"opencv", "cellpose"}:
        from admet.engines.analyze import create_analyze_registry

        return AdmetAPI(create_analyze_registry().create(engine_id))
    raise LookupError(f"unknown engine: {engine_id}")


if __name__ == "__main__":
    main()
