from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from admet.ui.presenter import SurfaceVM


def render_video_preview(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    html = runtime.surface_html(surface)
    if "admet-viewer" in html:
        ui.html(html).classes("w-full")
        return
    target = None
    selected = getattr(runtime, "selected_row_for_engine", None)
    if callable(selected):
        target = selected(str(surface.options.get("engine") or ""))
    ui.html(_viewer_placeholder(target)).classes("w-full")


def _viewer_placeholder(target: Any | None) -> str:
    if target is None:
        title = "Preview unavailable"
        detail = "Select a video row to preview frames and crop geometry."
        path = ""
    else:
        source = Path(str(target.source_path))
        title = html.escape(str(target.sample_id or source.stem or "Video preview"))
        detail = "Frame preview will appear here when the source can be decoded."
        path = html.escape(str(source))
    return f"""
    <div class="admet-viewer admet-viewer-placeholder">
      <div class="admet-viewer-message">
        <div>
          <div class="admet-viewer-placeholder-title">{title}</div>
          <div>{detail}</div>
          <div class="admet-viewer-placeholder-path">{path}</div>
        </div>
      </div>
    </div>
    """
