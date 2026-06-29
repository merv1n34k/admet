import unittest

from admet.core.engine import ActionSpec, EngineContext, EngineResult, validate_action_settings
from admet.core.schema import Param, ParamKind, ParamSchema, ResultRecord, ResultSet, SummaryStat
from admet.engines.analyze import BatchItem, BatchRunner


class FakeAnalysisEngine:
    id = "fake"
    name = "Fake Analysis"
    settings = ParamSchema(
        (
            Param("sample_id", "Sample ID", ParamKind.TEXT, default="sample"),
            Param("value", "Value", ParamKind.INTEGER, default=1, minimum=0),
            Param("fail", "Fail", ParamKind.BOOLEAN, default=False),
        )
    )
    actions = (
        ActionSpec(
            "analyze",
            "Analyze",
            "analysis",
            params=("sample_id", "value", "fail"),
        ),
    )

    def __init__(self):
        self.calls = []

    def run_action(
        self,
        action: str,
        settings: dict,
        context: EngineContext | None = None,
    ) -> EngineResult:
        normalized = validate_action_settings(self.settings, self.actions, action, settings)
        self.calls.append((action, normalized, context.metadata if context else {}))
        if normalized["fail"]:
            raise RuntimeError("sample failed")
        sample_id = normalized["sample_id"]
        return EngineResult(
            result_set=ResultSet(
                records=(
                    ResultRecord(
                        sample_id=sample_id,
                        engine=self.id,
                        values={"value": normalized["value"]},
                    ),
                ),
                stats=(SummaryStat("value", normalized["value"]),),
                metadata={"sample_id": sample_id},
            ),
            artifacts={"artifact": f"{sample_id}.txt"},
        )


class BatchRunnerTests(unittest.TestCase):
    def test_batch_runner_drives_engine_contract_and_aggregates_records(self):
        engine = FakeAnalysisEngine()
        events = []
        runner = BatchRunner(
            engine,
            [
                BatchItem({"sample_id": "a", "value": 2}),
                BatchItem({"sample_id": "b", "value": 3}, params={"dilution": 10}),
            ],
            context=EngineContext(metadata={"batch": "demo"}),
            item_started=lambda item_id: events.append(("started", item_id)),
            item_finished=lambda item_id, _result, outcome: events.append(
                ("finished", item_id, outcome["status"])
            ),
        )

        result = runner.run()

        self.assertEqual([call[0] for call in engine.calls], ["analyze", "analyze"])
        self.assertEqual([record.sample_id for record in result.result_set.records], ["a", "b"])
        self.assertEqual(
            [record.values["batch_item_id"] for record in result.result_set.records],
            ["a", "b"],
        )
        stats = {stat.name: stat.value for stat in result.result_set.stats}
        self.assertEqual(stats["batch_total"], 2)
        self.assertEqual(stats["batch_finished"], 2)
        self.assertEqual(stats["batch_failed"], 0)
        self.assertEqual(result.artifacts["items"]["a"], {"artifact": "a.txt"})
        self.assertEqual(result.result_set.metadata["items"][1]["params"], {"dilution": 10})
        self.assertEqual(
            events,
            [
                ("started", "a"),
                ("finished", "a", "finished"),
                ("started", "b"),
                ("finished", "b", "finished"),
            ],
        )

    def test_batch_runner_keeps_failed_items_as_logical_rows(self):
        engine = FakeAnalysisEngine()
        failures = []
        runner = BatchRunner(
            engine,
            [
                BatchItem({"sample_id": "ok", "value": 1}),
                BatchItem({"sample_id": "bad", "fail": True}),
            ],
            item_failed=lambda item_id, message: failures.append((item_id, message)),
        )

        result = runner.run()

        stats = {stat.name: stat.value for stat in result.result_set.stats}
        self.assertEqual(stats["batch_finished"], 1)
        self.assertEqual(stats["batch_failed"], 1)
        failed = result.result_set.records[1]
        self.assertEqual(failed.sample_id, "bad")
        self.assertEqual(failed.values["status"], "failed")
        self.assertEqual(failed.values["error"], "sample failed")
        self.assertEqual(failures, [("bad", "sample failed")])


if __name__ == "__main__":
    unittest.main()
