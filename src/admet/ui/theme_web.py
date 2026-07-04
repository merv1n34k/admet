from __future__ import annotations

from admet.ui.design import NOTICE_COLORS, PALETTE, RADII, SPACING, STATUS_COLORS, TYPOGRAPHY


def stylesheet() -> str:
    return f"""
    <style>
      body {{
        background: {PALETTE.background};
        color: {PALETTE.text};
        font-family: {TYPOGRAPHY.family};
      }}
      .admet-shell {{ min-height: 100vh; gap: 0; }}
      .admet-topbar {{
        background: {PALETTE.control};
        border-bottom: 1px solid {PALETTE.border};
        padding: {SPACING.topbar_y}px {SPACING.topbar_x}px;
      }}
      .admet-body {{ gap: {SPACING.body_gap}px; padding: {SPACING.body_gap}px; }}
      .admet-sidebar {{
        width: {SPACING.sidebar_width}px;
        position: sticky;
        top: {SPACING.body_gap}px;
        gap: 6px;
      }}
      .admet-content {{ min-width: 0; gap: 10px; }}
      .admet-panel {{
        background: {PALETTE.control};
        border: 1px solid {PALETTE.border};
        border-radius: {RADII.panel}px;
        padding: 10px;
        gap: {SPACING.default}px;
      }}
      .admet-panel-title {{
        font-size: {TYPOGRAPHY.body}px;
        font-weight: {TYPOGRAPHY.panel_title_weight};
        color: {PALETTE.text_heading};
      }}
      .admet-step {{
        width: 100%;
        justify-content: flex-start;
        border-radius: {RADII.control}px;
        color: {PALETTE.text_step};
      }}
      .admet-step-current {{ background: {PALETTE.control_pressed}; }}
      .admet-step-complete {{ color: {STATUS_COLORS["complete"]}; }}
      .admet-step-skipped, .admet-step-pending {{ color: {STATUS_COLORS["pending"]}; }}
      .admet-notice {{
        background: {PALETTE.control};
        border: 1px solid {NOTICE_COLORS["primary"]};
        border-radius: {RADII.panel}px;
        padding: 10px;
        margin-top: {SPACING.default}px;
      }}
      .admet-notice-success {{ border-color: {NOTICE_COLORS["success"]}; }}
      .admet-notice-warning {{ border-color: {NOTICE_COLORS["warning"]}; }}
      .admet-notice-danger {{ border-color: {NOTICE_COLORS["danger"]}; }}
      .admet-actions {{ gap: 6px; flex-wrap: wrap; }}
      .admet-field-grid {{ gap: {SPACING.default}px; }}
      .admet-surface {{ gap: {SPACING.default}px; }}
      .admet-surface-placeholder {{
        min-height: {SPACING.surface_min_height}px;
        justify-content: center;
        align-items: center;
        border: 1px dashed {PALETTE.border};
        border-radius: {RADII.panel}px;
      }}
      .admet-video-placeholder {{
        width: 100%;
        min-height: {SPACING.preview_min_height}px;
        position: relative;
        overflow: hidden;
        background: {PALETTE.video_placeholder};
        border-radius: {RADII.panel}px;
        display: flex;
        align-items: center;
        justify-content: center;
      }}
      .admet-video-bubbles::before,
      .admet-video-bubbles::after {{
        content: "";
        position: absolute;
        border-radius: {RADII.round}px;
        background: {PALETTE.video_bubble};
      }}
      .admet-video-bubbles::before {{ width: 180px; height: 180px; left: 20%; top: 15%; }}
      .admet-video-bubbles::after {{ width: 110px; height: 110px; right: 24%; bottom: 18%; }}
      .admet-video-caption {{
        position: relative;
        max-width: 80%;
        color: {PALETTE.video_caption};
        font-size: {TYPOGRAPHY.small}px;
        word-break: break-word;
      }}
      .admet-project-select {{ min-width: 300px; }}
    </style>
    """
