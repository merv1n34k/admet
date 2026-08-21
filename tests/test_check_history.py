import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from admet.core.project import ProjectStore
from admet.workflows.check_history import (
    CHECK_INTERVAL_DAYS,
    CheckRecord,
    discover_checks,
    latest_check,
    load_check,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _record(recorded_at: str, kind: str = "flow") -> CheckRecord:
    return CheckRecord(kind, recorded_at, "", "project", Path("check.json"))


class CheckAgeTests(unittest.TestCase):
    def test_a_check_from_today_is_not_due(self):
        self.assertFalse(_record("2026-08-21T09:00:00+00:00").is_due(NOW))

    def test_a_check_older_than_the_interval_is_due(self):
        stale = _record("2026-08-01T09:00:00+00:00")

        self.assertTrue(stale.is_due(NOW))
        self.assertAlmostEqual(stale.age_days(NOW), 20.125, places=3)

    def test_the_interval_boundary_counts_as_due(self):
        boundary = _record("2026-08-14T12:00:00+00:00")

        self.assertEqual(CHECK_INTERVAL_DAYS, 7)
        self.assertTrue(boundary.is_due(NOW))

    def test_an_unreadable_date_counts_as_due(self):
        # An unreadable timestamp is not evidence that the rig was checked.
        undated = _record("not a date")

        self.assertTrue(undated.is_due(NOW))
        self.assertIsNone(undated.age_days(NOW))

    def test_a_naive_timestamp_is_read_as_utc_rather_than_rejected(self):
        self.assertFalse(_record("2026-08-21T09:00:00").is_due(NOW))


class CheckDiscoveryTests(unittest.TestCase):
    def _project(self, root: Path, name: str, checks: tuple[tuple[str, str], ...]) -> None:
        store = ProjectStore.create(root / f"{name}.admetp", name)
        for kind, recorded_at in checks:
            store.append_system_check(
                {"kind": kind, "recorded_at": recorded_at}, summary=f"{kind} from {name}"
            )

    def test_checks_are_found_across_every_project(self):
        # The rig is checked, not the project, so history cannot stop at the one
        # that happens to be open.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._project(root, "monday", (("flow", "2026-08-17T09:00:00+00:00"),))
            self._project(root, "friday", (("flow", "2026-08-21T09:00:00+00:00"),))

            records = discover_checks(root)

            self.assertEqual([record.project_id for record in records], ["friday", "monday"])

    def test_the_newest_check_of_each_kind_is_what_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._project(
                root,
                "rig",
                (
                    ("flow", "2026-08-10T09:00:00+00:00"),
                    ("flow", "2026-08-20T09:00:00+00:00"),
                    ("dispense", "2026-08-11T09:00:00+00:00"),
                ),
            )

            records = discover_checks(root)

            self.assertEqual(latest_check(records, "flow").recorded_at, "2026-08-20T09:00:00+00:00")
            self.assertEqual(
                latest_check(records, "dispense").recorded_at, "2026-08-11T09:00:00+00:00"
            )
            self.assertIsNone(latest_check(records, "nothing-like-this"))

    def test_a_discovered_check_can_be_read_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._project(root, "rig", (("flow", "2026-08-20T09:00:00+00:00"),))

            record = latest_check(discover_checks(root), "flow")

            self.assertTrue(record.path.is_file())
            self.assertEqual(load_check(record.path)["kind"], "flow")
            self.assertEqual(record.summary, "flow from rig")

    def test_an_empty_root_has_no_history_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(discover_checks(Path(tmpdir)), ())
            self.assertEqual(discover_checks(Path(tmpdir) / "nowhere"), ())

    def test_an_unreadable_check_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            broken = Path(tmpdir) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")

            self.assertEqual(load_check(broken), {})
            self.assertEqual(load_check(Path(tmpdir) / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
