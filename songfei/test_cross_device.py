import unittest

from cross_device import (
    CATALOG,
    build_cases,
    condition_verdict,
    generate_cd4_rates,
    latency_metrics,
    normalize_run,
)
from cross_device_runner import prepare_vsoa_cases


class CrossDeviceContractTests(unittest.TestCase):
    def test_catalog_matches_seventeen_conditions(self):
        self.assertEqual(17, len(CATALOG))
        self.assertEqual({"CD1": 6, "CD2": 5, "CD3": 3, "CD4": 3}, {
            name: sum(case["scenario_name"] == name for case in CATALOG)
            for name in ("CD1", "CD2", "CD3", "CD4")
        })

    def test_cd1_values_are_frozen_and_direction_is_recorded(self):
        case = build_cases(0, {"publish_rate_hz": 9999, "direction": "B_TO_A"})[0]
        self.assertEqual(100, case["publish_rate_hz"])
        self.assertEqual("B_TO_A", case["direction"])

    def test_cd4_placeholder_cannot_be_run(self):
        with self.assertRaisesRegex(ValueError, "CD1"):
            build_cases(14, {"publish_rate_hz": 1})

    def test_fixed_window_fanout_metrics(self):
        case = {**CATALOG[13], "direction": "A_TO_B"}
        result = normalize_run({
            "status": "completed",
            "messages_attempted": 20000,
            "messages_sent": 20000,
            "messages_received_in_send_window": 80000,
            "messages_received_after_recovery": 80000,
            "clock_synchronization": {"maximum_uncertainty_ms": .4},
            "latency_p95_ms": 2,
        }, case)
        self.assertAlmostEqual(8.192, result["offered_throughput_mbps"])
        self.assertAlmostEqual(32.768, result["offered_delivery_throughput_mbps"])
        self.assertAlmostEqual(32.768, result["throughput_mbps"])
        self.assertEqual(1, result["end_to_end_achievement_ratio"])
        self.assertEqual("pass", result["capacity_verdict"])

    def test_missing_counts_remain_unknown(self):
        result = normalize_run({"status": "unsupported"}, CATALOG[0])
        self.assertIsNone(result["messages_sent"])
        self.assertIsNone(result["final_target_achievement_ratio"])
        self.assertEqual("invalid", result["run_validity"])

    def test_fanout_verdict_uses_worst_subscriber(self):
        case = {**CATALOG[13], "direction": "A_TO_B"}
        result = normalize_run({
            "status": "completed", "messages_attempted": 20000, "messages_sent": 20000,
            "messages_received": 79700, "messages_received_after_recovery": 79980,
            "subscriber_reports": [
                {"measurement_window_received": 20000, "final_received": 20000},
                {"measurement_window_received": 20000, "final_received": 20000},
                {"measurement_window_received": 20000, "final_received": 20000},
                {"measurement_window_received": 19700, "final_received": 19980},
            ],
        }, case)
        self.assertEqual("fail", result["capacity_verdict"])
        self.assertEqual(.985, result["per_link_target_achievement_ratio"][-1])
        self.assertAlmostEqual(.001, result["per_link_final_missing_ratio"][-1])

    def test_jitter_does_not_bridge_sequence_gap(self):
        metrics = latency_metrics([
            {"sequence": 1, "latency_ms": 1},
            {"sequence": 2, "latency_ms": 3},
            {"sequence": 4, "latency_ms": 100},
        ])
        self.assertEqual(1, metrics["jitter_pair_count"])
        self.assertEqual(2, metrics["jitter_ms"])

    def test_stable_decision_and_cd4_generation(self):
        runs = [{"capacity_verdict": value, "run_validity": "valid"}
                for value in ("pass", "pass", "fail", "pass", "pass")]
        self.assertEqual("stable_pass", condition_verdict(runs))
        self.assertEqual({
            "CD4-C_common_load": 800,
            "CD4-L_relative_stable": 1800,
            "CD4-H_pressure": 6000,
            "high_load_only": False,
        }, generate_cd4_rates(1000, 2000, 5000))

    def test_vsoa_matrix_uses_unique_engine_names_and_remote_roles(self):
        source = [{**CATALOG[0], "direction": "A_TO_B"},
                  {**CATALOG[1], "direction": "A_TO_B"},
                  {**CATALOG[13], "direction": "A_TO_B"}]
        prepared = prepare_vsoa_cases(source, {"agents": ["S", "R"], "token": "secret"})
        self.assertEqual(3, len({case["scenario_name"] for case in prepared}))
        self.assertEqual("CD1", prepared[0]["scenario_group"])
        self.assertEqual([0], prepared[2]["_distributed"]["publisher_agents"])
        self.assertEqual([1, 1, 1, 1], prepared[2]["_distributed"]["subscriber_agents"])


if __name__ == "__main__":
    unittest.main()
