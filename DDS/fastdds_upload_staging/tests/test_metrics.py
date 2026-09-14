from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from fastdds_bench.metrics import analyze_run, latency_statistics, linear_percentile
from fastdds_bench.rawio import (
    ALL_VALID_FLAGS,
    ReceiveRecord,
    ReceiveRecordWriter,
    SendRecord,
    SendRecordWriter,
)


class MetricDefinitionTests(unittest.TestCase):
    def test_linear_percentile_and_population_std(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(linear_percentile(values, 0.95) or 0, 3.85)
        self.assertAlmostEqual(linear_percentile(values, 0.99) or 0, 3.97)
        stats = latency_statistics({(0, 0): list(enumerate(values))})
        self.assertAlmostEqual(float(stats["latency_ms"]), 2.5)
        self.assertAlmostEqual(float(stats["latency_std_ms"]), math.sqrt(1.25))
        self.assertAlmostEqual(float(stats["jitter_ms"]), 1.0)

    def test_empty_latency_is_null(self) -> None:
        stats = latency_statistics({})
        for field in (
            "latency_ms",
            "latency_p95_ms",
            "latency_p99_ms",
            "latency_std_ms",
            "jitter_ms",
        ):
            self.assertIsNone(stats[field])

    def test_recovery_is_excluded_from_latency_and_throughput(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            send_path = tmp_path / "publisher_0.send.bin"
            send_writer = SendRecordWriter(send_path)
            for sequence, timestamp in enumerate(
                (1_000_000_000, 2_000_000_000, 3_000_000_000)
            ):
                send_writer.append(SendRecord(0, sequence, timestamp, 10, 1234))
            self.assertEqual(send_writer.close(), 3)

            receive_path = tmp_path / "subscriber_0.receive.bin"
            receive_writer = ReceiveRecordWriter(receive_path)
            for sequence, sent, received in (
                (0, 1_000_000_000, 1_100_000_000),
                (1, 2_000_000_000, 2_200_000_000),
                (2, 3_000_000_000, 3_500_000_000),
            ):
                receive_writer.append(
                    ReceiveRecord(
                        0,
                        sequence,
                        sent,
                        received,
                        10,
                        1234,
                        10,
                        1234,
                        ALL_VALID_FLAGS,
                    )
                )
            self.assertEqual(receive_writer.close(), 3)

            condition = {
                "payload_size_bytes": 10,
                "payload_checksum": 1234,
                "publisher_count": 1,
                "subscriber_count": 1,
                "network": {"packet_loss_percent": 0},
            }
            metrics = analyze_run(
                condition,
                tmp_path,
                [{"raw_file": send_path.name, "send_failure_count": 0}],
                [
                    {
                        "endpoint_id": 0,
                        "raw_file": receive_path.name,
                        "snapshot_record_count": 2,
                    }
                ],
                {
                    "send_complete_ns": 3_000_000_000,
                    "resource_window_ns": 1_000_000_000,
                    "startup_time_ms": 4.0,
                    "discovery_time_ms": 3.0,
                },
                {
                    "cpu_time_seconds": 0.5,
                    "peak_rss_bytes": 100_000_000,
                    "first_rss_bytes": 80_000_000,
                    "last_rss_bytes": 90_000_000,
                },
            )
            self.assertEqual(metrics["sent_success_count"], 3)
            self.assertEqual(metrics["expected_delivery_count"], 3)
            self.assertEqual(metrics["received_before_count"], 2)
            self.assertEqual(metrics["valid_unique_delivery_count"], 3)
            self.assertAlmostEqual(metrics["packet_loss"], 1 / 3)
            self.assertEqual(metrics["final_packet_loss"], 0)
            self.assertEqual(metrics["recovered_count"], 1)
            self.assertAlmostEqual(metrics["recovery_time_ms"], 500.0)
            self.assertEqual(metrics["latency_sample_count"], 2)
            self.assertAlmostEqual(metrics["latency_ms"], 150.0)
            self.assertAlmostEqual(metrics["jitter_ms"], 100.0)
            self.assertAlmostEqual(metrics["latency_drift_ms"], 100.0)
            self.assertAlmostEqual(metrics["throughput_mbps"], 160 / 1.2 / 1_000_000)
            self.assertAlmostEqual(
                metrics["offered_throughput_mbps"], 240 / 2 / 1_000_000
            )
            self.assertAlmostEqual(metrics["cpu_percent"], 50.0)
            self.assertAlmostEqual(metrics["memory_mb"], 100.0)
            self.assertAlmostEqual(metrics["memory_growth_mb"], 10.0)


if __name__ == "__main__":
    unittest.main()
