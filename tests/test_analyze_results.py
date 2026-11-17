from __future__ import annotations

import io
import json
import math
import tempfile
import unittest
import zipfile
from pathlib import Path

from admet.ui.analyze_results import (
    FileResult,
    StoredRun,
    volume_chart,
    fluidics_csv_path,
    dispersed_indices,
    delivered_volume,
    results_cells,
    expected_rate_hz,
    dispersed_flow_ul_min,
    bin_labels,
    cv_chart,
    diameter_chart,
    export_csv_zip,
    file_results,
    histogram_edges,
    inclusions_chart,
    result_charts,
    results_table_html,
    scatter_chart,
    settings_summary,
    short_names,
    spark,
    timeline_chart,
)


def _result(**overrides) -> FileResult:
    values = {"project": "p", "sample_id": "s", "engine": "opencv", "source_path": "/v.avi", "file_id": "f",
              "analyzed_at": "",
              "settings": {}, "fps": 100.0, "scale": 2.0, "frames": 10, "droplets": 3, "rate_hz": 20.0,
              "speed_mm_s": 2.0}
    values.update(overrides)
    return FileResult(**values)


def _run(project: Path) -> StoredRun:
    return StoredRun(project_path=project, run_id="analysis", raw_path=project / "analysis" / "raw.jsonl",
                     jobs=({"engine": "opencv", "sample_id": "s", "file_id": "f1", "status": "complete",
                            "source_path": "media/v.avi",
                            "metadata": {"analyzed_at": "2026-11-13T10:00:00+00:00", "total_droplets": 3}},),
                     matrix=({"file_id": "f1", "settings": {"start_frame": 0, "end_frame": 10, "k_sigma": 4}},))


def _rows() -> list[dict]:
    """Three droplets, each seen for three frames moving 10 px a frame, one every five frames."""
    rows = [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "run_context",
             "values": {"analysis": {"fps": 100.0, "microns_per_pixel": 2.0}, "detection": {"gate_pct": 50}}}]
    for droplet in range(3):
        for step in range(3):
            frame = droplet * 5 + step
            rows.append({"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "detection",
                         "values": {"frame": frame, "droplet_id": droplet + 1, "centroid_x": 100 + 10 * step,
                                    "centroid_y": 50, "equivalent_diameter": 20 + droplet, "area": 314 + step}})
    return rows


