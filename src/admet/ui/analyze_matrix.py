from __future__ import annotations

from pathlib import Path
from typing import Any

from admet.core.engine import Param, ParamKind
from admet.engines.cellpose.settings import CELLPOSE_SETTINGS
from admet.engines.opencv.settings import OPENCV_SETTINGS


ENGINE_SCHEMAS = {"opencv": OPENCV_SETTINGS, "cellpose": CELLPOSE_SETTINGS}
MATRIX_HIDDEN_PARAMS = {"video_path", "input_dir"}
FIELD_KINDS = {param.name: param.kind for schema in ENGINE_SCHEMAS.values() for param in schema.params}


def matrix_columns() -> list[dict[str, Any]]:
    return [
        {"name": "active", "label": "Use", "field": "active", "align": "center"},
        {"name": "project", "label": "Project", "field": "project", "align": "left"},
        {"name": "source", "label": "Source", "field": "source", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "sample_id", "label": "Sample ID", "field": "sample_id", "align": "left"},
        {"name": "analyzed", "label": "Analyzed", "field": "analyzed", "align": "left"},
    ]


def matrix_params(engine: str) -> tuple[Param, ...]:
    schema = ENGINE_SCHEMAS.get(engine)
    if schema is None:
        return ()
    return tuple(param for param in schema.params if param.name not in MATRIX_HIDDEN_PARAMS)


def matrix_field_kind(field: str) -> ParamKind | None:
    return FIELD_KINDS.get(field)


def cast_matrix_value(kind: ParamKind, value: Any) -> Any:
    if kind is ParamKind.BOOLEAN:
        return bool(value)
    if kind is ParamKind.INTEGER:
        return int(_numeric(value) or 0)
    if kind is ParamKind.FLOAT:
        return float(_numeric(value) or 0.0)
    return str(value or "")


def schema_matrix_columns(params: tuple[Param, ...]) -> list[dict[str, Any]]:
    columns = [
        {"name": "sample_id", "label": "Sample", "field": "sample_id", "align": "left"},
        {"name": "source", "label": "Source", "field": "source", "align": "left"},
    ]
    for param in params:
        align = "right" if param.kind in {ParamKind.INTEGER, ParamKind.FLOAT} else "left"
        columns.append({"name": param.name, "label": param.label, "field": param.name, "align": align})
    return columns


def schema_matrix_row(row: Any, params: tuple[Param, ...]) -> dict[str, Any]:
    data = {
        "uid": row.uid,
        "sample_id": row.sample_id,
        "source": Path(row.source_path).name or row.source_path,
    }
    for param in params:
        data[param.name] = _schema_cell_value(row, param)
    return data


def wire_matrix_table(table: Any, handler: Any) -> None:
    table.add_slot("body-cell-active", _bool_cell_slot("active"))
    table.add_slot(
        "body-cell-project",
        """
        <q-td :props="props">
          <div class="source-cell-name">{{ props.row.project }}</div>
        </q-td>
        """,
    )
    table.add_slot(
        "body-cell-source",
        """
        <q-td :props="props">
          <div class="source-cell-name">{{ props.row.source }}</div>
        </q-td>
        """,
    )
    table.add_slot(
        "body-cell-engine",
        """
        <q-td :props="props">
          <div class="source-cell-name">{{ props.row.engine }}</div>
        </q-td>
        """,
    )
    table.add_slot("body-cell-sample_id", _text_cell_slot("sample_id"))
    table.on("matrix-change", handler)


def wire_schema_matrix_table(table: Any, params: tuple[Param, ...], handler: Any) -> None:
    for param in params:
        table.add_slot(f"body-cell-{param.name}", _schema_cell_slot(param))
    table.on("matrix-change", handler)


def _schema_cell_value(row: Any, param: Param) -> Any:
    default = param.default
    if param.kind is ParamKind.BOOLEAN:
        return _row_bool(row, param.name, bool(default))
    if param.kind is ParamKind.INTEGER:
        return _row_int(row, param.name, int(default) if default is not None else 0)
    if param.kind is ParamKind.FLOAT:
        return _row_float(row, param.name, float(default) if default is not None else 0.0)
    return str(_row_setting(row, param.name, default if default is not None else ""))


def _numeric_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-input dense outlined type="number" input-class="matrix-num"
        v-model.number="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
    </q-td>
    """


def _text_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-input dense outlined v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
    </q-td>
    """


def _bool_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-checkbox dense v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @update:model-value="val => $parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: val}})" />
    </q-td>
    """


def _schema_cell_slot(param: Param) -> str:
    if param.kind is ParamKind.BOOLEAN:
        return _bool_cell_slot(param.name)
    if param.kind in {ParamKind.INTEGER, ParamKind.FLOAT}:
        return _numeric_cell_slot(param.name)
    return _text_cell_slot(param.name)


def _row_setting(row: Any, key: str, default: Any) -> Any:
    value = row.settings.get(key, default)
    return default if value in {None, ""} else value


def _row_int(row: Any, key: str, default: Any) -> int:
    return int(_numeric(_row_setting(row, key, default)) or 0)


def _row_float(row: Any, key: str, default: Any) -> float:
    return float(_numeric(_row_setting(row, key, default)) or 0.0)


def _row_bool(row: Any, key: str, default: Any) -> bool:
    value = _row_setting(row, key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
