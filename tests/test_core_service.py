"""Core owns the session, and engines are told where to write.

The property being pinned down is that data cannot land outside a project by
accident: an action that writes needs a session, gets its paths from core, and
what it produced is in the manifest afterwards.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from admet.core.engine import ActionSpec
from admet.core.run import RunJob, RunResult
from admet.core.service import Admet, NoProject
from admet.core.session import load_session


class RecordingEngine:
    """An engine that writes only where it is told, which is the contract."""

    id = "acquisition"
    name = "Fake acquisition"
    actions = (
        ActionSpec("noop", "Does nothing", "diagnostics"),
        ActionSpec(
            "start_recording",
            "Start Recording",
            "recording",
            params=("recording_label",),
            outputs=("video", "fluidics_csv"),
        ),
        ActionSpec("stop_recording", "Stop Recording", "recording", artifact="control_recording"),
    )

    def __init__(self):
        self.settings = _EmptySchema()
        self.jobs: list[RunJob] = []

    def observation(self) -> dict:
        """Part of the engine contract core reads when it records context."""
        return {"connection": {"fluidics": True, "simulated": True}, "channels": []}

    def run(self, job: RunJob) -> RunResult:
        self.jobs.append(job)
        if job.action == "start_recording":
            for path in job.outputs.values():
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text("data", encoding="utf-8")
            return RunResult(job.id, self.id, job.action)
        if job.action == "stop_recording":
            started = self.jobs[0]
            return RunResult(
                job.id,
                self.id,
                job.action,
                metadata={
                    "recording": {
                        "recording_id": started.metadata["recording_id"],
                        "video_path": str(started.outputs["video"]),
                        "fluidics_csv": str(started.outputs["fluidics_csv"]),
                    }
                },
            )
        return RunResult(job.id, self.id, job.action)


class _EmptySchema:
    params = ()


def _admet(tmp: str) -> tuple[Admet, RecordingEngine]:
    admet = Admet()
    engine = RecordingEngine()
    admet._engines["acquisition"] = engine
    admet.create_project(Path(tmp) / "rig.admetp")
    return admet, engine


class SessionTests(unittest.TestCase):
    def test_a_failed_save_leaves_the_project_readable(self):
        from admet.core.project import ProjectStore

        with tempfile.TemporaryDirectory() as tmp:
            store = Admet().create_project(Path(tmp) / "safe.admetp")
            before = (store.path / "manifest.json").read_text()

            with self.assertRaises(TypeError):
                store.update_metadata(unwritable=object())

            self.assertEqual((store.path / "manifest.json").read_text(), before)
            ProjectStore(store.path)
            self.assertEqual(list(store.path.glob(".manifest.json.*")), [])

    def test_a_recording_closed_by_emergency_stop_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "estop.admetp"
            admet = Admet()
            admet.create_project(path)
            admet.do("connect_fluidics", {"simulated": True})
            self.addCleanup(admet.do, "disconnect_fluidics")
            admet.do("start_recording", {"recording_label": "estop"})
            time.sleep(0.3)

            admet.do("emergency_stop")

            listed = json.loads((path / "manifest.json").read_text())["files"]
            on_disk = list(path.rglob("*.csv"))
            self.assertEqual(len(on_disk), 1)
            self.assertEqual([f["role"] for f in listed], ["control_fluidics_csv"])

    def test_creating_an_existing_project_opens_it_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exp.admetp"
            admet = Admet()
            admet.create_project(path)
            admet.do("connect_fluidics", {"simulated": True})
            self.addCleanup(admet.do, "disconnect_fluidics")
            admet.do("start_recording", {"recording_label": "kept"})
            time.sleep(0.3)
            admet.do("stop_recording")
            before = json.loads((path / "manifest.json").read_text())

            reopened = Admet().create_project(path, "other")

            after = json.loads((path / "manifest.json").read_text())
            self.assertEqual(after, before)
            self.assertEqual(reopened.session.project_id, before["project_id"])
            self.assertEqual(len(after["files"]), 1)

    def test_a_project_is_created_and_becomes_the_one_in_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()

            store = admet.create_project(Path(tmp) / "rig.admetp")

            self.assertTrue((store.path / "manifest.json").is_file())
            self.assertTrue(admet.describe_project()["open"])
            self.assertEqual(admet.describe_project()["project_id"], "rig")

    def test_opening_something_that_is_not_a_project_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(NoProject) as caught:
                Admet().open_project(tmp)

            self.assertIn("not a project", str(caught.exception))

    def test_with_no_project_open_nothing_claims_to_be_open(self):
        self.assertEqual(Admet().describe_project(), {"open": False})


class PathOwnershipTests(unittest.TestCase):
    def test_an_action_that_writes_is_refused_without_a_session(self):
        # Otherwise the files land somewhere no manifest mentions, which is the
        # same as losing them.
        admet = Admet()
        admet._engines["acquisition"] = RecordingEngine()

        with self.assertRaises(NoProject) as caught:
            admet.run("acquisition", "start_recording", {"recording_label": "set01"})

        self.assertIn("needs a project open", str(caught.exception))

    def test_core_hands_the_engine_its_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet, engine = _admet(tmp)

            admet.run("acquisition", "start_recording", {"recording_label": "set01"})

            job = engine.jobs[-1]
            self.assertEqual(set(job.outputs), {"video", "fluidics_csv"})
            for path in job.outputs.values():
                self.assertTrue(str(path).startswith(str(admet.project.path)))
            self.assertIn("set01", job.metadata["recording_id"])

    def test_an_action_that_writes_nothing_is_given_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet, engine = _admet(tmp)

            admet.run("acquisition", "noop")

            self.assertEqual(engine.jobs[-1].outputs, {})

    def test_the_engine_is_told_which_session_it_is_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet, engine = _admet(tmp)

            admet.run("acquisition", "noop")

            self.assertEqual(engine.jobs[-1].metadata["session_id"], "rig")
            self.assertEqual(engine.jobs[-1].metadata["workdir"], str(admet.project.path))


class FluidicsOnlyRecordingTests(unittest.TestCase):
    """System validation must not need a camera pointed at the chip."""

    def _recorded(self, tmp):
        admet = Admet()
        admet.create_project(Path(tmp) / "rig.admetp")
        admet.do("connect_fluidics", {"simulated": True})
        admet.do("apply_corrections", {"oil_l_scale": 1.07})
        started = admet.do("start_recording", {"recording_label": "oilcap"})
        time.sleep(0.5)
        admet.do("stop_recording")
        admet.do("disconnect_fluidics")
        return admet, started

    def test_a_recording_starts_with_no_camera_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet, started = self._recorded(tmp)

            self.assertFalse(admet.state()["camera"])
            self.assertTrue(started["csv_path"])

    def test_the_csv_path_is_known_as_soon_as_it_starts(self):
        # The operator needs somewhere to look while the run is going, not
        # after it has finished.
        with tempfile.TemporaryDirectory() as tmp:
            _admet_service, started = self._recorded(tmp)

            self.assertTrue(Path(started["csv_path"]).is_file())

    def test_the_fluidics_log_has_rows_in_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _admet_service, started = self._recorded(tmp)

            self.assertGreater(len(Path(started["csv_path"]).read_text().splitlines()), 1)

    def test_no_video_is_claimed_that_was_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet, _started = self._recorded(tmp)

            session = load_session(admet.project.path)
            recorded = json.loads(
                (admet.project.path / "records" / "metadata.json").read_text()
            )["recordings"][0]

            self.assertEqual([p for p in admet.project.path.rglob("*.avi")], [])
            self.assertEqual({f.role for f in session.files}, {"control_fluidics_csv"})
            self.assertEqual(recorded["video_path"], "")
            self.assertEqual(recorded["video_candidates"], [])

    def test_what_was_true_during_the_run_is_kept_with_it(self):
        # Flows measured under different correction factors are not comparable,
        # and a measurement without its channel mapping is not reproducible.
        with tempfile.TemporaryDirectory() as tmp:
            admet, _started = self._recorded(tmp)

            context = json.loads(
                (admet.project.path / "records" / "metadata.json").read_text()
            )["recordings"][0]["context"]

            self.assertEqual(context["corrections"]["oil_l_scale"], 1.07)
            self.assertEqual(context["channels"][0]["label"], "Oil L")
            self.assertIsNotNone(context["channels"][0]["pressure_max_mbar"])
            self.assertTrue(context["software_version"])
            self.assertTrue(context["connection"]["simulated"])


class RegistrationTests(unittest.TestCase):
    def test_what_an_action_produced_ends_up_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet, _engine = _admet(tmp)

            admet.run("acquisition", "start_recording", {"recording_label": "set01"})
            admet.run("acquisition", "stop_recording")

            session = load_session(admet.project.path)
            roles = {file.role for file in session.files}
            self.assertEqual(roles, {"control_video", "control_fluidics_csv"})
            self.assertTrue(any(item.id == "acq-records" for item in session.items))

    def test_the_registration_survives_a_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet, _engine = _admet(tmp)
            admet.run("acquisition", "start_recording", {"recording_label": "set01"})
            admet.run("acquisition", "stop_recording")

            reopened = Admet()
            reopened.open_project(admet.project.path)

            self.assertEqual(len(reopened.describe_project()["files"]), 2)

    def test_an_action_with_nothing_to_leave_behind_registers_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet, _engine = _admet(tmp)

            admet.run("acquisition", "noop")

            self.assertEqual(load_session(admet.project.path).files, ())


class PhantomFileTests(unittest.TestCase):
    def test_a_recording_without_a_camera_claims_no_video(self):
        # Both paths are allocated whether or not a camera is attached. A manifest
        # that lists a file which was never written is worse than one that omits
        # it, because everything downstream believes it.
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            store = admet.create_project(Path(tmp) / "rig.admetp")
            admet.run("acquisition", "connect_fluidics", {"simulated": True, "start_polling": True})
            admet.run("acquisition", "start_recording", {"recording_label": "csvonly"})
            time.sleep(0.6)
            admet.run("acquisition", "stop_recording")
            time.sleep(0.3)
            admet.run("acquisition", "disconnect_fluidics")

            session = load_session(store.path)

            roles = {file.role for file in session.files}
            self.assertIn("control_fluidics_csv", roles)
            self.assertNotIn("control_video", roles)
            for file in session.files:
                self.assertTrue(
                    (store.path / file.path).is_file(), f"{file.path} is in the manifest"
                )


class RoutingTests(unittest.TestCase):
    def test_an_unknown_engine_names_the_ones_there_are(self):
        with self.assertRaises(LookupError) as caught:
            Admet().engine("nonsense")

        self.assertIn("acquisition", str(caught.exception))

    def test_describe_reports_what_an_action_writes(self):
        admet = Admet()
        admet._engines["acquisition"] = RecordingEngine()

        described = {action["id"]: action for action in admet.describe("acquisition")["actions"]}

        self.assertEqual(described["start_recording"]["outputs"], ["video", "fluidics_csv"])
        self.assertEqual(described["stop_recording"]["artifact"], "control_recording")


class RealEngineTests(unittest.TestCase):
    """The same contract, against the acquisition engine itself, simulated."""

    def test_a_simulated_recording_lands_in_the_project_and_is_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            store = admet.create_project(Path(tmp) / "rig.admetp")
            admet.run("acquisition", "connect_fluidics", {"simulated": True, "start_polling": True})

            admet.run("acquisition", "start_recording", {"recording_label": "set01"})
            time.sleep(0.6)
            admet.run("acquisition", "stop_recording")
            time.sleep(0.3)

            written = [path for path in store.path.rglob("*.csv") if path.is_file()]
            self.assertTrue(written, "the fluidics log should be inside the project")
            self.assertTrue(str(written[0]).startswith(str(store.path)))
            session = load_session(store.path)
            self.assertTrue(session.files)
            admet.run("acquisition", "disconnect_fluidics")


if __name__ == "__main__":
    unittest.main()


class AnalysisTests(unittest.TestCase):
    """The analysis side goes through core the same way acquisition does."""

    def _video(self, directory: Path) -> Path:
        # A file with a video suffix is enough: nothing here decodes it.
        path = directory / "sample.avi"
        path.write_bytes(b"not really a video")
        return path

    def test_something_to_analyse_is_registered_with_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            store = admet.create_project(Path(tmp) / "rig.admetp")

            added = admet.do("add_source", {"path": str(self._video(Path(tmp)))})

            self.assertEqual(added["engine"], "opencv")
            self.assertEqual(added["role"], "analysis_video")
            session = load_session(store.path)
            self.assertEqual([file.role for file in session.files], ["analysis_video"])

    def test_the_engine_is_inferred_from_what_was_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(Path(tmp) / "rig.admetp")
            images = Path(tmp) / "frames"
            images.mkdir()

            video = admet.do("add_source", {"path": str(self._video(Path(tmp)))})
            directory = admet.do("add_source", {"path": str(images)})

            self.assertEqual(video["engine"], "opencv")
            self.assertEqual(directory["engine"], "cellpose")

    def test_adding_something_that_is_not_there_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(Path(tmp) / "rig.admetp")

            with self.assertRaises(Exception) as caught:
                admet.do("add_source", {"path": f"{tmp}/nothing.avi"})

            self.assertIn("does not exist", str(caught.exception))

    def test_analysing_with_nothing_to_analyse_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(Path(tmp) / "rig.admetp")

            with self.assertRaises(Exception) as caught:
                admet.do("run_analysis", {})

            self.assertIn("nothing to analyse", str(caught.exception))

    def test_analysing_without_a_project_is_refused(self):
        with self.assertRaises(Exception) as caught:
            Admet().do("run_analysis", {})

        self.assertIn("no project is open", str(caught.exception))

    def test_sources_lists_what_was_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(Path(tmp) / "rig.admetp")
            admet.do("add_source", {"path": str(self._video(Path(tmp))), "sample_id": "run01"})

            listed = admet.do("list_sources", {})["sources"]

            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["sample_id"], "run01")
            self.assertEqual(listed[0]["engine"], "opencv")
