"""An experiment: the runs it is made of, designed in one table, each with an id short enough to type.

An experiment has a code of three characters (no 0/O, 1/I/L to mistake), and each of its runs a
number: run 3 of experiment K7Q is K7Q-03. A run makes replicates (its protocol's steps named
repNN), and each collected replicate is a sample: K7Q-03-r2. That id goes into the run's record
through its protocol's run block, and onto whatever is collected from it, such as the folder of
microscopy images, so analyze can put the two together without being told.

A run is a row: its protocol, the values of that protocol's parameters, its condition, a note, and
where it got to. The table can come whole from a JSON document, which the store keeps under the
data folder as experiments/<code>.json.
"""

from __future__ import annotations

import json
import re
import secrets
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from admet.core.files import write_atomic

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"     # no 0 O 1 I L: nothing to mistake when typed
CODE_LENGTH = 3
STATES = ("planned", "running", "done", "failed")
EXPERIMENTS_DIR = "experiments"
_CODE = f"[{ALPHABET}]{{{CODE_LENGTH}}}"
RUN_ID = re.compile(rf"{_CODE}-\d{{2,3}}")
# A sample's id as typed into a folder's name: any case, the parts joined by -, _, . or a space, the
# replicate as r2 or rep2; not part of a longer run of letters and digits.
SAMPLE_IN_TEXT = re.compile(rf"(?<![A-Za-z0-9])({_CODE})[-_. ]?(\d{{2,3}})[-_. ]?r(?:ep)?(\d{{1,2}})(?![0-9])",
                            re.IGNORECASE)

Protocols = Callable[[str], dict[str, Any]]      # a protocol's name -> its document


def new_code(taken: set[str] | None = None) -> str:
    """A code no experiment in TAKEN has."""
    taken = taken or set()
    for _ in range(1000):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if code not in taken:
            return code
    raise RuntimeError("no free experiment code left")


def sample_id(run_id: str, replicate: int) -> str:
    return f"{run_id}-r{int(replicate)}"


def find_sample_id(text: str) -> str | None:
    """The sample's id written in TEXT (a folder's name), as ADMET writes it: K7Q-03-r2; None when
    there is none. k7q_03_rep2 and K7Q03r2 are the same id."""
    match = SAMPLE_IN_TEXT.search(str(text or ""))
    if not match:
        return None
    code, run, replicate = match.groups()
    return sample_id(f"{code.upper()}-{int(run):02d}", int(replicate))


