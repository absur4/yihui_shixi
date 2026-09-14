from __future__ import annotations

import importlib.util
import unittest

from fastdds_bench.results import GROUP_FIELDS, summarize_runs


def _condition() -> dict[str, object]:
    return {
        "condition_id": "EXAMPLE",
        "scenario_id": "S01",
        "scenario_name": "point_to_point",
        "payload_size_bytes": 1024,
        "message_count": 10,
        "publish_rate_hz": 100.0,
        "publisher_count": 1,
        "subscriber_count": 1,
        "transport_mode": "UDPv4",
        "qos_profile": "reliable",
        "network_profile": "normal",
    }


class SchemaAndSummaryTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("jsonschema") and importlib.util.find_spec("psutil"),
        "optional runtime dependencies are not installed",
    )
    def test_null_run_and_suite_validate_against_schema(self) -> None:
        from fastdds_bench.controller import _null_run
        from fastdds_bench.schema_validation import validate_result

        config = {"suite": {"module_version": "3.6.2"}}
        run = _null_run(
            config,
            _condition(),
            "run-1",
            1,
            "2026-09-13T00:00:00.000Z",
            "2026-09-13T00:00:01.000Z",
            "synthetic failure",
        )
        suite = {
            "schema_version": "1.0",
            "suite_id": "suite-1",
            "metrics_definition_version": "1.0",
            "module_name": "fastdds",
            "module_version": "3.6.2",
            "adapter_version": "1.0.0",
            "suite_start_time": "2026-09-13T00:00:00.000Z",
            "suite_end_time": "2026-09-13T00:00:01.000Z",
            "environment": {},
            "runs": [run],
            "summary": summarize_runs([run]),
        }
        validate_result(suite)
        suite["runs"][0]["latency_ms"] = float("nan")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            validate_result(suite)

    def test_summary_never_mixes_required_group_fields(self) -> None:
        first = {field: "x" for field in GROUP_FIELDS}
        first.update(
            {
                "condition_id": "a",
                "status": "passed",
                "latency_ms": 1.0,
                "payload_size_bytes": 1024,
                "publish_rate_hz": 100.0,
            }
        )
        second = dict(first, condition_id="b", payload_size_bytes=2048)
        summary = summarize_runs([first, second])
        self.assertEqual(len(summary), 2)


if __name__ == "__main__":
    unittest.main()
