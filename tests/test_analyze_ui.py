import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from admet.core.engine import EngineRegistry
from admet.core.project import ProjectStore
from admet.core.session import SessionItem
from admet.ui.analyze import (
    AnalyzeWorkflowView,
    MatrixRow,
    _addable,
    _browser_entries,
    _droplet_at,
    _roi_from_drag,
    _uid,
    _usable_cache_root,
    apply_click,
)
from admet.workflows import create_analyze_workflow


def _make_view(recent: dict[str, str] | None = None) -> AnalyzeWorkflowView:
    return AnalyzeWorkflowView(create_analyze_workflow(), create_analyze_workflow().initial_state(),
                               EngineRegistry(), recent=recent if recent is not None else {})


def _row(engine: str = "opencv", sample: str = "s", **settings) -> MatrixRow:
    return MatrixRow(uid=_uid(), project_path="/tmp/study.admetp", source_path=f"/tmp/{sample}.avi",
                     engine=engine, sample_id=sample, settings=dict(settings))


class WorkflowShapeTests(unittest.TestCase):
    def test_three_stages_with_their_actions(self):
        workflow = create_analyze_workflow()

        self.assertEqual([stage.id for stage in workflow.stages], ["files", "analyse", "results"])
        self.assertEqual([action.label for action in workflow.stages[1].actions],
                         ["Run all ticked", "Run selected", "Reset file settings"])
        self.assertEqual([action.label for action in workflow.stages[2].actions], ["Export CSV", "Export plots"])


class UidUniquenessTests(unittest.TestCase):
    def test_uid_is_unique_across_rapid_calls(self):
        uids = [_uid() for _ in range(2000)]
        self.assertEqual(len(set(uids)), len(uids))


