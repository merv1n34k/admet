import unittest
from types import SimpleNamespace

from admet.core.engine import EngineRegistry
from admet.ui.analyze import AnalyzeWorkflowView, MatrixRow, _uid
from admet.workflows import create_analyze_workflow


def _make_view() -> AnalyzeWorkflowView:
    return AnalyzeWorkflowView(
        create_analyze_workflow(),
        create_analyze_workflow().initial_state(),
        EngineRegistry(),
    )


def _row(engine: str = "opencv", sample: str = "s") -> MatrixRow:
    return MatrixRow(
        uid=_uid(),
        project_path="/tmp/study.admetp",
        source_path=f"/tmp/{sample}.avi",
        engine=engine,
        sample_id=sample,
    )


class UidUniquenessTests(unittest.TestCase):
    def test_uid_is_unique_across_rapid_calls(self):
        # Rows are created in tight loops (project load), so a millisecond-based id
        # collides and breaks per-row identity in the matrix table.
        uids = [_uid() for _ in range(2000)]
        self.assertEqual(len(set(uids)), len(uids))


class MatrixChangeTargetingTests(unittest.TestCase):
    def test_use_toggle_changes_only_the_targeted_row(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b"), _row(sample="c")]
        # every row must carry its own uid
        self.assertEqual(len({row.uid for row in view.matrix}), 3)

        target = view.matrix[2]
        view._handle_matrix_change(
            SimpleNamespace(args={"uid": target.uid, "field": "active", "value": False})
        )

        self.assertEqual([row.active for row in view.matrix], [True, True, False])

    def test_targets_excludes_deselected_rows(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b")]
        view.matrix[0].active = False

        targets = view._targets("opencv")

        self.assertEqual([row.sample_id for row in targets], ["b"])

    def test_selected_row_changes_structure_signature(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b")]

        view.selected_uid = view.matrix[0].uid
        first_signature = view._structure_signature()
        view.selected_uid = view.matrix[1].uid
        second_signature = view._structure_signature()

        self.assertNotEqual(first_signature, second_signature)
        self.assertIn(view.matrix[1].uid, second_signature)


if __name__ == "__main__":
    unittest.main()
