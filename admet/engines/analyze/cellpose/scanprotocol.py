from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path


def find_scanprotocol(input_dir: str | Path) -> Path | None:
    matches = sorted(Path(input_dir).glob("*.scanprotocol"))
    return matches[0] if matches else None


def parse_scanprotocol(path: str | Path) -> dict | None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None

    field_pattern = None
    extents = None
    for element in root.iter():
        name = _local(element.tag)
        if name == "FieldSequencePattern" and element.text:
            field_pattern = element.text.strip()
        elif name == "Extents" and extents is None:
            points = _extent_points(element)
            if len(points) >= 3:
                extents = points

    if not field_pattern or not extents:
        return None

    xs = [point[0] for point in extents]
    ys = [point[1] for point in extents]
    area_w = max(xs) - min(xs)
    area_h = max(ys) - min(ys)
    if area_w <= 0 or area_h <= 0:
        return None

    return {
        "field_pattern": field_pattern,
        "area_w": area_w,
        "area_h": area_h,
        "aspect": area_w / area_h,
    }


def compute_grid(n_fields: int, aspect: float) -> tuple[int, int]:
    if n_fields <= 0:
        return (0, 0)
    best = None
    for rows in range(1, n_fields + 1):
        cols = math.ceil(n_fields / rows)
        empty = cols * rows - n_fields
        ratio_err = abs((cols / rows) - aspect)
        score = (ratio_err, empty)
        if best is None or score < best[0]:
            best = (score, cols, rows)
    return (best[1], best[2])


def field_cells(n_fields: int, cols: int, rows: int, pattern: str) -> list[tuple[int, int]]:
    cells = []
    vertical = "vertical" in pattern.lower()
    serpentine = "serpentine" in pattern.lower()
    if vertical:
        for index in range(n_fields):
            col = index // rows
            pos = index % rows
            row = pos if (col % 2 == 0 or not serpentine) else (rows - 1 - pos)
            cells.append((row, col))
    else:
        for index in range(n_fields):
            row = index // cols
            pos = index % cols
            col = pos if (row % 2 == 0 or not serpentine) else (cols - 1 - pos)
            cells.append((row, col))
    return cells


def build_layout(input_dir: str | Path, n_fields: int) -> dict | None:
    path = find_scanprotocol(input_dir)
    if path is None:
        return None
    info = parse_scanprotocol(path)
    if info is None:
        return None
    cols, rows = compute_grid(n_fields, info["aspect"])
    cells = field_cells(n_fields, cols, rows, info["field_pattern"])
    return {
        "cols": cols,
        "rows": rows,
        "pattern": info["field_pattern"],
        "cells": [list(cell) for cell in cells],
    }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _extent_points(element) -> list[tuple[float, float]]:
    points = []
    for point in element:
        if _local(point.tag) != "Point":
            continue
        x = y = None
        for coord in point:
            name = _local(coord.tag)
            if name == "_x" and coord.text:
                x = float(coord.text)
            elif name == "_y" and coord.text:
                y = float(coord.text)
        if x is not None and y is not None:
            points.append((x, y))
    return points
