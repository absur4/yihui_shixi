import tempfile
from pathlib import Path
import unittest

from mqtt_cd.planner import (build_scan, capacity_boundaries, classify_conditions,
                             derive_cd4, next_boundary_cases, next_plateau_cases, summarize_scans)
from mqtt_cd.report import write_report


def condition(rate=100, passed=5, stage="confirmation", duration=60, direction="A_to_B", **extra):
    rows = []
    for repeat in range(1, 6):
        row = dict(scenario="CD1", middleware="MQTT", profile="sender_service_tcp_v2",
                   direction=direction, rate_hz=rate, payload_bytes=1024, subscribers=1,
                   duration_s=duration, stage=stage, repeat=repeat, level=f"r{rate}-{stage}",
                   execution_status="completed", run_validity="valid", run_id=f"{rate}-{stage}-{repeat}",
                   capacity_verdict="pass" if repeat <= passed else "fail",
                   clock_quality=dict(model_validated=True, window_valid=True, latency_valid=True),
                   metrics=dict(throughput_mbps=8.0, latency_p95_ms=1.0))
        row.update(extra)
        rows.append(row)
    return rows


class PlannerTests(unittest.TestCase):
    def test_matrix_is_exact_and_deterministic(self):
        cases = build_scan(["CD1", "CD2", "CD3"], ["A_to_B", "B_to_A"])
        self.assertEqual(len(cases), 140)
        self.assertEqual(cases, build_scan(["CD1", "CD2", "CD3"], ["A_to_B", "B_to_A"]))
        self.assertEqual({c["rate_hz"] for c in cases if c["scenario"] == "CD1"}, {100, 500, 1000, 2000, 5000, 10000})
        self.assertEqual({c["payload_bytes"] for c in cases if c["scenario"] == "CD2"}, {1024, 16384, 65536, 262144, 1048576})
        self.assertTrue(all(c["rate_hz"] == 200 for c in cases if c["scenario"] == "CD2"))
        self.assertEqual([c["repeat"] for c in cases], sorted(c["repeat"] for c in cases))

    def test_four_of_five_and_no_clock_pass(self):
        self.assertEqual(classify_conditions(condition(passed=4))[0]["status"], "stable_pass")
        self.assertEqual(classify_conditions(condition(passed=1))[0]["status"], "stable_fail")
        self.assertEqual(classify_conditions(condition(passed=3))[0]["status"], "unstable_or_insufficient")
        rows = condition(clock_quality={"model_validated": False, "window_valid": True})
        self.assertEqual(classify_conditions(rows)[0]["passes"], 0)
        self.assertEqual(classify_conditions(condition()[:4])[0]["status"], "unstable_or_insufficient")

    def test_repeat_duplicates_do_not_manufacture_passes(self):
        rows = [condition()[0]]*5
        classified = classify_conditions(rows)[0]
        self.assertEqual(classified["slots_present"], 1)
        self.assertEqual(classified["status"], "unstable_or_insufficient")

    def test_environment_fingerprints_never_fill_each_others_slots(self):
        rows = condition(comparison_fingerprint="env-A")[:3]+condition(comparison_fingerprint="env-B")[3:]
        groups = classify_conditions(rows)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(c["status"] == "unstable_or_insufficient" for c in groups))
        rows = condition(comparison_fingerprint="env-A")+condition(comparison_fingerprint="env-B")
        self.assertEqual(len(capacity_boundaries(rows)), 2)
        self.assertFalse(derive_cd4(rows)["cases"])
        self.assertEqual(next_boundary_cases(rows, ["A_to_B"])["skipped"][0]["reason"], "mixed_comparison_fingerprints")

    def test_facility_replacement_requires_explicit_link(self):
        rows = condition(passed=3)
        rows[-1].update(run_validity="invalid", failure_origin="collector")
        replacement = dict(rows[-1], run_id="replacement", capacity_verdict="pass", run_validity="valid",
                           failure_origin=None, replaces_run_id=rows[-1]["run_id"])
        self.assertEqual(classify_conditions(rows+[replacement])[0]["status"], "stable_pass")

    def test_experiment_facility_failure_is_not_a_capacity_failure(self):
        rows = condition(passed=0, failure_origin="experiment_facility", run_validity="invalid")
        self.assertEqual(classify_conditions(rows)[0]["failures"], 0)
        replacement = dict(rows[0], run_id="replacement", capacity_verdict="pass", run_validity="valid",
                           failure_origin=None, replaces_run_id=rows[0]["run_id"])
        self.assertEqual(classify_conditions(rows+[replacement])[0]["passes"], 1)

    def test_coarse_is_not_confirmed_and_nonmonotonic_blocks_reference(self):
        rows = condition(stage="scan", duration=20)
        self.assertIsNone(capacity_boundaries(rows)[0]["R_pass"])
        rows = condition(rate=500, passed=0)+condition(rate=1000)
        bound = capacity_boundaries(rows)[0]
        self.assertEqual(bound["bound_status"], "non_monotonic_or_uncertain")
        self.assertIsNone(bound["R_pass"])
        self.assertFalse(derive_cd4(rows)["cases"])

    def test_confirmation_and_bisection(self):
        coarse = condition(rate=100, stage="scan", duration=20)+condition(rate=500, passed=0, stage="scan", duration=20)
        plan = next_boundary_cases(coarse, ["A_to_B"])
        self.assertEqual({c["rate_hz"] for c in plan["cases"]}, {100, 500})
        self.assertTrue(all(c["duration_s"] == 60 for c in plan["cases"]))
        plan = next_boundary_cases(coarse+condition(100)+condition(500, passed=0), ["A_to_B"])
        self.assertEqual({c["rate_hz"] for c in plan["cases"]}, {300})

    def test_boundary_resume_fills_only_unoccupied_slots(self):
        rows = condition(100, stage="scan", duration=20)+condition(500, passed=0, stage="scan", duration=20)
        rows += condition(100)+condition(500, passed=0)[:2]
        plan = next_boundary_cases(rows, ["A_to_B"])
        self.assertEqual({c["rate_hz"] for c in plan["cases"]}, {500})
        self.assertEqual({c["repeat"] for c in plan["cases"]}, {3, 4, 5})
        self.assertTrue(all(c["level"] == "r500-confirmation" for c in plan["cases"]))

    def test_external_mqtt_reference_cannot_hide_local_conflicts(self):
        rows = condition(500, passed=0)+condition(1000)
        ref = dict(middleware="MQTT", direction="A_to_B", profile="sender_service_tcp_v2",
                   validated=True, bound_status="bounded", R_pass=1000, R_fail=2000)
        plan = derive_cd4(rows, references=[ref])
        self.assertFalse(plan["cases"])

    def test_exploratory_and_coarse_conflicts_are_not_capacity_references(self):
        self.assertEqual(capacity_boundaries(condition(measurement_kind="exploratory")), [])
        self.assertEqual(capacity_boundaries(condition(exploratory=True)), [])
        self.assertEqual(capacity_boundaries(condition(local_check=True)), [])
        self.assertEqual(capacity_boundaries(condition(spec={"local_check": True}, measurement_kind="formal")), [])
        rows = condition(stage="scan", duration=20)+condition(passed=0)
        boundary = capacity_boundaries(rows)[0]
        self.assertEqual(boundary["bound_status"], "non_monotonic_or_uncertain")
        self.assertEqual(boundary["coarse_confirmation_conflicts"], [100])

    def test_refinement_limit_and_fixed_extensions(self):
        rows = condition(100, stage="scan", duration=20)+condition(500, passed=0, stage="scan", duration=20)
        rows += condition(100)+condition(500, passed=0)
        for rate in (300, 400, 450):
            rows += condition(rate, stage="refinement")
        self.assertFalse(next_boundary_cases(rows, ["A_to_B"])["cases"])
        rows = sum((condition(rate, stage="scan", duration=20) for rate in (100, 500, 1000, 2000, 5000, 10000)), [])
        rows += condition(10000)
        self.assertEqual({c["rate_hz"] for c in next_boundary_cases(rows, ["A_to_B"])["cases"]}, {20000})
        rows += condition(20000, stage="extension")
        self.assertEqual({c["rate_hz"] for c in next_boundary_cases(rows, ["A_to_B"])["cases"]}, {40000})

    def test_cd4_complete_common_and_independent_conditions(self):
        own = condition(1000)+condition(2000, passed=0)+condition(500, direction="B_to_A")
        refs = [dict(middleware=m, direction=d, R_pass=800, R_fail=1600, validated=True,
                     profile="sender_service_tcp_v2", bound_status="bounded")
                for m in ("VSOA", "Zenoh") for d in ("A_to_B", "B_to_A")]
        plan = derive_cd4(own, refs)
        self.assertEqual(len(plan["cases"]), 30)
        self.assertEqual({c["rate_hz"] for c in plan["cases"] if c["scenario"] == "CD4-C"}, {400})
        self.assertEqual({c["rate_hz"] for c in plan["cases"] if c["scenario"] == "CD4-H" and c["direction"] == "A_to_B"}, {2400})
        reverse = [c for c in plan["cases"] if c["scenario"] == "CD4-H" and c["direction"] == "B_to_A"]
        self.assertTrue(all(c["high_load_only"] for c in reverse))
        self.assertTrue(all(c["duration_s"] == 300 and c["warmup_s"] == 10 and c["drain_s"] == 5 for c in plan["cases"]))

    def test_missing_common_and_manual_exploratory(self):
        self.assertFalse(derive_cd4([])["cases"])
        plan = derive_cd4([], manual_rates={"A_to_B": {"CD4-L": 123}})
        self.assertEqual(len(plan["cases"]), 5)
        self.assertTrue(all(c["exploratory"] and c["load_source"] == "manual_exploratory" for c in plan["cases"]))
        self.assertEqual(len(plan["skipped"]), 5)
        manual_common = derive_cd4([], manual_rates={"A_to_B": {"CD4-C": 77}})
        self.assertEqual(len(manual_common["cases"]), 5)
        self.assertTrue(all(c["reference"] == {"manual_rate_hz": 77} for c in manual_common["cases"]))

    def test_cd4_preserves_seed_and_repeat_blocks(self):
        manual = {direction: {"CD4-L": 100, "CD4-H": 500} for direction in ("A_to_B", "B_to_A")}
        cases = derive_cd4([], manual_rates=manual, seed=42)["cases"]
        self.assertTrue(all(case["random_seed"] == 42 for case in cases))
        self.assertEqual(cases, derive_cd4([], manual_rates=manual, seed=42)["cases"])
        self.assertEqual([case["repeat"] for case in cases], sorted(case["repeat"] for case in cases))

    def test_latency_knee_and_plateau_not_faked_from_collapse(self):
        rows = condition(100, stage="scan", duration=20)
        for rate in (500, 1000):
            rows += condition(rate, stage="scan", duration=20, metrics={"latency_p95_ms": 4.0})
        self.assertEqual(summarize_scans(rows)["latency_knees"][0]["R_knee"], 500)
        rows = []
        for size, throughput in ((1024, 100), (16384, 95), (65536, 70)):
            rows += condition(200, stage="scan", duration=20, scenario="CD2", payload_bytes=size,
                              level=f"b{size}", metrics={"throughput_mbps": throughput})
        plateau = summarize_scans(rows)["payload_plateaus"][0]
        self.assertIsNone(plateau["B_plateau"])
        self.assertEqual(plateau["status"], "collapse")

    def test_report_escapes_data_and_does_not_need_assets(self):
        row = condition()[0]
        row["limitations"] = ["<script>alert('x')</script>"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"report.html"
            write_report(path, {"runs": [row], "status": "cancelled"})
            html = path.read_text(encoding="utf-8")
            self.assertNotIn("<script>", html)
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("不可测 / 未提供", html)
            self.assertIn("A_to_B", html)
            self.assertNotIn("https://", html)
            self.assertIn("<td>cancelled</td>", html)

    def test_report_keeps_prior_coarse_conflict_in_boundary_summary(self):
        prior = condition(100, stage="scan", duration=20)+condition(500, passed=0, stage="scan", duration=20)
        current = condition(100)+condition(500)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"followup.html"
            write_report(path, {"reference_runs": prior, "runs": current, "status": "completed"})
            html = path.read_text(encoding="utf-8")
            self.assertIn("non_monotonic_or_uncertain", html)
            self.assertIn("此前 10 轮引用证据", html)

    def test_plateau_requires_overload_and_long_confirmation(self):
        rows = []
        for size, throughput in ((16384, 100), (65536, 101), (262144, 102)):
            rows += condition(200, stage="scan", duration=20, scenario="CD2", payload_bytes=size,
                              level=f"b{size}", metrics={"throughput_mbps": throughput},
                              per_subscriber=[{"achievement_upper": .9}])
        plan = next_plateau_cases(rows)
        self.assertEqual(len(plan["cases"]), 15)
        self.assertIsNone(summarize_scans(rows)["payload_plateaus"][0]["B_plateau"])
        for size, throughput in ((16384, 100), (65536, 101), (262144, 102)):
            rows += condition(200, stage="confirmation", scenario="CD2", payload_bytes=size,
                              level=f"b{size}", metrics={"throughput_mbps": throughput},
                              per_subscriber=[{"achievement_upper": .9}])
        plateau = summarize_scans(rows)["payload_plateaus"][0]
        self.assertEqual(plateau["B_plateau"], 101)
        self.assertEqual(plateau["status"], "confirmed")

    def test_plateau_resume_does_not_repeat_completed_failure_slots(self):
        rows = []
        for size, throughput in ((16384, 100), (65536, 101), (262144, 102)):
            kwargs = dict(scenario="CD2", payload_bytes=size, level=f"b{size}",
                          metrics={"throughput_mbps": throughput}, per_subscriber=[{"achievement_upper": .9}])
            rows += condition(200, stage="scan", duration=20, **kwargs)
            rows += condition(200, passed=0, **kwargs)[:4]
        self.assertIsNone(summarize_scans(rows)["payload_plateaus"][0]["B_plateau"])
        plan = next_plateau_cases(rows)
        self.assertEqual(len(plan["cases"]), 3)
        self.assertEqual({c["repeat"] for c in plan["cases"]}, {5})
        self.assertEqual({c["payload_bytes"] for c in plan["cases"]}, {16384, 65536, 262144})


if __name__ == "__main__":
    unittest.main()
