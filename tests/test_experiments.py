"""An experiment designed as a table of runs, each run and each sample with an id short enough to type."""

import json
import tempfile
import unittest
from pathlib import Path

from admet.core.experiments import (ALPHABET, Experiment, ExperimentStore, find_sample_id, new_code,
                                    sample_id)
from admet.workflows.json_protocol import run_fields, template_documents

TEMPLATES = template_documents()


def protocols(name):
    return TEMPLATES[name]


DESIGN = {"experiment": "ratio sweep 26 Nov", "protocol": "dropseq_3n",
          "runs": [{"parameters": {"oil_value": 300, "cells_value": 40, "beads_value": 40}},
                   {"parameters": {"oil_value": 250, "cells_value": 67, "beads_value": 67},
                    "condition": "boundary", "note": "near the bulge"},
                   {}]}


class IdTests(unittest.TestCase):
    def test_a_code_is_three_characters_nothing_can_be_mistaken_for(self):
        codes = {new_code() for _ in range(200)}
        self.assertTrue(all(len(code) == 3 and set(code) <= set(ALPHABET) for code in codes))
        self.assertFalse(set("".join(codes)) & set("01OIL"))
        self.assertNotIn(new_code(set(codes)), codes)

    def test_a_samples_id_is_found_in_a_folders_name_however_it_was_typed(self):
        for text in ("K7Q-03-r2", "K7Q-03-r2_set101.2026-11-26-14-03-11", "k7q_03_rep2 cells", "K7Q03r2",
                     "set101 K7Q.03.r2", "K7Q-003-r2"):
            self.assertEqual(find_sample_id(text), "K7Q-03-r2", text)
        self.assertEqual(find_sample_id("K7Q-12-r10"), "K7Q-12-r10")
        for text in ("set101.2026-11-26-14-03-11", "XK7Q-03-r2", "K7Q-3-r2", "K0Q-03-r2", "K7Q-03-r2345", ""):
            self.assertIsNone(find_sample_id(text), text)
        self.assertEqual(sample_id("K7Q-03", 2), "K7Q-03-r2")


class ExperimentTests(unittest.TestCase):
    def test_a_design_becomes_runs_with_ids_conditions_and_their_protocols_values(self):
        experiment = Experiment.from_json(DESIGN, protocols)
        code = experiment.code
        self.assertEqual([run.run_id(code) for run in experiment.runs], [f"{code}-01", f"{code}-02", f"{code}-03"])
        self.assertEqual([run.condition for run in experiment.runs], ["300/40/40", "boundary", ""])
        self.assertEqual(experiment.runs[1].note, "near the bulge")
        document = experiment.document(experiment.runs[1], protocols)
        self.assertEqual(document["run"], {"name": f"{code}-02", "id": f"{code}-02", "condition": "boundary",
                                           "experiment": "ratio sweep 26 Nov"})
        self.assertEqual(document["parameter_values"]["oil_value"], 250)
        self.assertEqual(run_fields(document["run"]), document["run"])                  # the protocol accepts it

    def test_a_design_with_a_mistake_names_the_run_and_takes_nothing(self):
        wrong = {**DESIGN, "runs": [DESIGN["runs"][0], {"parameters": {"oil_flow": 300}}]}
        with self.assertRaisesRegex(ValueError, "run 2"):
            Experiment.from_json(wrong, protocols)
        with self.assertRaisesRegex(ValueError, "run 1: no protocol"):
            Experiment.from_json({"runs": [{"protocol": "nothing"}]}, protocols)
        with self.assertRaisesRegex(ValueError, "run 1: no protocol"):
            Experiment.from_json({"runs": [{}]}, protocols)

    def test_the_table_comes_back_as_it_went_out_and_runs_move_on(self):
        experiment = Experiment.from_json(DESIGN, protocols)
        first, second = (run.run_id(experiment.code) for run in experiment.runs[:2])
        experiment.mark(first, "done", "20261126_140000_" + first.lower())
        self.assertEqual(experiment.next_planned().run_id(experiment.code), second)
        again = Experiment.from_json(json.loads(json.dumps(experiment.to_json())), protocols)
        self.assertEqual(again.to_json(), experiment.to_json())
        with self.assertRaises(ValueError):
            experiment.mark(first, "lost")
        with self.assertRaises(LookupError):
            experiment.mark("ZZZ-99", "done")

    def test_the_store_keeps_each_experiment_under_its_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperimentStore(tmp)
            experiment = Experiment.from_json(DESIGN, protocols, taken=store.codes())
            path = store.save(experiment)
            self.assertEqual(path, Path(tmp) / "experiments" / f"{experiment.code}.json")
            self.assertEqual(store.load(experiment.code, protocols).to_json(), experiment.to_json())
            self.assertEqual(store.list()[0]["code"], experiment.code)
            self.assertEqual((store.list()[0]["runs"], store.list()[0]["done"]), (3, 0))
            self.assertEqual(store.codes(), {experiment.code})


class RunBlockTests(unittest.TestCase):
    def test_the_run_block_takes_an_experiments_id_and_name_and_refuses_another_id(self):
        self.assertEqual(run_fields({"name": "K7Q-03", "id": "K7Q-03", "experiment": "sweep"}),
                         {"name": "K7Q-03", "id": "K7Q-03", "experiment": "sweep"})
        with self.assertRaisesRegex(ValueError, "like K7Q-03"):
            run_fields({"id": "run three"})

    def test_a_plan_carries_the_runs_id_and_experiment_into_its_record(self):
        from admet.core.service import Admet

        experiment = Experiment.from_json(DESIGN, protocols)
        document = experiment.document(experiment.runs[0], protocols)
        with tempfile.TemporaryDirectory() as tmp:
            plan = Admet(home=tmp).plan_protocol("run_json_protocol", {"protocol": document})
        self.assertEqual((plan["run_code"], plan["experiment"], plan["tag"]),
                         (f"{experiment.code}-01", "ratio sweep 26 Nov", f"{experiment.code}-01"))


class ServiceTests(unittest.TestCase):
    def test_a_saved_protocol_comes_before_a_template_and_experiments_are_kept_in_the_data_folder(self):
        from admet.core.service import Admet

        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet(home=tmp)
            self.assertEqual(admet.protocol_document("dropseq_3n")["name"], "dropseq_3n")
            with self.assertRaises(LookupError):
                admet.protocol_document("nothing")
            experiment = Experiment.from_json(DESIGN, admet.protocol_document)
            admet.experiments.save(experiment)
            self.assertTrue((admet.home.data_dir / "experiments" / f"{experiment.code}.json").is_file())


if __name__ == "__main__":
    unittest.main()
