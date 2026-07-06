from __future__ import annotations

import unittest

from admet.ui.analyze import RawSummary, _scatter_chart, _track_timeline_chart


class AnalyzeChartTests(unittest.TestCase):
    def test_track_timeline_uses_cartesian_line_segments(self) -> None:
        summary = _summary(spans=((1, 0, 12), (2, 3, 18)))

        chart = _track_timeline_chart([summary])

        self.assertTrue(chart["grid"]["containLabel"])
        self.assertEqual(chart["xAxis"]["name"], "frame")
        self.assertEqual(chart["yAxis"]["name"], "droplet id")
        self.assertEqual([series["type"] for series in chart["series"]], ["line", "line"])
        self.assertEqual(chart["series"][0]["data"], [[0, 1], [12, 1]])

    def test_scatter_chart_keeps_axis_labels_inside_grid(self) -> None:
        chart = _scatter_chart(
            "Area by x position",
            "x",
            "um2",
            [_summary(detections=((0, 10, 20, 30, 40),))],
            lambda detection: [detection[1], detection[3]],
        )

        self.assertTrue(chart["grid"]["containLabel"])
        self.assertGreaterEqual(chart["xAxis"]["nameGap"], 30)
        self.assertGreaterEqual(chart["yAxis"]["nameGap"], 30)
        self.assertEqual(chart["series"][0]["data"], [[10, 30]])


def _summary(
    *,
    detections: tuple[tuple[float, float, float, float, float], ...] = (),
    spans: tuple[tuple[float, float, float], ...] = (),
) -> RawSummary:
    return RawSummary(
        project="project.admetp",
        run_id="run-1",
        sample_id="sample-a",
        engine="opencv",
        rows=0,
        frames=0,
        droplets=0,
        mean_diameter=0.0,
        median_diameter=0.0,
        std_diameter=0.0,
        cv_percent=0.0,
        inclusions=0,
        volume_nl=0.0,
        true_count=0.0,
        frequency_hz=0.0,
        threshold=0.0,
        detections=detections,
        spans=spans,
    )


if __name__ == "__main__":
    unittest.main()
