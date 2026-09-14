from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastdds_bench.config import (
    ConfigError,
    load_config,
    normalize_config,
    select_conditions,
)
from fastdds_bench.payload import crc32_text, make_payload_text
from fastdds_bench.scheduler import aggregate_slot, aggregate_target_ns
from fastdds_bench.transport import render_transport_xml
from fastdds_bench.util import PROJECT_ROOT


class ConfigAndTransportTests(unittest.TestCase):
    def test_example_and_formal_config_are_valid(self) -> None:
        example, warnings = load_config(PROJECT_ROOT / "config.example.yaml")
        self.assertEqual(warnings, [])
        self.assertEqual(len(select_conditions(example)), 1)
        self.assertEqual(sum(c["repeats"] for c in select_conditions(example)), 1)

        formal, warnings = load_config(PROJECT_ROOT / "config.yaml")
        self.assertEqual(warnings, [])
        self.assertEqual(len(formal["conditions"]), 30)
        self.assertEqual(len(select_conditions(formal)), 26)
        self.assertEqual(sum(c["repeats"] for c in select_conditions(formal)), 130)

    def test_missing_stop_condition_is_rejected(self) -> None:
        source = {
            "schema_version": "1.0",
            "metrics_definition_version": "1.0",
            "qos_profiles": {"reliable": {}},
            "conditions": [
                {
                    "condition_id": "bad",
                    "scenario_id": "S00",
                    "scenario_name": "bad",
                    "payload_size_bytes": 1,
                }
            ],
        }
        with self.assertRaisesRegex(ConfigError, "needs message_count"):
            normalize_config(source)

    def test_payload_size_and_checksum_are_deterministic(self) -> None:
        payload = make_payload_text(1_048_576, 7401, "S02_1m")
        self.assertEqual(len(payload.encode("ascii")), 1_048_576)
        self.assertEqual(payload, make_payload_text(1_048_576, 7401, "S02_1m"))
        self.assertEqual(crc32_text(payload), crc32_text(payload))

    def test_udpv4_transport_cannot_fall_back_to_shm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            udp_path = render_transport_xml(tmp_path / "udp.xml", "UDPv4", "test")
            udp = udp_path.read_text(encoding="utf-8")
            self.assertIn("<type>UDPv4</type>", udp)
            self.assertIn("<useBuiltinTransports>false</useBuiltinTransports>", udp)
            self.assertIn("<transport_id>benchmark_udpv4</transport_id>", udp)

            shm_path = render_transport_xml(tmp_path / "shm.xml", "SHM", "test")
            shm = shm_path.read_text(encoding="utf-8")
            self.assertIn("<type>SHM</type>", shm)
            self.assertIn("<useBuiltinTransports>false</useBuiltinTransports>", shm)

    def test_multiple_publishers_share_aggregate_slots(self) -> None:
        slots = [
            aggregate_slot(sequence, publisher_id, 4)
            for publisher_id in range(4)
            for sequence in range(3)
            if aggregate_slot(sequence, publisher_id, 4) < 10
        ]
        self.assertEqual(sorted(slots), list(range(10)))
        self.assertEqual(aggregate_target_ns(1_000, 9, 1000), 9_001_000)


if __name__ == "__main__":
    unittest.main()
