from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QLabel, QVBoxLayout

from admet.ui.presenter import SurfaceVM


def render_video_preview_qt(layout: QVBoxLayout, surface: SurfaceVM, runtime: Any) -> None:
    engine = str(surface.options.get("engine") or "")
    target = None
    selected = getattr(runtime, "selected_row_for_engine", None)
    if callable(selected):
        target = selected(engine)
    if target is None:
        label = QLabel("Select a video row to preview frames and crop geometry.")
    else:
        source = Path(str(target.source_path))
        label = QLabel(f"{target.sample_id or source.stem}\n{source}")
    label.setWordWrap(True)
    layout.addWidget(label)
