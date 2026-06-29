from __future__ import annotations

import argparse

from admet.engines.analyze import create_analyze_registry
from admet.workflows import create_analyze_workflow


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="admet")
    parser.add_argument("mode", choices=("analyze", "control"))
    parser.add_argument("--engine", default="opencv", help="engine id for analyze mode")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--native", action="store_true", help="launch in a native webview window")
    args = parser.parse_args(argv)

    if args.mode == "analyze":
        from nicegui import ui

        workflow = create_analyze_workflow()
        registry = create_analyze_registry()
        try:
            engine = registry.create(args.engine)
        except LookupError as exc:
            parser.error(str(exc))
    else:
        parser.error("control uses the PySide6 UI target; the NiceGUI launcher is analysis-only")

    from admet.ui.nicegui import render_workflow

    def root() -> None:
        render_workflow(workflow, workflow.initial_state(), engine.settings, engine=engine)

    ui.run(
        root=root,
        native=args.native,
        reload=False,
        show=False,
        port=args.port,
        title=f"admet {args.mode}",
    )


if __name__ == "__main__":
    main()