class MatrixChangeTests(unittest.TestCase):
    def test_use_toggle_changes_only_the_targeted_row(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b"), _row(sample="c")]
        target = view.matrix[2]

        view._handle_matrix_change(SimpleNamespace(args={"uid": target.uid, "field": "active", "value": False}))

        self.assertEqual([row.active for row in view.matrix], [True, True, False])
        self.assertEqual([row.sample_id for row in view._targets("opencv")], ["a", "b"])

    def test_a_scale_typed_in_the_table_lands_on_the_row(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(engine="cellpose", sample="imgs")]

        view._handle_matrix_change(SimpleNamespace(args={"uid": view.matrix[0].uid, "field": "microns_per_pixel",
                                                         "value": "1.5"}))
        view._handle_matrix_change(SimpleNamespace(args={"uid": view.matrix[1].uid, "field": "microns_per_pixel",
                                                         "value": 2}))

        self.assertEqual(view.matrix[0].settings["microns_per_pixel"], 1.5)
        self.assertEqual(view.matrix[1].settings["px_to_um"], 2.0)   # the imaging engine's name for it

    def test_selected_row_changes_structure_signature(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b")]

        view.selected_uid = view.matrix[0].uid
        first = view._structure_signature()
        view.selected_uid = view.matrix[1].uid

        self.assertNotEqual(first, view._structure_signature())


class ReadinessTests(unittest.TestCase):
    def test_run_waits_for_scale_and_fps_on_every_ticked_file(self):
        view = _make_view()
        view.matrix = [_row(sample="a", microns_per_pixel=1.2, fps=200), _row(sample="b", microns_per_pixel=1.2)]

        view.state = view.state.__class__(index=1, statuses=view.state.statuses)   # on 2. Analyse

        self.assertTrue(view._guard_value("has_targets"))
        self.assertFalse(view._guard_value("targets_ready"))
        self.assertIn("1 missing µm/px or FPS", view._progress()[1])
        view.matrix[1].settings["fps"] = 200
        self.assertTrue(view._guard_value("targets_ready"))
        self.assertEqual(view._progress()[1], "2 ticked · 0 analysed")

    def test_an_image_folder_needs_only_its_scale(self):
        view = _make_view()
        view.matrix = [_row(engine="cellpose", sample="imgs")]
        self.assertFalse(view._row_ready(view.matrix[0]))
        view.matrix[0].settings["px_to_um"] = 1.14
        self.assertTrue(view._row_ready(view.matrix[0]))

    def test_reset_keeps_scale_and_fps_and_restores_detection_defaults(self):
        view = _make_view()
        view.matrix = [_row(sample="a", microns_per_pixel=1.2, fps=200, k_sigma=9, gate_pct=80)]
        view.selected_uid = view.matrix[0].uid

        view._reset_row_settings()

        settings = view.matrix[0].settings
        self.assertEqual((settings["microns_per_pixel"], settings["fps"]), (1.2, 200))
        self.assertEqual((settings["k_sigma"], settings["gate_pct"]), (4.0, 50.0))


class ProjectTests(unittest.TestCase):
    def test_the_last_opened_project_that_still_exists_is_reopened(self):
        with tempfile.TemporaryDirectory() as tmp:
            older = ProjectStore.create(Path(tmp) / "older", "older").path
            newer = ProjectStore.create(Path(tmp) / "newer", "newer").path
            recent = {str(older): "2026-11-01T10:00:00+00:00", str(newer): "2026-11-02T10:00:00+00:00",
                      str(Path(tmp) / "gone.admetp"): "2026-11-03T10:00:00+00:00"}

            self.assertEqual(_make_view(recent).last_project(), newer)

    def test_opening_the_same_project_again_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectStore.create(Path(tmp) / "study", "study")
            view = _make_view()
            view._load_project(store.path)
            lines = len(view.action_log)

            view._select_project(str(store.path))

            self.assertEqual(len(view.action_log), lines)

    def test_removing_a_row_forgets_the_file_in_the_project_but_not_a_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectStore.create(Path(tmp) / "study", "study")
            video = Path(tmp) / "clip.avi"
            video.write_bytes(b"")
            recording = store.path / "records" / "cam.avi"
            recording.parent.mkdir(parents=True, exist_ok=True)
            recording.write_bytes(b"")
            store.upsert_file_path(recording, role="control_video")
            store.save()
            view = _make_view()
            view._load_project(store.path)
            view._append_source(video)
            store = ProjectStore(store.path)
            clip = next(file for file in store.session.files if file.role == "analysis_video")
            # An analysis run leaves a sample item pointing at the file; it must go with the file.
            store.session = replace(store.session, items=(SessionItem("clip", "analysis", "opencv", files=(clip.id,)),))
            store.save()

            self.assertEqual([row.removable for row in view.matrix], [False, True])
            view._remove_row(view.matrix[1])
            view._remove_row(view.matrix[0])

            self.assertEqual([Path(row.source_path).name for row in view.matrix], ["cam.avi"])
            store = ProjectStore(store.path)
            self.assertEqual([file.role for file in store.session.files], ["control_video"])
            self.assertEqual(store.session.items, ())


class CacheRootTests(unittest.TestCase):
    def test_another_machines_cache_folder_is_not_used_here(self):
        own = str(Path.home() / ".admet-cache")
        self.assertEqual(_usable_cache_root(r"C:\Users\ADMIN\.admet-cache"), own)
        self.assertEqual(_usable_cache_root("relative/cache"), own)
        self.assertEqual(_usable_cache_root(None), own)
        self.assertEqual(_usable_cache_root(str(Path(tempfile.gettempdir()) / "cache")),
                         str(Path(tempfile.gettempdir()) / "cache"))


class FileDialogTests(unittest.TestCase):
    def test_only_videos_images_and_folders_with_images_can_be_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clip.avi").write_bytes(b"")
            (root / "notes.txt").write_text("x")
            (root / "analysis").mkdir()
            (root / "imgs").mkdir()
            (root / "imgs" / "a.png").write_bytes(b"")

            entries = _browser_entries(root, "source")

            self.assertEqual([entry.name for entry in entries], ["analysis", "imgs", "clip.avi"])
            self.assertEqual([_addable(entry) for entry in entries], [False, True, True])


class CropTests(unittest.TestCase):
    def test_a_drag_gives_a_clamped_rectangle_and_a_click_gives_nothing(self):
        self.assertEqual(_roi_from_drag(50, 40, 10, 90, 640, 120), (10, 40, 40, 50))
        self.assertEqual(_roi_from_drag(600, 100, 700, 200, 640, 120), (600, 100, 40, 20))
        self.assertIsNone(_roi_from_drag(50, 40, 52, 41, 640, 120))


class ImagingCorrectionTests(unittest.TestCase):
    def test_clicks_switch_a_droplet_off_and_on_and_change_its_inclusions(self):
        fix = apply_click(None)
        self.assertEqual(fix, {"disabled": True})
        self.assertEqual(apply_click(fix), {})                                   # back on: nothing left
        fix = apply_click(None, shift=True)
        fix = apply_click(fix, shift=True)
        self.assertEqual(fix, {"inclusions": 2})
        self.assertEqual(apply_click(apply_click(fix, alt=True), alt=True), {})

    def test_the_droplet_under_the_pointer_is_the_nearest_one_holding_it(self):
        droplets = [{"droplet_id": 0, "center_x": 100, "center_y": 100, "diameter_px": 60},
                    {"droplet_id": 1, "center_x": 150, "center_y": 100, "diameter_px": 60}]
        self.assertEqual(_droplet_at(droplets, 128, 100)["droplet_id"], 1)
        self.assertEqual(_droplet_at(droplets, 80, 90)["droplet_id"], 0)
        self.assertIsNone(_droplet_at(droplets, 300, 300))


class PathLabelTests(unittest.TestCase):
    def test_a_windows_path_is_shown_whole_on_hover(self):
        from nicegui import Client
        from nicegui.page import page

        from admet.ui.analyze import _path_label

        path = r"C:\Users\ADMIN\Projects\admet\projects\p.admetp\records\camera\run_ab12_step01_20261112_171440.avi"
        with Client(page(""), request=None):
            label = _path_label(path)
        self.assertEqual(label.props["title"], path)


if __name__ == "__main__":
    unittest.main()