class FileResultTests(unittest.TestCase):
    def test_one_value_per_droplet_with_rate_and_speed(self):
        result, = file_results(_run(Path("/p/study.admetp")), _rows())

        self.assertEqual(result.droplets, 3)
        self.assertEqual(result.diameters, (40.0, 42.0, 44.0))           # medians per droplet, in µm
        self.assertEqual(len(result.areas), 3)
        self.assertAlmostEqual(result.periods_s[0], 0.05)
        self.assertAlmostEqual(result.rate_hz, 20.0)
        self.assertAlmostEqual(result.speed_mm_s, 2.0)                   # 10 px/frame · 2 µm/px · 100 fps
        self.assertEqual(result.analyzed_at[:10], "2026-11-13")
        self.assertEqual(result.settings["k_sigma"], 4)
        self.assertEqual(result.settings["fps"], 100.0)
        self.assertEqual(result.source_path, "/p/study.admetp/media/v.avi")
        self.assertIn("µm/px 2", settings_summary(result.settings))

    def test_the_scale_and_fps_of_the_day_apply_without_a_new_run(self):
        scales = {("opencv", "/p/study.admetp/media/v.avi"): (4.0, 200.0)}

        result, = file_results(_run(Path("/p/study.admetp")), _rows(), scales=scales)

        self.assertEqual(result.diameters, (80.0, 84.0, 88.0))           # px × 4 µm/px, not the run's 2
        self.assertAlmostEqual(result.rate_hz, 40.0)                     # 5 frames apart at 200 fps
        self.assertAlmostEqual(result.speed_mm_s, 8.0)                   # 10 px/frame · 4 µm/px · 200 fps
        self.assertEqual((result.scale, result.fps, result.settings["fps"]), (4.0, 200.0, 200.0))
        self.assertEqual(result.expected_hz, 0.0)                        # no flow known: nothing expected

    def test_the_expected_rate_comes_from_the_dispersed_flow_and_the_droplet_size(self):
        record = {"channels": [{"label": "Oil L", "mode": "flow", "flow_ul_min": 300.0},
                               {"label": "Cells M", "mode": "flow", "flow_ul_min": 40.0},
                               {"label": "Beads M", "mode": "flow", "flow_ul_min": 40.0}]}
        self.assertEqual(dispersed_flow_ul_min(record), 80.0)
        self.assertEqual(dispersed_flow_ul_min({"channels": [{"label": "Oil L", "flow_ul_min": 300.0}]}), 0.0)
        self.assertEqual(dispersed_flow_ul_min(None), 0.0)
        self.assertAlmostEqual(expected_rate_hz(80.0, 124.0), 1336, delta=2)   # 1.333 µL/s over 1.0 nL
        self.assertEqual(expected_rate_hz(80.0, 0.0), 0.0)

        scales = {("opencv", "/p/study.admetp/media/v.avi"): (2.0, 100.0, 80.0)}
        result, = file_results(_run(Path("/p/study.admetp")), _rows(), scales=scales)

        self.assertEqual(result.dispersed_ul_min, 80.0)
        self.assertAlmostEqual(result.expected_hz, expected_rate_hz(80.0, 42.0), delta=1)   # the median diameter
        self.assertIn("expected", results_cells(result, "#000")["rate"])

    def test_the_delivered_volume_is_read_from_the_runs_fluidics_csv_within_the_recording(self):
        record = {"run_id": "run_x", "started_at": "2026-11-16T10:00:05", "stopped_at": "2026-11-16T10:00:15",
                  "channels": [{"index": 0, "label": "Oil L"}, {"index": 1, "label": "Cells M"},
                               {"index": 2, "label": "Beads M"}]}
        self.assertEqual(dispersed_indices(record), (1, 2))
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "study.admetp"
            run_dir = project / "records" / "protocols" / "run_x"
            run_dir.mkdir(parents=True)
            csv_path = project / "records" / "fluidics" / "run_x.csv"
            csv_path.parent.mkdir()
            lines = ["timestamp,elapsed_s,pressure_0_mbar,flow_0_ul_min,flow_1_ul_min,flow_2_ul_min,"
                     "volume_0_ul,volume_1_ul,volume_2_ul,stable_0"]
            for tick in range(41):                                          # every 0.5 s from 10:00:00
                seconds = tick * 0.5
                lines.append(f"10:00:{seconds:06.3f},{seconds + 100:.3f},15.0,300,40,40,"
                             f"{1000 + 5 * seconds:.3f},{200 + seconds / 1.5:.4f},{300 + seconds / 1.5:.4f},1")
            csv_path.write_text("\n".join(lines) + "\n")
            (run_dir / "summary.json").write_text(json.dumps({"artifacts": {"fluidics_csv": "records/fluidics/run_x.csv"}}))

            self.assertEqual(fluidics_csv_path(project, "run_x"), csv_path)
            self.assertIsNone(fluidics_csv_path(project, "run_y"))
            series = delivered_volume(csv_path, record["started_at"], record["stopped_at"], dispersed_indices(record))

        self.assertEqual(series[0], (0.0, 0.0))                             # from the recording's start
        self.assertEqual(series[-1][0], 10.0)                               # to its stop, not the CSV's end
        self.assertAlmostEqual(series[-1][1], 2 * 10 / 1.5, places=2)       # cells and beads, no oil
        self.assertEqual(len(series), 21)
        # Across midnight: the stop is on the next day.
        self.assertEqual(delivered_volume(Path("/nowhere.csv"), "2026-11-16T23:59:59", "2026-11-17T00:00:09", (1,)), ())

    def test_the_detected_volume_accumulates_per_droplet_and_the_plot_compares_it_with_the_delivered(self):
        delivered = [[0.0, 0.0], [0.05, 0.0001], [0.1, 0.0002]]
        key = ("opencv", "/p/study.admetp/media/v.avi")

        result, = file_results(_run(Path("/p/study.admetp")), _rows(), deliveries={key: delivered})

        sphere = lambda d: math.pi / 6 * d ** 3 * 1e-9                       # µm to µL
        self.assertEqual([t for t, _ in result.detected_ul], [0.0, 0.05, 0.1])   # frames 0, 5, 10 at 100 fps
        self.assertAlmostEqual(result.detected_ul[0][1], sphere(40), places=9)
        self.assertAlmostEqual(result.detected_total_ul, sphere(40) + sphere(42) + sphere(44), places=9)
        self.assertEqual(result.delivered_ul, ((0.0, 0.0), (0.05, 0.0001), (0.1, 0.0002)))
        self.assertEqual(result.delivered_total_ul, 0.0002)

        chart = volume_chart([result])
        self.assertEqual([s["name"].split()[-1] for s in chart["series"]], ["delivered", "detected"])
        self.assertIn("volume_delivered_vs_detected", [name for name, _ in result_charts([result])])
        self.assertIn("of 0.0002 delivered", results_cells(result, "#000")["volume"])

        alone, = file_results(_run(Path("/p/study.admetp")), _rows())
        self.assertEqual(alone.delivered_ul, ())
        self.assertIn("no fluidics record", results_cells(alone, "#000")["volume"])

    def test_a_rate_from_speed_over_spacing_is_taken_as_the_engine_gave_it(self):
        rows = _rows()
        rows[0]["values"]["generation"] = {"method": "spacing", "spacing_px": 50.0, "period_frames": 5.0,
                                           "crossings": 0, "rate_hz": 20.0}

        result, = file_results(_run(Path("/p/study.admetp")), rows)

        self.assertAlmostEqual(result.rate_hz, 20.0)                     # 100 fps over 5 frames
        self.assertEqual(result.rate_note, "speed over spacing")

    def test_two_files_with_the_same_sample_name_stay_two_results(self):
        rows = []
        for file_id, source in (("f1", "media/a.avi"), ("f2", "media/b.avi")):
            rows.append({"engine": "opencv", "item_id": "cells", "file_id": file_id, "kind": "run_context",
                         "values": {"analysis": {"fps": 50.0, "microns_per_pixel": 1.0}}})
            rows.append({"engine": "opencv", "item_id": "cells", "file_id": file_id, "kind": "detection",
                         "values": {"frame": 1, "droplet_id": 1, "centroid_x": 5.0, "centroid_y": 5.0,
                                    "equivalent_diameter": 30, "area": 700}})
        run = StoredRun(Path("/p/study.admetp"), "analysis", Path("analysis/raw.jsonl"),
                        jobs=tuple({"engine": "opencv", "sample_id": "cells", "file_id": file_id, "status": "complete",
                                    "source_path": source, "metadata": {}}
                                   for file_id, source in (("f1", "media/a.avi"), ("f2", "media/b.avi"))))

        results = file_results(run, rows)

        self.assertEqual([(r.sample_id, r.file_id, Path(r.source_path).name) for r in results],
                         [("cells", "f1", "a.avi"), ("cells", "f2", "b.avi")])

    def test_every_row_of_a_long_file_counts_and_the_plot_points_are_spread(self):
        rows = [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "run_context",
                 "values": {"analysis": {"fps": 100.0, "microns_per_pixel": 1.0}}}]
        rows += [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "detection",
                  "values": {"frame": i, "droplet_id": i, "centroid_x": 1.0, "centroid_y": 1.0,
                             "equivalent_diameter": 10, "area": 78}} for i in range(70_000)]

        result, = file_results(_run(Path("/p/study.admetp")), iter(rows))

        self.assertEqual(len(result.diameters), 70_000)
        self.assertEqual(len(result.detections), 6000)
        self.assertGreater(result.detections[-1][0], 60_000)       # a sample from the whole file, not its start

    def test_an_imaging_droplet_counts_once_per_image(self):
        """Cellpose numbers droplets from 0 in every image; the same id in two images is two droplets."""
        rows = [{"engine": "cellpose", "item_id": "imgs", "kind": "droplet",
                 "values": {"frame": frame, "droplet_id": droplet, "center_x": 10.0 * droplet, "center_y": 5.0,
                            "diameter_um": 140.0 + droplet, "area_um2": 15000.0, "inclusions": frame + droplet}}
                for frame in (1, 2) for droplet in (0, 1)]
        run = StoredRun(Path("/p/study.admetp"), "analysis", Path("analysis/raw.jsonl"),
                        jobs=({"engine": "cellpose", "sample_id": "imgs", "file_id": "f2", "status": "complete",
                               "source_path": "media/imgs", "metadata": {"droplet_count": 4}},))

        result, = file_results(run, rows)

        self.assertEqual(result.droplets, 4)
        self.assertEqual(result.diameters, (140.0, 141.0, 140.0, 141.0))
        self.assertEqual(result.inclusion_counts, (1, 2, 2, 3))
        self.assertEqual(len(result.detections), 4)
        self.assertEqual(result.rate_hz, 0.0)

    def test_hand_corrections_switch_droplets_off_and_change_inclusions(self):
        rows = [{"engine": "cellpose", "item_id": "imgs", "kind": "droplet",
                 "values": {"frame": 1, "droplet_id": droplet, "center_x": 10.0 * droplet, "center_y": 5.0,
                            "diameter_um": 140.0, "area_um2": 15000.0, "inclusions": 1}}
                for droplet in (0, 1, 2)]
        run = StoredRun(Path("/p/study.admetp"), "analysis", Path("analysis/raw.jsonl"),
                        jobs=({"engine": "cellpose", "sample_id": "imgs", "file_id": "f2", "status": "complete",
                               "source_path": "media/imgs", "metadata": {"droplet_count": 3}},))
        fixes = {("cellpose", "/p/study.admetp/media/imgs"): {"1:0": {"disabled": True}, "1:2": {"inclusions": 2}}}

        result, = file_results(run, rows, fixes)

        self.assertEqual(result.droplets, 2)
        self.assertEqual(result.inclusion_counts, (1, 3))

    def test_the_table_holds_the_numbers_and_their_sparklines(self):
        result, = file_results(_run(Path("/p/study.admetp")), _rows())

        table = results_table_html([result])

        self.assertIn("42.0 ± 1.6", table)
        self.assertIn('class="spark"', table)
        self.assertIn("k 4", table)
        self.assertNotIn("Inclusions", table)

    def test_spark_is_empty_below_two_values(self):
        self.assertEqual(spark([1.0]), "")
        self.assertIn("<rect", spark([1.0, 1.1, 1.2, 5.0]))