def default_condition(parameters: dict[str, Any]) -> str:
    """A row's condition when none is given: its numbers as set, joined by /: 300/40/40."""
    numbers = [value for value in parameters.values() if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return "/".join(f"{value:g}" for value in numbers)


@dataclass
class Run:
    number: int
    protocol: str
    parameters: dict[str, Any] = field(default_factory=dict)
    condition: str = ""
    note: str = ""
    state: str = "planned"
    run_folder: str = ""             # the run's folder name once it ran

    def run_id(self, code: str) -> str:
        return f"{code}-{self.number:02d}"


@dataclass
class Experiment:
    code: str
    name: str = ""
    runs: list[Run] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # -- the table ----------------------------------------------------------------------------
    def add(self, protocol: str, parameters: dict[str, Any] | None = None, *, condition: str = "",
            note: str = "") -> Run:
        number = max((run.number for run in self.runs), default=0) + 1
        run = Run(number, protocol, dict(parameters or {}), condition or default_condition(parameters or {}), note)
        self.runs.append(run)
        return run

    def run(self, run_id: str) -> Run:
        for run in self.runs:
            if run.run_id(self.code) == run_id:
                return run
        raise LookupError(f"no run {run_id} in experiment {self.code}")

    def next_planned(self) -> Run | None:
        return next((run for run in self.runs if run.state == "planned"), None)

    def mark(self, run_id: str, state: str, run_folder: str = "") -> Run:
        if state not in STATES:
            raise ValueError(f"a run is one of {', '.join(STATES)}")
        run = self.run(run_id)
        run.state = state
        if run_folder:
            run.run_folder = run_folder
        return run

    # -- what a run is given --------------------------------------------------------------------
    def document(self, run: Run, protocols: Protocols) -> dict[str, Any]:
        """The run's protocol with its parameter values and its run block: named by its id, in its
        condition, marked with its id and the experiment's name."""
        from admet.workflows.json_protocol import parameter_values

        document = deepcopy(protocols(run.protocol))
        document["parameter_values"] = {**document.get("parameter_values", {}), **run.parameters}
        parameter_values(document)                       # undeclared names and wrong kinds are refused here
        run_id = run.run_id(self.code)
        block = {"name": run_id, "id": run_id}
        if run.condition:
            block["condition"] = run.condition[:80]
        if self.name:
            block["experiment"] = self.name[:80]
        document["run"] = block
        return document

    # -- JSON -------------------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {"experiment": self.name, "code": self.code, "created_at": self.created_at,
                "runs": [{"id": run.run_id(self.code), "protocol": run.protocol, "parameters": run.parameters,
                          "condition": run.condition, "note": run.note, "state": run.state,
                          "run_folder": run.run_folder} for run in self.runs]}

    @classmethod
    def from_json(cls, document: dict[str, Any], protocols: Protocols, *, taken: set[str] | None = None) -> Experiment:
        """An experiment from its document: {"experiment": name, "protocol": default protocol,
        "runs": [{"protocol"?, "parameters"?, "condition"?, "note"?}, ...]}. Every run is checked
        against its protocol before any is taken, and an error names the run it is in."""
        if not isinstance(document, dict) or not isinstance(document.get("runs"), list):
            raise ValueError("an experiment is an object with a list of runs")
        code = str(document.get("code") or "") or new_code(taken)
        if not re.fullmatch(_CODE, code):
            raise ValueError(f"an experiment's code is {CODE_LENGTH} of {ALPHABET}")
        experiment = cls(code, str(document.get("experiment") or ""))
        if document.get("created_at"):
            experiment.created_at = str(document["created_at"])
        default_protocol = document.get("protocol")
        for index, entry in enumerate(document["runs"], 1):
            where = f"run {index}"
            if not isinstance(entry, dict):
                raise ValueError(f"{where}: each run is an object")
            protocol = entry.get("protocol") or default_protocol
            if not protocol:
                raise ValueError(f"{where}: no protocol, for the run or the experiment")
            parameters = entry.get("parameters") or {}
            if not isinstance(parameters, dict):
                raise ValueError(f"{where}: parameters is an object of names and values")
            run = experiment.add(str(protocol), parameters, condition=str(entry.get("condition") or ""),
                                 note=str(entry.get("note") or ""))
            if entry.get("id"):
                number = re.fullmatch(rf"{code}-(\d{{2,3}})", str(entry["id"]))
                if not number:
                    raise ValueError(f"{where}: id {entry['id']} is not one of experiment {code}")
                run.number = int(number.group(1))
            state = str(entry.get("state") or "planned")
            if state not in STATES:
                raise ValueError(f"{where}: state is one of {', '.join(STATES)}")
            run.state, run.run_folder = state, str(entry.get("run_folder") or "")
            try:
                experiment.document(run, protocols)
            except (KeyError, LookupError) as exc:
                raise ValueError(f"{where}: no protocol {protocol}") from exc
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from exc
        numbers = [run.number for run in experiment.runs]
        if len(set(numbers)) != len(numbers):
            raise ValueError("two runs share an id")
        return experiment


class ExperimentStore:
    """The experiments of one data folder: experiments/<code>.json, each written whole."""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / EXPERIMENTS_DIR

    def codes(self) -> set[str]:
        return {path.stem for path in self.root.glob("*.json")} if self.root.is_dir() else set()

    def save(self, experiment: Experiment) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{experiment.code}.json"
        write_atomic(path, json.dumps(experiment.to_json(), indent=2, allow_nan=False) + "\n")
        return path

    def load(self, code: str, protocols: Protocols) -> Experiment:
        if not re.fullmatch(_CODE, str(code)):
            raise ValueError(f"no experiment {code}")
        return Experiment.from_json(json.loads((self.root / f"{code}.json").read_text(encoding="utf-8")), protocols)

    def list(self) -> list[dict[str, Any]]:
        """Every experiment, newest first: its code, name, and how many of its runs are done."""
        entries = []
        for path in self.root.glob("*.json") if self.root.is_dir() else ():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            runs = document.get("runs") or []
            entries.append({"code": path.stem, "name": document.get("experiment", ""),
                            "created_at": document.get("created_at", ""), "runs": len(runs),
                            "done": sum(1 for run in runs if run.get("state") == "done")})
        return sorted(entries, key=lambda entry: entry["created_at"], reverse=True)
