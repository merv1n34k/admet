"""Protocol definitions and run artifacts within an ADMET project."""

import json
import math
import os
from pathlib import Path
import re
import tempfile

from admet.workflows.json_protocol import load, normalize


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ProtocolStore:
    def __init__(self, project):
        self.project = project
        self.root = project.path

    def _path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
            raise ValueError("invalid protocol name")
        path = self.root / "protocols" / f"{name}.json"
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("protocol path leaves the project")
        return path

    def save(self, document, *, replace=False):
        document = normalize(document)
        path = self._path(document["name"])
        if path.exists() and not replace:
            raise FileExistsError(f"{document['name']} already exists; use replace=true or a new name")
        write_json(path, document)
        self.project.upsert_file_path(path, role="protocol", media_type="application/json")
        self.project.save()
        return {"path": str(path), "protocol": document}

    def read(self, name):
        path = self._path(name)
        return {"path": str(path), "protocol": load(path)}

    def list(self):
        result = []
        for path in sorted((self.root / "protocols").glob("*.json")):
            try:
                document = load(self._path(path.stem))
                result.append({"name": document["name"], "path": str(path),
                               "steps": len(document["steps"])})
            except (OSError, ValueError) as exc:
                result.append({"name": path.stem, "path": str(path), "error": str(exc)})
        return {"protocols": result}

    def plan(self, plan):
        if plan.get("run_id"):
            write_json(self.root / "records" / "protocols" / plan["run_id"] / "plan.json", plan)

    def begin(self, plan):
        directory = self.root / "records" / "protocols" / plan.get("run_id", plan["plan_id"])
        write_json(directory / "plan.json", plan)
        definition = plan["normalized_settings"].get("protocol")
        if definition is not None:
            write_json(directory / "protocol.json", definition)
            fields = definition.get("measurements", {})
            if fields:
                write_json(directory / "measurements.json", {
                    "run_id": plan.get("run_id", plan["plan_id"]), "revision": 0,
                    "fields": fields, "values": {key: None for key in fields},
                })
        path = directory / "summary.json"
        write_json(path, {"plan_id": plan["plan_id"], "run_id": plan.get("run_id"), "state": "executing"})
        self.project.upsert_file_path(path, role="protocol_run", media_type="application/json")
        self.project.save()
        return directory

    def measurements(self, run_id, changes=None):
        if not isinstance(run_id, str) or not re.fullmatch(r"(?:run|plan)_[a-f0-9]{32}", run_id):
            raise ValueError("invalid run ID")
        path = self.root / "records" / "protocols" / run_id / "measurements.json"
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("measurement path leaves project")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if changes is not None:
            if not isinstance(changes, dict) or set(changes) - set(payload["fields"]):
                raise ValueError("undeclared measurement")
            for value in changes.values():
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                    raise ValueError("measurements must be finite numbers or null")
            payload["values"].update(changes)
            payload["revision"] += 1
            write_json(path, payload)
        return payload

    def measurement_runs(self):
        result = []
        for path in (self.root / "records" / "protocols").glob("*/measurements.json"):
            plan = json.loads((path.parent / "plan.json").read_text(encoding="utf-8"))
            result.append({"run_id": path.parent.name,
                           "name": plan["normalized_settings"]["protocol"]["name"],
                           "at": plan.get("executed_at", "")})
        return sorted(result, key=lambda entry: entry["at"], reverse=True)

    def finish(self, plan, directory, artifacts):
        self.plan(plan)
        write_json(directory / "summary.json", {**plan, "artifacts": artifacts})
