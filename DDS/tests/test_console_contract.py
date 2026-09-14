"""控制台契约测试：不依赖 Fast DDS 原生环境，用桩引擎跑完整编排链路。

覆盖：
* `console_runner.execute_spec()` 的产物布局与字段；
* 每个条件跑满 `case_repeats`；
* 环境未就绪时 `status="not_tested"`、指标全 `null`、样本文件仍存在；
* `console_runner.main()` 的 stdout 协议与退出码。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import console_runner  # noqa: E402
from fastdds_bench.results import sample_document  # noqa: E402
from fastdds_bench.util import atomic_write_json, read_json  # noqa: E402

UNIFIED_FIELDS = (
    "run_id",
    "middleware_id",
    "middleware_version",
    "scenario_name",
    "scenario_title",
    "repeat",
    "status",
    "payload_size_bytes",
    "publish_rate_hz",
    "publisher_count",
    "subscriber_count",
    "messages_sent",
    "messages_received",
    "unique_deliveries",
    "expected_deliveries",
    "achieved_publish_rate_hz",
    "latency_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "latency_std_ms",
    "throughput_mbps",
    "jitter_ms",
    "packet_loss",
    "final_packet_loss",
    "duplicate_count",
    "out_of_order_count",
    "corrupted_count",
    "startup_time_ms",
    "discovery_time_ms",
    "recovery_time_ms",
    "cpu_percent",
    "memory_mb",
    "latency_sample_count",
    "configuration",
    "statistics",
    "link_metrics",
    "environment",
    "limitations",
)


def load_adapter():
    spec = importlib.util.spec_from_file_location(
        "dds_adapter_for_test", PROJECT_ROOT / "adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubBridge:
    """桩引擎：产出结构合法的单轮结果，不接触任何原生库。"""

    environment = {"vendor": "eProsima Fast DDS", "version": "3.6.2", "os": "stub"}
    probe = {"ok": True, "error": None}
    warnings: list[str] = []

    def __init__(self) -> None:
        self.measured = 0

    def measure(self, case_index, repeat_index, ordinal, run_dir, run_id, suite_id):
        self.measured += 1
        latencies = [0.71, 0.62, 0.49]
        atomic_write_json(
            Path(run_dir) / "subscriber-0.receive.bin", {"records": len(latencies)}
        )
        return {
            "effective_config": {
                "condition_id": "S01_1KiB_100",
                "payload_size_bytes": 1024,
                "publish_rate_hz": 100,
                "publisher_count": 1,
                "subscriber_count": 1,
                "transport_mode": "udp",
                "qos_profile": "reliable",
                "network_profile": "normal",
                "repeats": 1,
                "payload_checksum": 1,
            },
            "status": "completed",
            "run_id": run_id,
            "suite_id": suite_id,
            "condition_id": "S01_1KiB_100",
            "scenario_id": "S01",
            "scenario_name": "point_to_point_latency",
            "scenario_title": "点对点低延迟 · 1 KiB @ 100 Hz",
            "repeat_index": repeat_index,
            "module_version": "3.6.2",
            "adapter_version": "test",
            "measurement_engine": "stub",
            "sent_success_count": 100,
            "received_before_count": 100,
            "valid_unique_delivery_count": 100,
            "expected_delivery_count": 100,
            "send_window_seconds": 1.0,
            "latency_ms": 0.6,
            "latency_p95_ms": 0.7,
            "latency_p99_ms": 0.71,
            "latency_std_ms": 0.05,
            "jitter_ms": 0.09,
            "throughput_mbps": 0.82,
            "cpu_percent": 12.0,
            "memory_mb": 30.0,
            "packet_loss": 0.0,
            "final_packet_loss": 0.0,
            "startup_time_ms": 120.0,
            "discovery_time_ms": 40.0,
            "recovery_time_ms": 0.0,
            "latency_sample_count": len(latencies),
            "jitter_sample_count": 2,
            "per_link": [{"publisher_id": 0, "subscriber_id": 0}],
            "errors": [],
            "warnings": [],
            "fault_events": [],
            "restart_count": 0,
            "test_start_time": "2026-09-14T00:00:00.000Z",
            "test_end_time": "2026-09-14T00:00:01.000Z",
            "artifacts": {},
            "timing_details": {},
            "resource_details": {},
        }

    def write_samples(self, case_index, run_dir, run_id):
        atomic_write_json(
            Path(run_dir) / "subscriber-0.result.json",
            sample_document([0.71, 0.62, 0.49], run_id=run_id, condition_id="stub"),
        )
        return "subscriber-0.result.json"


class _StubRun:
    engine = _StubBridge()


class ConsoleContractTests(unittest.TestCase):
    def _spec(self, job: Path, repeats: int, index: int = 0) -> dict:
        adapter = load_adapter()
        cases = adapter.build_cases(index, {"repeats": repeats}, False)
        output = job / "output"
        output.mkdir(parents=True, exist_ok=True)
        return {
            "middleware": "dds",
            "job_id": job.name,
            "config": adapter.base_config(),
            "cases": cases,
            "plan": [
                {
                    "scenario_id": cases[0]["scenario_id"],
                    "scenario_name": cases[0]["scenario_id"],
                    "planned_repeats": repeats,
                }
            ],
            "output": str(output),
            "logs": str(output / "logs"),
        }

    def test_not_tested_path_keeps_null_metrics_and_writes_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            spec = self._spec(job, repeats=1)
            original = console_runner._build_bridge
            console_runner._build_bridge = lambda _spec, _log: (None, "桩：环境未就绪")
            try:
                document = console_runner.execute_spec(spec)
            finally:
                console_runner._build_bridge = original
            output = Path(spec["output"])
            self.assertEqual(document["status"], "completed")
            self.assertEqual(document["planned_runs"], 1)
            self.assertEqual(len(document["runs"]), 1)
            run = document["runs"][0]
            self.assertEqual(run["status"], "not_tested")
            for field in UNIFIED_FIELDS:
                self.assertIn(field, run, f"缺少统一字段 {field}")
            for field in (
                "latency_ms",
                "throughput_mbps",
                "cpu_percent",
                "memory_mb",
                "packet_loss",
                "final_packet_loss",
                "startup_time_ms",
                "discovery_time_ms",
            ):
                self.assertIsNone(run[field], f"{field} 必须保持 null")
            runs_dir = output / "runs"
            self.assertEqual(len(list(runs_dir.glob("*.json"))), 1)
            samples = output / run["artifacts_directory"] / "subscriber-0.result.json"
            self.assertTrue(samples.is_file())
            self.assertEqual(
                json.loads(samples.read_text(encoding="utf-8"))["latencies_ms"], []
            )
            self.assertTrue((output / "logs" / "suite.log").is_file())

    def test_every_condition_runs_its_full_repeat_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            spec = self._spec(job, repeats=3)
            original = console_runner._build_bridge
            console_runner._build_bridge = lambda _spec, _log: (_StubRun.engine, None)
            try:
                document = console_runner.execute_spec(spec)
            finally:
                console_runner._build_bridge = original
            self.assertEqual(document["planned_runs"], 3)
            self.assertEqual(len(document["runs"]), 3)
            self.assertTrue(all(run["status"] == "completed" for run in document["runs"]))
            output = Path(spec["output"])
            self.assertEqual(len(list((output / "runs").glob("*.json"))), 3)
            self.assertEqual(len(list((output / "artifacts").iterdir())), 3)
            for run in document["runs"]:
                samples = output / run["artifacts_directory"] / "subscriber-0.result.json"
                self.assertEqual(
                    json.loads(samples.read_text(encoding="utf-8"))["latencies_ms"],
                    [0.71, 0.62, 0.49],
                )
                self.assertAlmostEqual(run["achieved_publish_rate_hz"], 100.0)
            self.assertEqual(len(document["scenario_summaries"]), 1)
            self.assertEqual(document["scenario_summaries"][0]["repeat_count"], 3)

    def test_main_prints_protocol_lines_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            spec = self._spec(job, repeats=1)
            spec_path = job / "spec.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            original = console_runner._build_bridge
            console_runner._build_bridge = lambda _spec, _log: (None, "桩：环境未就绪")
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    code = console_runner.main([str(spec_path)])
            finally:
                console_runner._build_bridge = original
            self.assertEqual(code, 0)
            text = buffer.getvalue()
            self.assertIn("START ", text)
            self.assertIn("END ", text)
            self.assertIn("RESULT ", text)
            self.assertIn("status=completed", text)

    def test_capability_not_tested_condition_is_reported_without_measuring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            adapter = load_adapter()
            cases = [
                case
                for case in adapter.build_cases(24, {}, True)
                if case["capability"] != "supported"
            ]
            self.assertTrue(cases, "S09 应该包含 not_tested 条件")
            output = job / "output"
            output.mkdir(parents=True, exist_ok=True)
            spec = {
                "middleware": "dds",
                "job_id": "job",
                "config": adapter.base_config(),
                "cases": cases,
                "plan": [],
                "output": str(output),
                "logs": str(output / "logs"),
            }
            stub = _StubBridge()
            original = console_runner._build_bridge
            console_runner._build_bridge = lambda _spec, _log: (stub, None)
            try:
                document = console_runner.execute_spec(spec)
            finally:
                console_runner._build_bridge = original
            self.assertEqual(stub.measured, 0, "标注 not_tested 的条件不得进入引擎")
            self.assertTrue(all(run["status"] == "not_tested" for run in document["runs"]))
            self.assertTrue(all(run["errors"] for run in document["runs"]))


if __name__ == "__main__":
    unittest.main()