class ChartTests(unittest.TestCase):
    def test_histogram_bins_follow_a_tight_distribution(self):
        values = [49.9 + 0.01 * (i % 20) for i in range(400)]

        edges = histogram_edges(values)
        labels = bin_labels(edges)

        self.assertEqual(len(labels), len(set(labels)))                 # neighbours read differently
        self.assertGreaterEqual(len(labels), 8)

    def test_diameter_chart_colours_each_file(self):
        chart = diameter_chart([_result(sample_id="a", diameters=(10.0, 11.0, 12.0)),
                                _result(sample_id="b", diameters=(20.0, 21.0, 22.0))])

        self.assertEqual([series["name"] for series in chart["series"]], ["a", "b"])
        self.assertNotEqual(chart["series"][0]["itemStyle"]["color"], chart["series"][1]["itemStyle"]["color"])
        self.assertEqual(sum(chart["series"][0]["data"]), 3)

    def test_cv_chart_has_one_bar_per_sample_in_its_colour(self):
        chart = cv_chart([_result(sample_id="a", diameters=(10.0, 10.0, 12.0)),
                          _result(sample_id="b", diameters=(20.0, 20.0)), _result(sample_id="none")])

        self.assertEqual(chart["xAxis"]["data"], ["a", "b"])
        values = [point["value"] for point in chart["series"][0]["data"]]
        self.assertAlmostEqual(values[0], 8.84, places=2)
        self.assertEqual(values[1], 0.0)
        self.assertNotEqual(chart["series"][0]["data"][0]["itemStyle"]["color"],
                            chart["series"][0]["data"][1]["itemStyle"]["color"])

    def test_only_plots_with_data_for_the_ticked_files_are_shown(self):
        video = _result(sample_id="v", diameters=(10.0, 11.0), spans=((1, 0, 5),), arrivals_s=(0.0, 0.1),
                        detections=((0, 1.0, 2.0, 3.0),))
        imaging = _result(sample_id="i", engine="cellpose", diameters=(140.0, 141.0), inclusion_counts=(0, 1, 1, 3),
                          detections=((1, 10.0, 20.0, 15000.0),))

        self.assertEqual([name for name, _ in result_charts([video])],
                         ["diameter_distribution", "cv_per_sample", "droplet_timeline", "droplet_positions",
                          "area_along_x"])
        self.assertEqual([name for name, _ in result_charts([imaging])],
                         ["diameter_distribution", "cv_per_sample", "droplet_positions", "area_along_x",
                          "inclusions_per_droplet"])
        self.assertEqual(result_charts([]), [])
        chart = inclusions_chart([imaging])
        self.assertEqual(chart["xAxis"]["data"], ["0", "1", "3"])
        self.assertEqual(chart["series"][0]["data"], [1, 2, 1])

    def test_timeline_counts_droplets_against_seconds_from_each_files_start(self):
        chart = timeline_chart([_result(sample_id="a", arrivals_s=(0.0, 0.01, 0.02)),
                                _result(sample_id="b", arrivals_s=(0.0, 0.05))])

        self.assertEqual(chart["series"][0]["data"], [[0.0, 1], [0.01, 2], [0.02, 3]])
        self.assertEqual(chart["series"][1]["data"], [[0.0, 1], [0.05, 2]])
        self.assertEqual(chart["yAxis"]["name"], "droplets")

    def test_plots_name_samples_by_what_differs(self):
        names = ["run_18dc8be5506449fda417fd6206c9230f_step01_20261113_183825",
                 "run_18dc8be5506449fda417fd6206c9230f_step02_20261113_184149"]

        self.assertEqual(short_names(names), {names[0]: "step01_20261113_183825", names[1]: "step02_20261113_184149"})
        self.assertEqual(short_names(["a", "b"]), {"a": "a", "b": "b"})
        self.assertEqual(short_names(["only"]), {"only": "only"})
        chart = cv_chart([_result(sample_id=names[0], diameters=(1.0, 2.0)),
                          _result(sample_id=names[1], diameters=(1.0, 2.0))])
        self.assertEqual(chart["xAxis"]["data"], ["step01_20261113_183825", "step02_20261113_184149"])

    def test_the_rate_falls_back_to_arrivals_when_the_gate_saw_few_droplets(self):
        rows = [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "run_context",
                 "values": {"analysis": {"fps": 100.0, "microns_per_pixel": 1.0},
                            "generation": {"crossings": 2, "rate_hz": 3.0}}}]
        rows += [{"engine": "opencv", "item_id": "s", "file_id": "f1", "kind": "detection",
                  "values": {"frame": 2 * i, "droplet_id": i, "centroid_x": 1.0, "centroid_y": 1.0,
                             "equivalent_diameter": 10, "area": 78}} for i in range(50)]

        result, = file_results(_run(Path("/p/study.admetp")), rows)

        self.assertAlmostEqual(result.rate_hz, 50.0)                        # 49 intervals over 98 frames at 100 fps
        self.assertEqual(result.rate_note, "from arrivals; gate saw 2 of 50")
        self.assertEqual(result.arrivals_s[:3], (0.0, 0.02, 0.04))

    def test_scatter_chart_keeps_axis_labels_inside_grid(self):
        chart = scatter_chart("t", "x", "y", [_result(detections=((0, 1.0, 2.0, 3.0),))], lambda d: [d[1], d[2]])

        self.assertTrue(chart["grid"]["containLabel"])
        self.assertEqual(chart["series"][0]["data"], [[1.0, 2.0]])


