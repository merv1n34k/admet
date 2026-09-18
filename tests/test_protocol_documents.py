"""Reading, validating and compiling protocol documents.

These tests are about documents only: nothing here touches hardware and no
protocol is run. What is pinned down is that a document means exactly what it
says, that anything else is refused with a message naming the place, and that
the same document always expands the same way.
"""

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from admet.workflows.protocols.compiler import (
    MAX_EXPANDED_STEPS,
    bind_parameters,
    compile_protocol,
)
from admet.workflows.protocols.loader import (
    ENV_PROTOCOL_PATH,
    content_hash,
    discover,
    find,
    read_protocol,
)
from admet.workflows.protocols.model import ProtocolError
from admet.workflows.protocols.validator import validate

MINIMAL = """
protocol_version: 1
id: minimal
name: Minimal protocol
capabilities:
  channels: [oil]
steps:
  - stop_channel: oil
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def _protocol(directory: Path, text: str = MINIMAL, name: str = "p.yaml"):
    report = validate(read_protocol(_write(directory, name, text)))
    if not report.ok:
        raise AssertionError("; ".join(str(error) for error in report.errors))
    return report.protocol


def _errors(directory: Path, text: str) -> list[str]:
    return [str(error) for error in validate(read_protocol(_write(directory, "p.yaml", text))).errors]


class LoadingTests(unittest.TestCase):
    def test_a_document_is_data_and_only_data(self):
        # A protocol file is input from outside the program. Nothing in it may
        # cause Python to be built, imported or called.
        with tempfile.TemporaryDirectory() as tmp:
            for hostile in (
                "!!python/object/apply:os.system ['echo pwned']",
                "!!python/name:os.system",
                "!!python/object/new:type",
            ):
                path = _write(Path(tmp), "hostile.yaml", hostile)
                with self.subTest(hostile=hostile):
                    with self.assertRaises(ProtocolError) as caught:
                        read_protocol(path)
                    self.assertIn("not valid YAML", str(caught.exception))

    def test_scalars_mean_what_they_look_like(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp),
                "scalars.yaml",
                """
                protocol_version: 1
                count: 42
                ratio: 1.87
                clock: 12:30
                date: 2026-08-30
                infinite: .inf
                """,
            )
            raw = read_protocol(path).raw

            self.assertEqual(raw["count"], 42)
            self.assertEqual(raw["ratio"], 1.87)
            # YAML 1.1 would read these as 750, a date and a float. None of them
            # is what the operator wrote, and a non-finite setpoint is not a value.
            self.assertEqual(raw["clock"], "12:30")
            self.assertEqual(raw["date"], "2026-08-30")
            self.assertEqual(raw["infinite"], ".inf")

    def test_a_broken_document_says_where_it_broke(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "broken.yaml", "steps:\n  - set_flow: {channel: oil\n")

            with self.assertRaises(ProtocolError) as caught:
                read_protocol(path)

            self.assertIn("line", str(caught.exception))
            self.assertIn(str(path), str(caught.exception))

    def test_an_empty_or_non_mapping_document_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text, reason in (
                ("empty.yaml", "", "is empty"),
                ("list.yaml", "- one\n- two\n", "must be a mapping"),
            ):
                with self.subTest(name=name):
                    with self.assertRaises(ProtocolError) as caught:
                        read_protocol(_write(Path(tmp), name, text))
                    self.assertIn(reason, str(caught.exception))

    def test_the_hash_follows_the_content_not_the_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            here = _write(Path(tmp), "here.yaml", MINIMAL)
            there = _write(Path(tmp), "there.yaml", MINIMAL)
            changed = _write(Path(tmp), "changed.yaml", MINIMAL + "description: now different\n")

            self.assertEqual(read_protocol(here).content_hash, read_protocol(there).content_hash)
            self.assertNotEqual(
                read_protocol(here).content_hash, read_protocol(changed).content_hash
            )
            self.assertTrue(read_protocol(here).content_hash.startswith("sha256:"))
            self.assertEqual(content_hash("x"), content_hash("x"))


class DiscoveryTests(unittest.TestCase):
    def test_the_nearest_definition_of_an_id_wins(self):
        # A project may correct a shipped protocol for one experiment without
        # anyone editing the installation.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            (project / "protocols").mkdir(parents=True)
            user = root / "user"
            user.mkdir()
            _write(project / "protocols", "p.yaml", MINIMAL + "description: from the project\n")
            _write(user, "p.yaml", MINIMAL + "description: from the user\n")

            source = find("minimal", project_dir=project, user_dirs=[user])

            self.assertEqual(source.origin, "project")
            self.assertEqual(source.raw["description"], "from the project")

    def test_a_broken_document_does_not_hide_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp)
            _write(user, "good.yaml", MINIMAL)
            _write(user, "broken.yaml", "{ not: valid")

            found = discover(user_dirs=[user])

            self.assertEqual([source.declared_id for source in found], ["minimal"])

    def test_the_search_path_can_be_extended_by_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp)
            _write(user, "p.yaml", MINIMAL)
            previous = os.environ.get(ENV_PROTOCOL_PATH)
            os.environ[ENV_PROTOCOL_PATH] = str(user)
            try:
                self.assertEqual(find("minimal").declared_id, "minimal")
            finally:
                if previous is None:
                    os.environ.pop(ENV_PROTOCOL_PATH, None)
                else:
                    os.environ[ENV_PROTOCOL_PATH] = previous

    def test_an_id_that_is_nowhere_says_where_it_looked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProtocolError) as caught:
                find("no_such_protocol", user_dirs=[Path(tmp)])

            self.assertIn("no_such_protocol", str(caught.exception))
            self.assertIn(tmp, str(caught.exception))


class VersionTests(unittest.TestCase):
    def test_a_future_version_is_refused_rather_than_guessed_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(Path(tmp), MINIMAL.replace("protocol_version: 1", "protocol_version: 2"))

            self.assertTrue(any("protocol_version" in error for error in errors))

    def test_a_missing_version_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(Path(tmp), MINIMAL.replace("protocol_version: 1\n", ""))

            self.assertTrue(any("protocol_version is required" in error for error in errors))


class RejectionTests(unittest.TestCase):
    def test_an_unknown_key_is_an_error_not_a_shrug(self):
        # Ignoring it would mean the operator wrote something that never happened
        # while the run reported success.
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(Path(tmp), MINIMAL + "nmae: typo\n")

            self.assertTrue(any("unknown key 'nmae'" in error for error in errors))

    def test_an_unknown_action_names_what_is_understood(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(
                Path(tmp), MINIMAL.replace("  - stop_channel: oil", "  - teleport: {to: mars}")
            )

            joined = " ".join(errors)
            self.assertIn("unknown action 'teleport'", joined)
            self.assertIn("set_flow", joined)

    def test_a_step_may_not_name_two_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(
                Path(tmp),
                MINIMAL.replace(
                    "  - stop_channel: oil",
                    "  - {set_flow: {channel: oil, value: 1}, stop_channel: oil}",
                ),
            )

            self.assertTrue(any("exactly one action" in error for error in errors))

    def test_a_channel_outside_the_declared_capabilities_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(Path(tmp), MINIMAL.replace("stop_channel: oil", "stop_channel: plasma"))

            self.assertTrue(any("not in capabilities.channels" in error for error in errors))

    def test_a_non_finite_setpoint_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (".inf", ".nan", "-.inf"):
                with self.subTest(value=value):
                    errors = _errors(
                        Path(tmp),
                        MINIMAL.replace(
                            "  - stop_channel: oil",
                            f"  - set_flow: {{channel: oil, value: {value}}}",
                        ),
                    )
                    self.assertTrue(any("must be a number" in error for error in errors), errors)

    def test_a_malformed_type_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(
                Path(tmp),
                MINIMAL.replace(
                    "steps:", "parameters:\n  vol:\n    type: quantity\nsteps:"
                ),
            )

            self.assertTrue(any("unknown type 'quantity'" in error for error in errors))

    def test_every_problem_is_reported_not_just_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(
                Path(tmp),
                """
                protocol_version: 1
                id: Bad-Id
                capabilities:
                  channels: [oil]
                  laser: true
                steps:
                  - teleport: {}
                """,
            )

            self.assertGreaterEqual(len(errors), 4)


class SubstitutionTests(unittest.TestCase):
    BASE = """
    protocol_version: 1
    id: subs
    name: Substitutions
    capabilities:
      channels: [oil]
    parameters:
      rate:
        type: number
        default: 250
    steps:
      - set_flow:
          channel: oil
          value: "%s"
    """

    def test_a_reference_is_replaced_by_the_value_it_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.BASE % "${rate}")

            result = compile_protocol(protocol)

            self.assertTrue(result.ok)
            self.assertEqual(result.steps[0].step.value, 250.0)

    def test_arithmetic_is_not_a_substitution(self):
        # The moment expressions are allowed, a document decides what runs. The
        # grammar is a name lookup and nothing else.
        with tempfile.TemporaryDirectory() as tmp:
            for hostile in (
                "${rate * 2}",
                "${rate.__class__}",
                "${__import__('os')}",
                "${rate; drop}",
            ):
                with self.subTest(hostile=hostile):
                    errors = _errors(Path(tmp), self.BASE % hostile)
                    self.assertTrue(
                        any("not a plain parameter name" in error for error in errors), errors
                    )

    def test_a_reference_to_something_undeclared_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = _errors(Path(tmp), self.BASE % "${not_declared}")

            self.assertTrue(any("not a parameter or loop variable" in error for error in errors))

    def test_a_loop_variable_is_in_scope_only_inside_its_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = """
            protocol_version: 1
            id: scope
            name: Scope
            capabilities:
              channels: [oil]
            steps:
              - foreach:
                  variable: step_mbar
                  values: [1, 2]
                  steps:
                    - set_pressure: {channel: oil, value: "${step_mbar}"}
              - set_pressure: {channel: oil, value: "${step_mbar}"}
            """

            errors = _errors(Path(tmp), outside)

            self.assertTrue(any("not a parameter or loop variable" in error for error in errors))


class ParameterTests(unittest.TestCase):
    DOC = """
    protocol_version: 1
    id: params
    name: Parameters
    capabilities:
      channels: [oil]
    parameters:
      oil_id:
        type: string
        required: true
      rate:
        type: number
        default: 250
        minimum: 1
        maximum: 1000
        unit: uL/min
      passes:
        type: integer
        default: 2
        minimum: 1
      heights:
        type: list[number]
        default: [-10, 0, 10]
    steps:
      - stop_channel: oil
    """

    def test_a_required_parameter_without_a_value_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.DOC)

            _values, problems = bind_parameters(protocol, {})

            self.assertTrue(any("is required" in str(problem) for problem in problems))

    def test_defaults_fill_in_what_was_not_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.DOC)

            values, problems = bind_parameters(protocol, {"oil_id": "HFE-7500"})

            self.assertEqual(problems, [])
            self.assertEqual(values["rate"], 250.0)
            self.assertEqual(values["passes"], 2)
            self.assertEqual(values["heights"], [-10.0, 0.0, 10.0])

    def test_bounds_are_enforced_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.DOC)

            for value, reason in ((0.5, "at least"), (5000, "at most")):
                with self.subTest(value=value):
                    _values, problems = bind_parameters(
                        protocol, {"oil_id": "x", "rate": value}
                    )
                    self.assertTrue(any(reason in str(problem) for problem in problems))

    def test_a_value_of_the_wrong_type_is_refused_not_converted(self):
        # "250" typed into a form is a string. Reading it as a number would mean
        # the run used a value the operator never wrote.
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.DOC)

            _values, problems = bind_parameters(protocol, {"oil_id": "x", "rate": "250"})

            self.assertTrue(any("must be a number" in str(problem) for problem in problems))

    def test_a_whole_number_is_accepted_where_a_number_is_wanted(self):
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.DOC)

            values, problems = bind_parameters(protocol, {"oil_id": "x", "rate": 300})

            self.assertEqual(problems, [])
            self.assertEqual(values["rate"], 300.0)

    def test_a_parameter_the_protocol_does_not_have_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.DOC)

            _values, problems = bind_parameters(protocol, {"oil_id": "x", "typo": 1})

            self.assertTrue(any("not a parameter" in str(problem) for problem in problems))


class ExpansionTests(unittest.TestCase):
    SWEEP = """
    protocol_version: 1
    id: sweep
    name: Sweep
    capabilities:
      channels: [oil]
    parameters:
      offsets:
        type: list[number]
        default: [-20, 0, 20]
    steps:
      - foreach:
          variable: offset_mbar
          values: "${offsets}"
          steps:
            - set_pressure: {channel: oil, value: "${offset_mbar}"}
            - wait_time: 5
      - stop_channel: oil
    """

    def test_a_loop_becomes_its_iterations_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compile_protocol(_protocol(Path(tmp), self.SWEEP))

            self.assertTrue(result.ok)
            self.assertEqual(
                [step.step.value for step in result.steps if step.kind == "SetPressure"],
                [-20.0, 0.0, 20.0],
            )
            self.assertEqual(result.steps[-1].kind, "StopChannel")

    def test_the_same_document_and_parameters_expand_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.SWEEP)

            first = compile_protocol(protocol)
            second = compile_protocol(protocol)

            self.assertEqual(
                [step.step_id for step in first.steps], [step.step_id for step in second.steps]
            )

    def test_a_step_id_says_which_line_it_came_from_and_which_time_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compile_protocol(_protocol(Path(tmp), self.SWEEP))

            pressures = [step for step in result.steps if step.kind == "SetPressure"]
            self.assertEqual(pressures[0].source_step_id, pressures[2].source_step_id)
            self.assertNotEqual(pressures[0].step_id, pressures[2].step_id)
            self.assertTrue(pressures[2].step_id.endswith("#0003"))

    def test_every_expanded_step_remembers_what_it_was_bound_to(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compile_protocol(_protocol(Path(tmp), self.SWEEP))

            pressures = [step for step in result.steps if step.kind == "SetPressure"]
            self.assertEqual(dict(pressures[1].bindings), {"offset_mbar": 0.0})

    def test_repeat_runs_its_block_the_stated_number_of_times(self):
        doc = """
        protocol_version: 1
        id: rep
        name: Repeat
        capabilities:
          channels: [oil]
        steps:
          - repeat:
              count: 3
              steps:
                - wait_time: 1
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = compile_protocol(_protocol(Path(tmp), doc))

            self.assertEqual(len(result.steps), 3)

    def test_a_matrix_visits_every_combination_in_declared_order(self):
        doc = """
        protocol_version: 1
        id: mat
        name: Matrix
        capabilities:
          channels: [oil]
        steps:
          - matrix:
              variables:
                height: [0, 10]
                offset: [-5, 5]
              steps:
                - set_pressure: {channel: oil, value: "${offset}"}
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = compile_protocol(_protocol(Path(tmp), doc))

            self.assertEqual(
                [dict(step.bindings) for step in result.steps],
                [
                    {"height": 0, "offset": -5},
                    {"height": 0, "offset": 5},
                    {"height": 10, "offset": -5},
                    {"height": 10, "offset": 5},
                ],
            )

    def test_parameters_change_the_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            protocol = _protocol(Path(tmp), self.SWEEP)

            result = compile_protocol(protocol, {"offsets": [1.0, 2.0]})

            self.assertEqual(
                [step.step.value for step in result.steps if step.kind == "SetPressure"],
                [1.0, 2.0],
            )

    def test_an_expansion_nobody_meant_to_start_is_refused(self):
        doc = """
        protocol_version: 1
        id: huge
        name: Huge
        capabilities:
          channels: [oil]
        steps:
          - repeat:
              count: 200
              steps:
                - repeat:
                    count: 200
                    steps:
                      - wait_time: 1
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = compile_protocol(_protocol(Path(tmp), doc))

            self.assertFalse(result.ok)
            self.assertTrue(any(str(MAX_EXPANDED_STEPS) in str(e) for e in result.errors))


if __name__ == "__main__":
    unittest.main()
