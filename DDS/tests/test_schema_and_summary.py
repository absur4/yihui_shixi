from __future__ import annotations

import importlib.util
import unittest

from fastdds_bench.results import GROUP_FIELDS, suite_document, summarize_runs


def _condition() -> dict[str, object]:
    return {
        "condition_id": "S01_1KiB_100",
        "scenario_id": "S01",
        "scenario_name": "point_to_point_latency",
        "scenario_title": "点对点低延迟 · 1 KiB @ 100 Hz",
        "capability": "supported",
        "payload_size_bytes": 1024,
        "message_count": 10,
        "publish_rate_hz": 100.0,
        "publisher_count": 1,
        "subscriber_count": 1,
        "transport_mode": "udp",
        "qos_profile": "reliable",
        "network_profile": "normal",
    }


class SchemaAndSummaryTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("jsonschema") and importlib.util.find_spec("psutil"),
        "optional runtime dependencies are not installed",
    )
    def test_error_run_and_suite_validate_against_schema(self) -> None:
        from fastdds_bench.controller import _null_run
        from fastdds_bench.schema_validation import validate_result

        config = {
            "suite": {"module_version": "3.6.2", "vendor": "eProsima Fast DDS"},
        }
        run = _null_run(
            config,
            _condition(),
            "run-1",
            1,
            "2026-09-13T00:00:00.000Z",
            "2026-09-13T00:00:01.000Z",
            "synthetic failure",
        )
        self.assertEqual(run["status"], "error")
        self.assertIsNone(run["latency_ms"])
        result = suite_document(
            run_id="suite-1",
            status="error",
            planned_runs=1,
            test_start_time="2026-09-13T00:00:00.000Z",
            test_end_time="2026-09-13T00:00:01.000Z",
            environment={},
            configuration={"source": "unit-test"},
            runs=[run],
            limitations=["unit test"],
            middleware_version="3.6.2",
        )
        validate_result(result)
        result["runs"][0]["latency_ms"] = float("nan")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            validate_result(result)

    def test_not_tested_run_keeps_null_metrics(self) -> None:
        from fastdds_bench.results import not_tested_run

        run = not_tested_run(
            _condition(),
            repeat=1,
            run_id="run-2",
            suite_id="suite-2",
            status="not_tested",
            reason="Fast DDS 环境未就绪",
        )
        for field in (
            "latency_ms",
            "latency_p95_ms",
            "throughput_mbps",
            "cpu_percent",
            "memory_mb",
            "packet_loss",
            "final_packet_loss",
            "startup_time_ms",
        ):
            self.assertIsNone(run[field], f"{field} must stay null when not measured")
        self.assertEqual(run["status"], "not_tested")
        self.assertIn("环境未就绪", run["errors"][0])

    def test_summary_never_mixes_required_group_fields(self) -> None:
        first = {field: "x" for field in GROUP_FIELDS}
        first.update(
            {
                "middleware_id": "dds",
                "condition_id": "a",
                "status": "completed",
                "latency_ms": 1.0,
                "payload_size_bytes": 1024,
                "publish_rate_hz": 100.0,
            }
        )
        second = dict(first, condition_id="b", payload_size_bytes=2048)
        summary = summarize_runs([first, second])
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["repeat_count"], 1)
        self.assertFalse(summary[0]["formal_minimum_met"])

    def test_only_completed_runs_count_as_valid_repeats(self) -> None:
        rows = []
        for repeat in range(1, 6):
            row = {field: "y" for field in GROUP_FIELDS}
            row.update(
                {
                    "middleware_id": "dds",
                    "condition_id": "S12_integrity",
                    "status": "completed" if repeat > 1 else "not_tested",
                    "latency_ms": float(repeat),
                    "payload_size_bytes": 16384,
                    "publish_rate_hz": 1000.0,
                }
            )
            rows.append(row)
        summary = summarize_runs(rows)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["attempted_repeats"], 5)
        self.assertEqual(summary[0]["valid_repeats"], 4)
        self.assertFalse(summary[0]["formal_minimum_met"])


if __name__ == "__main__":
    unittest.main()
