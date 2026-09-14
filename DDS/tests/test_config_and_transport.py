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
from fastdds_bench.scheduler import paced_target_ns
from fastdds_bench.transport import render_transport_xml, transport_detail
from fastdds_bench.util import PROJECT_ROOT


class ConfigAndTransportTests(unittest.TestCase):
    def test_example_and_formal_config_are_valid(self) -> None:
        example, warnings = load_config(PROJECT_ROOT / "config.example.yaml")
        self.assertEqual(warnings, [])
        self.assertEqual(len(select_conditions(example)), 1)
        self.assertEqual(sum(c["repeats"] for c in select_conditions(example)), 1)

        formal, warnings = load_config(PROJECT_ROOT / "config.yaml")
        self.assertEqual(warnings, [])
        # S01–S12 全部条件都在目录里；其中只有 supported 且 enabled 的才进入正式矩阵。
        self.assertEqual(len(formal["conditions"]), 36)
        self.assertEqual(len(select_conditions(formal)), 30)
        self.assertEqual(sum(c["repeats"] for c in select_conditions(formal)), 150)

    def test_naming_and_scope_are_unified(self) -> None:
        config, _warnings = load_config(PROJECT_ROOT / "config.yaml")
        self.assertEqual(config["suite"]["middleware_id"], "dds")
        self.assertEqual(config["suite"]["module_name"], "dds")
        self.assertEqual(config["suite"]["rate_scope"], "per_publisher")
        self.assertEqual(config["suite"]["message_count_scope"], "per_publisher")
        for condition in config["conditions"]:
            self.assertEqual(condition["transport_mode"], condition["transport_mode"].lower())
            self.assertIn(condition["transport_mode"], {"udp", "shm", "default"})
            self.assertRegex(condition["scenario_id"], r"^S\d{2}$")

    def test_scenario_names_come_from_the_shared_catalogue(self) -> None:
        import sys

        sys.path.insert(0, str(PROJECT_ROOT.parent))
        from interfaces.scenarios import SCENARIO_BY_ID  # noqa: E402

        config, _warnings = load_config(PROJECT_ROOT / "config.yaml")
        for condition in config["conditions"]:
            scenario = SCENARIO_BY_ID[condition["scenario_id"]]
            self.assertEqual(condition["scenario_name"], scenario.name)

    def test_not_tested_conditions_are_labelled_and_disabled(self) -> None:
        config, _warnings = load_config(PROJECT_ROOT / "config.yaml")
        for condition in config["conditions"]:
            if condition["capability"] != "supported":
                self.assertFalse(condition["enabled"])
                self.assertTrue(condition["capability_note"])

    def test_missing_stop_condition_is_rejected(self) -> None:
        source = {
            "schema_version": "1.0",
            "metrics_definition_version": "1.0",
            "qos_profiles": {"reliable": {}},
            "conditions": [
                {
                    "condition_id": "bad",
                    "scenario_id": "S01",
                    "scenario_name": "point_to_point_latency",
                    "payload_size_bytes": 1,
                }
            ],
        }
        with self.assertRaisesRegex(ConfigError, "needs message_count"):
            normalize_config(source)

    def test_bad_scenario_id_is_rejected(self) -> None:
        source = {
            "schema_version": "1.0",
            "metrics_definition_version": "1.0",
            "qos_profiles": {"reliable": {}},
            "conditions": [
                {
                    "condition_id": "bad",
                    "scenario_id": "S00",
                    "scenario_name": "point_to_point_latency",
                    "payload_size_bytes": 1024,
                    "message_count": 10,
                }
            ],
        }
        with self.assertRaisesRegex(ConfigError, "scenario_id"):
            normalize_config(source)

    def test_missing_capability_note_is_rejected(self) -> None:
        source = {
            "schema_version": "1.0",
            "metrics_definition_version": "1.0",
            "qos_profiles": {"reliable": {}},
            "conditions": [
                {
                    "condition_id": "bad",
                    "scenario_id": "S09",
                    "scenario_name": "weak_network_recovery",
                    "payload_size_bytes": 1024,
                    "message_count": 10,
                    "capability": "not_tested",
                }
            ],
        }
        with self.assertRaisesRegex(ConfigError, "capability_note"):
            normalize_config(source)

    def test_payload_size_and_checksum_are_deterministic(self) -> None:
        payload = make_payload_text(1_048_576, 7401, "S02_1MiB")
        self.assertEqual(len(payload.encode("ascii")), 1_048_576)
        self.assertEqual(payload, make_payload_text(1_048_576, 7401, "S02_1MiB"))
        self.assertEqual(crc32_text(payload), crc32_text(payload))

    def test_udpv4_transport_cannot_fall_back_to_shm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            udp_path = render_transport_xml(tmp_path / "udp.xml", "udp", "test")
            udp = udp_path.read_text(encoding="utf-8")
            self.assertIn("<type>UDPv4</type>", udp)
            self.assertIn("<useBuiltinTransports>false</useBuiltinTransports>", udp)
            self.assertIn("<transport_id>benchmark_udpv4</transport_id>", udp)

            shm_path = render_transport_xml(tmp_path / "shm.xml", "shm", "test")
            shm = shm_path.read_text(encoding="utf-8")
            self.assertIn("<type>SHM</type>", shm)
            self.assertIn("<useBuiltinTransports>false</useBuiltinTransports>", shm)

            self.assertIn("UDPv4", transport_detail("udp"))
            self.assertIn("SHM", transport_detail("shm"))

    def test_rate_is_paced_per_publisher(self) -> None:
        # 每发布者口径：每个发布者按自己的序号排队，不再摊到全局槽位。
        self.assertEqual(paced_target_ns(1_000, 9, 1000), 9_001_000)
        self.assertEqual(paced_target_ns(0, 0, 100), 0)
        with self.assertRaises(ValueError):
            paced_target_ns(0, 0, 0)


if __name__ == "__main__":
    unittest.main()
