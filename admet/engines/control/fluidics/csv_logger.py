from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path


class CsvLogger:
    def __init__(
        self,
        output_dir: str | Path = "logs",
        *,
        prefix: str = "fluidics",
        filename: str = "",
    ):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._prefix = prefix
        self._filename = filename
        self._file = None
        self._writer = None
        self._row_count = 0
        self._filepath: str | None = None

    @property
    def filepath(self) -> str | None:
        return self._filepath

    @property
    def row_count(self) -> int:
        return self._row_count

    def start(self, pressure_count: int, sensor_count: int) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = self._filename or f"{self._prefix}_{timestamp}.csv"
        self._filepath = str(self._output_dir / filename)

        self._file = open(self._filepath, "w", encoding="utf-8", newline="")
        headers = ["timestamp", "elapsed_s"]
        headers.extend(f"pressure_{index}_mbar" for index in range(pressure_count))
        headers.extend(f"flow_{index}_ul_min" for index in range(sensor_count))
        headers.extend(f"volume_{index}_ul" for index in range(sensor_count))
        headers.extend(f"stable_{index}" for index in range(sensor_count))
        self._writer = csv.writer(self._file)
        self._writer.writerow(headers)
        self._row_count = 0
        return self._filepath

    def write_row(
        self,
        timestamp: str,
        elapsed_s: float,
        pressures: list[float],
        flows: list[float],
        volumes: list[float] | None = None,
        stability: list[bool] | None = None,
    ) -> None:
        if self._writer is None:
            return
        row = [timestamp, f"{elapsed_s:.3f}"]
        row.extend(f"{pressure:.2f}" for pressure in pressures)
        row.extend(f"{flow:.3f}" for flow in flows)
        if volumes:
            row.extend(f"{volume:.3f}" for volume in volumes)
        if stability:
            row.extend("1" if stable else "0" for stable in stability)
        self._writer.writerow(row)
        self._row_count += 1
        if self._row_count % 10 == 0 and self._file:
            self._file.flush()

    def stop(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
            self._writer = None