class ExportTests(unittest.TestCase):
    def test_csv_export_zips_a_summary_and_every_droplet_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "study.admetp"
            (project / "analysis").mkdir(parents=True)
            (project / "analysis" / "raw.jsonl").write_text("\n".join(json.dumps(row) for row in _rows()))
            run = _run(project)
            results = file_results(run, _rows())

            archive = zipfile.ZipFile(io.BytesIO(export_csv_zip([run], results)))

            self.assertEqual(sorted(archive.namelist()), ["droplets/study_s_opencv.csv", "summary.csv"])
            summary = archive.read("summary.csv").decode().splitlines()
            self.assertTrue(summary[0].startswith("project,sample,engine"))
            self.assertIn("study.admetp,s,opencv", summary[1])
            droplets = archive.read("droplets/study_s_opencv.csv").decode().splitlines()
            self.assertEqual(droplets[0], "kind,frame,droplet_id,centroid_x,centroid_y,equivalent_diameter,area")
            self.assertEqual(len(droplets), 10)

    def test_csv_export_leaves_out_droplets_switched_off_and_keeps_the_raw_count(self):
        rows = [{"engine": "cellpose", "item_id": "imgs", "kind": "droplet",
                 "values": {"frame": 1, "droplet_id": droplet, "inclusions": 1}} for droplet in (0, 1)]
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "study.admetp"
            (project / "analysis").mkdir(parents=True)
            (project / "analysis" / "raw.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
            run = StoredRun(project, "analysis", project / "analysis" / "raw.jsonl",
                            jobs=({"engine": "cellpose", "sample_id": "imgs", "file_id": "f2", "status": "complete",
                                   "source_path": "media/imgs", "metadata": {}},))
            fixes = {("cellpose", str(project / "media/imgs")): {"1:0": {"disabled": True},
                                                                  "1:1": {"inclusions": 1}}}

            archive = zipfile.ZipFile(io.BytesIO(export_csv_zip([run], file_results(run, rows, fixes), fixes)))

            lines = archive.read("droplets/study_imgs_cellpose.csv").decode().splitlines()
            self.assertEqual(lines, ["kind,frame,droplet_id,inclusions,inclusions_raw", "droplet,1,1,2,1"])


if __name__ == "__main__":
    unittest.main()
