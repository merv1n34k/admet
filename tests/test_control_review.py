"""The Review runs page: recorded pressure and flow from saved CSVs, offscreen."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from admet.ui import theme as ui  # noqa: E402
from admet.ui.review import ReviewPanel, project_run, read_fluidics_csv, step_markers  # noqa: E402
from tests.test_metrology import archive  # noqa: E402


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = archive(tmp.name, "gravimetry")

    def test_a_recorded_run_reads_its_channels_and_step_marks(self):
        run = project_run(self.run, "gravimetry")
        t, pressures, flows = read_fluidics_csv(self.run / "fluidics.csv")

        # The archive recorded channel 1 only: it stays channel 1, the others are empty.
        self.assertEqual(len(run.flows), 3)
        self.assertEqual(len(run.t), len(t))
        self.assertGreater(max(flows[1]), 10)
        self.assertTrue(all(map(lambda v: v != v, flows[0])))
        starts = [label for _t, label, _color in run.markers if label.startswith("▶")]
        ends = [label for _t, label, _color in run.markers if label.startswith("■")]
        self.assertEqual(len(starts), len(ends))
        self.assertIn("▶ 1", starts)
        self.assertTrue(all(0 <= at <= t[-1] + 5 for at, _label, _color in run.markers))

    def test_markers_skip_confirm_waits_and_run_level_events(self):
        events = [{"step_name": "a", "step_index": 0, "monotonic": 101, "outcome": "running", "confirmation_message": "ok?"},
                  {"step_name": "a", "step_index": 0, "monotonic": 102, "outcome": "running", "confirmation_message": ""},
                  {"step_name": "a", "step_index": 0, "monotonic": 105, "outcome": "cancelled"},
                  {"step_name": "", "monotonic": 106, "outcome": "cancelled"}]

        self.assertEqual([(at, label) for at, label, _ in step_markers(events, 100)], [(2, "▶ 1"), (5, "■ 1")])

    def test_runs_overlay_and_channels_toggle(self):
        panel = ReviewPanel()
        self.addCleanup(panel.close)
        panel.set_project(str(self.root))
        loaded = self.root / "droplegen_20261022_101500.csv"
        shutil.copy(self.run / "fluidics.csv", loaded)
        panel.add_files([str(loaded)])
        self.assertEqual(panel.table.rowCount(), 2)                         # the project run, then the file
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

        self.assertEqual(len(panel.shown_runs()), 2)
        self.assertEqual(len(panel.flow_plot.listDataItems()), 2)          # 2 runs x the one recorded channel
        self.assertNotEqual(panel.table.item(0, 1).foreground().color(), panel.table.item(1, 1).foreground().color())
        panel.channel_boxes[1].setChecked(False)
        self.assertEqual(len(panel.flow_plot.listDataItems()), 0)
        self.assertIn("2 run(s)", panel.status.text())

    def test_first_run_moves_whole_runs_and_stacked_overlays_every_step(self):
        import math
        import numpy as np
        from admet.ui.review import ReviewRun, placements, segments

        recorded = ReviewRun("run", np.arange(40.0, 500.0), [], [],
                             [(100.0, "▶ 1", ""), (300.0, "■ 1", ""), (320.0, "▶ 2", ""), (420.0, "■ 2", "")])
        loaded = ReviewRun("file", np.arange(30.0, 300.0), [], [])                   # no steps: one segment
        whole = (-math.inf, math.inf)

        self.assertEqual((recorded.anchor, loaded.anchor), (100.0, 30.0))
        self.assertEqual(segments(recorded), [(100.0, 300.0), (320.0, 420.0)])
        # Between runs only: each run whole, its first step on the first run's first step.
        self.assertEqual(placements([recorded, loaded], "first_run"), [[(*whole, 0.0)], [(*whole, 70.0)]])
        # Within runs too: every step of every run starts at 0.
        self.assertEqual(placements([recorded, loaded], "stacked"),
                         [[(100.0, 300.0, -100.0), (320.0, 420.0, -320.0)], [(30.0, 299.0, -30.0)]])
        self.assertEqual(placements([recorded, loaded], "recorded"), [[(*whole, 0.0)], [(*whole, 0.0)]])

    def test_stacked_draws_one_curve_per_step(self):
        panel = ReviewPanel()
        self.addCleanup(panel.close)
        panel.set_project(str(self.root))
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        steps = len([1 for _t, label, _c in panel.shown_runs()[0][1].markers if label.startswith("■")])

        self.assertEqual(len(panel.flow_plot.listDataItems()), 1)                   # first_run: the whole run
        panel.align.setCurrentIndex(panel.align.findData("stacked"))
        self.assertEqual(len(panel.flow_plot.listDataItems()), steps)
        self.assertFalse([i for i in panel.flow_plot.items() if getattr(i, "step_marker", False)])

    def test_crosshair_reads_and_pins_a_reference(self):
        panel = ReviewPanel()
        self.addCleanup(panel.close)
        crosshair = panel.crosshairs[panel.flow_plot]
        crosshair.pin(10.0, 40.0)
        crosshair.hover(12.5, 41.5)

        self.assertIn("t=12.50 s  41.50 µL/min", crosshair.label.toPlainText())
        self.assertIn("Δt=2.50 s  Δ=1.50 µL/min", crosshair.label.toPlainText())
        crosshair.clear()
        crosshair.hover(12.5, 41.5)
        self.assertNotIn("Δt", crosshair.label.toPlainText())

    def test_ctrl_wheel_zooms_a_plot_and_the_plain_wheel_scrolls_the_page(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QScrollArea
        from admet.ui.control import _WheelGuard

        scroll = QScrollArea()
        self.addCleanup(scroll.close)
        panel = ReviewPanel()
        scroll.setWidget(panel)
        guard = _WheelGuard(scroll)

        def wheel(modifiers):
            return QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120),
                               Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False)

        viewport = panel.flow_plot.viewport()
        self.assertFalse(guard.eventFilter(viewport, wheel(Qt.KeyboardModifier.ControlModifier)))
        self.assertFalse(guard.eventFilter(viewport, wheel(Qt.KeyboardModifier.MetaModifier)))
        self.assertTrue(guard.eventFilter(viewport, wheel(Qt.KeyboardModifier.NoModifier)))


if __name__ == "__main__":
    unittest.main()


def qc_run(root: Path, run_id: str = "run_q") -> Path:
    """A run with timestamps on its fluidics rows and a QC file: one 20 s collection of 1170 droplets a second."""
    import csv
    import json

    project = Path(root)
    directory = project / "records" / "protocols" / run_id
    directory.mkdir(parents=True)
    fluidics = project / "records" / "fluidics" / f"{run_id}.csv"
    fluidics.parent.mkdir(parents=True, exist_ok=True)
    with fluidics.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "elapsed_s", "pressure_0_mbar", "pressure_1_mbar", "pressure_2_mbar",
                         "flow_0_ul_min", "flow_1_ul_min", "flow_2_ul_min"])
        for tick in range(81):                                                  # 10:00:00 to 10:00:40, every 0.5 s
            seconds = tick * 0.5
            on = 10 <= seconds <= 30
            writer.writerow([f"10:00:{seconds:06.3f}", f"{seconds:.3f}", 1300 if on else 0, 420 if on else 0,
                             410 if on else 0, 300 if on else 0, 40 if on else 0, 40 if on else 0])
    qc = project / "records" / "camera" / f"{run_id}.qc.csv"
    qc.parent.mkdir(parents=True, exist_ok=True)
    with qc.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "elapsed_s", "recording_id", "step", "part", "rate_hz", "rate_half", "count",
                         "diameter", "diameter_unit", "diameter_half", "cv_percent", "cv_half", "n", "bursts",
                         "speed_px_frame", "spacing_px", "state"])
        for k, second in enumerate(range(11, 30)):
            state = "SETTLING" if k < 2 else "STABLE"
            writer.writerow([f"2026-11-17T10:00:{second:02d}.100", f"{k + 1}.000", f"{run_id}_step01", 1, 1,
                             1170, 21 if k > 2 else "", 1170 * (k + 1), 124.0, "um", 0.5, 2.7, 0.3, 200, k + 1, 48, 128, state])
    events = [{"step_index": 0, "step_name": "collect", "state": "running", "outcome": "running", "monotonic": 110},
              {"step_index": 0, "step_name": "collect", "state": "running", "outcome": "completed", "monotonic": 130}]
    (directory / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (directory / "summary.json").write_text(json.dumps({
        "run_id": run_id, "state": "completed", "executed_at": "2026-11-17T10:00:00", "completed_at": "2026-11-17T10:00:40",
        "artifacts": {"fluidics_csv": f"records/fluidics/{run_id}.csv", "qc_csv": f"records/camera/{run_id}.qc.csv",
                      "polling_origin_monotonic": 100, "recording_closed": True}}))
    return directory


class DropletQCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = qc_run(self.root)

    def test_the_qc_rows_land_on_the_fluidics_clock_with_the_expected_rate_from_the_flows(self):
        from admet.core.droplets import expected_rate_hz

        run = project_run(self.run, "run_q")

        self.assertIsNotNone(run.qc)
        self.assertEqual([round(t, 1) for t in run.qc.t[:3]], [11.1, 12.1, 13.1])            # wall clock → elapsed_s
        self.assertEqual(run.qc.diameter_unit, "um")
        self.assertAlmostEqual(run.qc.expected[5], expected_rate_hz(80.0, 124.0), delta=1)   # cells + beads, no oil
        self.assertTrue(all(s == "STABLE" for s in run.qc.state[2:]))
        self.assertEqual(run.qc.recording[0], "run_q_step01")
        self.assertEqual([(at, label) for at, label, _ in run.markers], [(10, "▶ 1"), (30, "■ 1")])

    def test_a_row_past_midnight_lands_on_the_next_day(self):
        from admet.ui.review import read_qc_csv

        path = self.root / "late.qc.csv"
        path.write_text("timestamp,rate_hz,rate_half,count,diameter,diameter_unit,cv_percent,cv_half,state,recording_id\n"
                        "2026-11-18T00:00:05.000,1000,10,5,120,um,3,0.3,STABLE,r\n")
        qc = read_qc_csv(path, (23 * 3600 + 59 * 60 + 55.0, 500.0))               # fluidics began at 23:59:55 = 500 s
        self.assertAlmostEqual(qc.t[0], 510.0)

    def test_the_page_draws_the_rate_its_band_the_expected_line_and_the_cv_and_fills_the_columns(self):
        from admet.ui.review import qc_cells

        panel = ReviewPanel()
        self.addCleanup(panel.close)
        panel.set_project(str(self.root))
        self.assertEqual(panel.table.rowCount(), 1)
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

        run = panel.shown_runs()[0][1]
        self.assertEqual(len(panel.droplet_plot.listDataItems()), 2)            # the rate and the expected line
        self.assertTrue(any(getattr(item, "qc_item", False) for item in panel.droplet_plot.items()))   # the band
        self.assertEqual(len(panel.cv_plot.listDataItems()), 2)                 # SETTLING, then STABLE
        self.assertEqual(qc_cells(run.qc), ("22 230", "1170", "2.7 STABLE"))
        self.assertEqual([panel.table.item(0, c).text() for c in (6, 7, 8)], ["22 230", "1170", "2.7 STABLE"])
        self.assertEqual(panel.table.horizontalHeaderItem(8).text(), "CV, %")
        self.assertEqual([label.text for _s, label in panel.legends[panel.flow_plot].items],
                         ["run_q · Oil L", "run_q · Cells M", "run_q · Beads M"])
        self.assertEqual([label.text for _s, label in panel.legends[panel.droplet_plot].items],
                         ["run_q · rate ± 95 %", "run_q · expected from the flows"])
        self.assertEqual([label.text for _s, label in panel.legends[panel.cv_plot].items],
                         ["STABLE", "UNSTABLE", "SETTLING", "5 % limit"])
        grid = panel.layout().itemAt(3).layout()                                  # pressure, CV / flow, rate
        at = lambda r, c: grid.itemAtPosition(r, c).widget()
        self.assertEqual((at(0, 0), at(0, 1), at(1, 0), at(1, 1)),
                         (panel.pressure_plot, panel.cv_plot, panel.flow_plot, panel.droplet_plot))

        panel.align.setCurrentIndex(1)                                           # stacked: the step from 0
        self.assertEqual(len(panel.droplet_plot.listDataItems()), 2)
        self.assertEqual(len(panel.legends[panel.flow_plot].items), 3)           # still one entry a run and channel
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(len(panel.droplet_plot.listDataItems()), 0)
        self.assertFalse(any(getattr(item, "qc_item", False) for item in panel.droplet_plot.items()))
