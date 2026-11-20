"""The file tables of the analyze stages: their columns, rows and editable cells."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from admet.core.engine import ParamKind

# The settings a file can carry, in the order their columns go: the field, its label, its kind, the
# engines it applies to. Each is a column of the Analyse table when chosen, with a value in its header
# that lands on every ticked file.
SETTING_FIELDS: tuple[tuple[str, str, ParamKind, tuple[str, ...]], ...] = (
    ("microns_per_pixel", "µm/px", ParamKind.FLOAT, ("opencv", "cellpose")),
    ("fps", "FPS", ParamKind.FLOAT, ("opencv",)),
    ("start_frame", "First frame", ParamKind.INTEGER, ("opencv",)),
    ("end_frame", "Last frame", ParamKind.INTEGER, ("opencv",)),
    ("k_sigma", "Threshold, × noise", ParamKind.FLOAT, ("opencv",)),
    ("satellite_px", "Satellite size, px", ParamKind.INTEGER, ("opencv",)),
    ("min_area", "Min droplet area, px²", ParamKind.FLOAT, ("opencv",)),
    ("skip_left_pct", "Skip left, %", ParamKind.FLOAT, ("opencv",)),
    ("skip_right_pct", "Skip right, %", ParamKind.FLOAT, ("opencv",)),
    ("gate_pct", "Gate, %", ParamKind.FLOAT, ("opencv",)),
    ("frame_limit", "Image limit", ParamKind.INTEGER, ("cellpose",)),
    ("detect_inclusions", "Detect inclusions", ParamKind.BOOLEAN, ("cellpose",)),
)
MASS_EDIT_FIELDS = SETTING_FIELDS
# The columns the Analyse table can show, in order: every setting, and two read-only summaries.
ANALYSE_COLUMN_LABELS: dict[str, str] = {**{field: label for field, label, _kind, _applies in SETTING_FIELDS},
                                         "range": "Frames", "crop": "Crop"}
DEFAULT_ANALYSE_FIELDS = ("microns_per_pixel", "fps", "range", "crop")
# What a row's table cells edit, and as which kind.
FIELD_KINDS = {field: kind for field, _label, kind, _applies in SETTING_FIELDS}
FIELD_KINDS["px_to_um"] = ParamKind.FLOAT


def files_columns() -> list[dict[str, Any]]:
    return [
        {"name": "active", "label": "Use", "field": "active", "align": "center"},
        {"name": "sample_id", "label": "Sample", "field": "sample_id", "align": "left"},
        {"name": "source", "label": "File", "field": "source", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "count", "label": "Frames / images", "field": "count", "align": "right"},
        {"name": "analyzed", "label": "Analysed", "field": "analyzed", "align": "left"},
        {"name": "remove", "label": "", "field": "remove", "align": "center"},
    ]


def analyse_fields(chosen) -> list[str]:
    """The chosen columns in their canonical order, unknown names dropped."""
    wanted = set(chosen or ())
    return [field for field in ANALYSE_COLUMN_LABELS if field in wanted]


def analyse_columns(fields) -> list[dict[str, Any]]:
    """The Analyse table's columns: Use, the file, then the chosen setting columns, then when it was analysed."""
    columns = [
        {"name": "active", "label": "Use", "field": "active", "align": "center"},
        {"name": "sample_id", "label": "Sample", "field": "sample_id", "align": "left"},
        {"name": "source", "label": "File", "field": "source", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
    ]
    columns += [{"name": field, "label": ANALYSE_COLUMN_LABELS[field], "field": field, "align": "left"}
                for field in analyse_fields(fields)]
    columns.append({"name": "analyzed", "label": "Analysed", "field": "analyzed", "align": "left"})
    return columns


def results_columns(fields: tuple[str, ...], labels: dict[str, str]) -> list[dict[str, Any]]:
    columns = [{"name": "active", "label": "Use", "field": "active", "align": "center"}]
    columns += [{"name": field, "label": labels[field], "field": field, "align": "left"} for field in fields]
    return columns


def wire_results_table(table: Any, on_change: Any, html_fields: tuple[str, ...]) -> None:
    """Use toggles like the other tables; every other cell is HTML made server-side."""
    table.add_slot("body-cell-active", _bool_cell_slot("active"))
    for field in html_fields:
        table.add_slot(f"body-cell-{field}", f"""
        <q-td :props="props"><div v-html="props.row.{field}"></div></q-td>
        """)
    table.on("matrix-change", on_change)


def files_row(row: Any, *, count: str, analyzed: str, removable: bool) -> dict[str, Any]:
    return {
        "uid": row.uid,
        "active": row.active,
        "sample_id": row.sample_id,
        "source": Path(row.source_path).name or row.source_path,
        "engine": row.engine,
        "count": count,
        "analyzed": analyzed or "—",
        "remove": removable,
    }


def analyse_row(row: Any, *, range_text: str, crop_text: str, analyzed: str) -> dict[str, Any]:
    """A file's row: every setting it can carry (None and "<field>_applies" False where its engine has none)."""
    cells: dict[str, Any] = {
        "uid": row.uid,
        "active": row.used,                   # the tick past 1. Files
        "sample_id": row.sample_id,
        "source": Path(row.source_path).name or row.source_path,
        "engine": row.engine,
        "range": range_text,
        "crop": crop_text,
        "analyzed": analyzed or "—",
    }
    for field, _label, kind, applies in SETTING_FIELDS:
        takes = row.engine in applies
        value = row.settings.get(setting_key(field, row.engine)) if takes else None
        cells[field] = (bool(value) if kind is ParamKind.BOOLEAN else _number(value)) if takes else None
        cells[f"{field}_applies"] = takes
    return cells


def wire_files_table(table: Any, on_change: Any, on_remove: Any) -> None:
    table.add_slot("body-cell-active", _bool_cell_slot("active"))
    table.add_slot("body-cell-sample_id", _text_cell_slot("sample_id"))
    table.add_slot("body-cell-remove", """
    <q-td :props="props">
      <q-btn v-if="props.row.remove" flat dense round size="sm" icon="close" title="Remove from the list"
        @click.stop="$parent.$emit('matrix-remove', {uid: props.row.uid})" />
    </q-td>
    """)
    table.on("matrix-change", on_change)
    table.on("matrix-remove", on_remove)


def wire_analyse_table(table: Any, on_change: Any, fields) -> None:
    """Each setting column edits per row; a cell is dimmed when the row's engine has no such setting.
    The headers' controls are NiceGUI elements, added by the view into the header-cell slots."""
    table.add_slot("body-cell-active", _bool_cell_slot("active"))
    kinds = dict(FIELD_KINDS)
    for field in analyse_fields(fields):
        if field not in kinds:
            continue                                         # a read-only summary column
        if kinds[field] is ParamKind.BOOLEAN:
            table.add_slot(f"body-cell-{field}", _bool_cell_slot(field, applies=f"props.row.{field}_applies"))
        else:
            required = field in ("microns_per_pixel", "fps")
            table.add_slot(f"body-cell-{field}", _required_number_slot(field, applies=f"props.row.{field}_applies",
                                                                        required=required))
    table.on("matrix-change", on_change)


def setting_key(field: str, engine: str) -> str:
    """The settings key a table field means for an engine: the imaging engine names the scale px_to_um."""
    return "px_to_um" if field == "microns_per_pixel" and engine == "cellpose" else field


def apply_setting(rows: list[Any], field: str, value: Any) -> list[Any]:
    """Set one field on every ticked row whose engine takes it; the rows that changed."""
    spec = next((item for item in MASS_EDIT_FIELDS if item[0] == field), None)
    if spec is None:
        return []
    _field, _label, kind, applies = spec
    changed = []
    for row in rows:
        if not (row.active and row.used) or row.engine not in applies:
            continue
        key = setting_key(field, row.engine)
        new = cast_matrix_value(kind, value)
        if row.settings.get(key) != new:
            row.settings[key] = new
            changed.append(row)
    return changed


def cast_matrix_value(kind: ParamKind, value: Any) -> Any:
    if kind is ParamKind.BOOLEAN:
        return bool(value)
    if kind is ParamKind.INTEGER:
        return int(_number(value) or 0)
    if kind is ParamKind.FLOAT:
        return float(_number(value) or 0.0)
    return str(value or "")


def matrix_field_kind(field: str) -> ParamKind | None:
    return FIELD_KINDS.get(field)


def _required_number_slot(field: str, *, applies: str = "true", required: bool = True) -> str:
    # For a required field, empty or zero is "missing": the cell is outlined red until a value is in.
    missing = f"props.row.{field} > 0 ? '' : 'matrix-missing'" if required else "''"
    return f"""
    <q-td :props="props">
      <q-input v-if="{applies}" dense outlined type="number" step="any" input-class="matrix-num"
        :class="{missing}"
        v-model.number="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
      <span v-else class="muted">—</span>
    </q-td>
    """


def _text_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-input dense outlined class="matrix-text" v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
    </q-td>
    """


def _bool_cell_slot(field: str, *, applies: str = "true") -> str:
    return f"""
    <q-td :props="props">
      <q-checkbox v-if="{applies}" dense v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @update:model-value="val => $parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: val}})" />
      <span v-else class="muted">—</span>
    </q-td>
    """


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
