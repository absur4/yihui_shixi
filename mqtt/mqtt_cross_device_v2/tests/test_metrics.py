import csv
import json
from pathlib import Path
import tempfile
import unittest

from mqtt_cd.analysis import (analyze_run, capacity_verdict, consecutive_jitter,
                              delivery_metrics, quantile)
from mqtt_cd.clock import ClockModel, estimate


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def calibration(node, offset=0, end=40, count=5):
    result = []
    for second in range(0, end + 1, 5):
        center = second * 1_000_000_000
        for index in range(count):
            base = center + index * 1_000_000
            result.append(dict(node_id=node, t1=base - 100_000, t4=base + 100_000,
                               t2=base + offset - 500, t3=base + offset + 500))
    return result


class Fixture:
    def __init__(self, directory, sent=2000, subscribers=1, validated=True,
                 rate=100, duration=20, scenario="CD1"):
        self.directory = Path(directory)
        self.start, self.offset = 10_000_000_000, 2_000_000_000
        self.spec = dict(run_id="fixture", nodes=dict(sender="A", receiver="B"), rate_hz=rate,
                         payload_bytes=1024, publishers=1, subscribers=subscribers,
                         duration_s=duration, drain_s=2, sample_interval_s=.2,
                         scenario=scenario, direction="A_to_B", reference_start_ns=self.start,
                         clock=dict(model_validated=validated, drift_bound_ppm=0,
                                    interval_s=5, latency_limit_ms=1, window_limit_ms=20))
        self.clocks = calibration("A", end=math_end(duration)) + calibration("B", self.offset, math_end(duration))
        self.sender = self.directory / "nodes" / "A"
        self.receiver = self.directory / "nodes" / "B"
        for node, start, folder, role in (("A", self.start, self.sender, "publisher"),
                                          ("B", self.start + self.offset, self.receiver, "subscriber")):
            folder.mkdir(parents=True)
            write_json(folder / "arm.json", dict(start_ns=start))
            write_json(folder / "node.json", dict(node_id=node, role=role))
            write_json(folder / "agent_status.json", dict(state="completed"))
            count = 1 if node == "A" else subscribers
            for index in range(count):
                write_json(folder / f"worker-{role}-{index}.summary.json", dict(status="completed", errors=[]))
            with (folder / "resources.jsonl").open("w", encoding="utf-8") as stream:
                for index in range(int(duration * 5) + 1):
                    roles = {role: dict(cpu_percent=10, memory_mb=20)}
                    if node == "A":
                        roles["infrastructure"] = dict(cpu_percent=0, memory_mb=0)
                    stream.write(json.dumps(dict(time_ns=start + index * 200_000_000,
                                                roles=roles,
                                                host_cpu_percent=5, rss_total_mb=20)) + "\n")
        self.sent = sent
        self.source_rows = [[sequence, self.start + int(sequence / rate * 1e9),
                             self.start + int(sequence / rate * 1e9) + 1000, 1, 0, 1, 1024, .001]
                            for sequence in range(sent)]
        self.write_source()
        for index in range(subscribers):
            self.write_receiver(index, [self.receipt(sequence) for sequence in range(sent)])

    def write_source(self):
        with (self.sender / "publisher-0.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["sequence", "send_ns", "return_ns", "accepted", "rc", "phase", "payload_bytes", "api_ms"])
            writer.writerows(self.source_rows)
        formal = [row for row in self.source_rows if row[5] == 1]
        write_json(self.sender / "worker-publisher-0.summary.json",
                   dict(status="completed", errors=[], accepted=sum(row[3] == 1 for row in formal),
                        api_rejected=sum(row[3] == 0 for row in formal), queue_rejected=0))

    def receipt(self, sequence, receive_ns=None):
        sent = self.start + int(sequence / self.spec["rate_hz"] * 1e9)
        return [0, sequence, sent, receive_ns if receive_ns is not None else sent + self.offset + 1_000_000,
                1, "ok", 1024, 1088]

    def write_receiver(self, index, rows):
        rows = list(rows)
        with (self.receiver / f"subscriber-{index}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["publisher_id", "sequence", "send_ns", "receive_ns", "phase", "validation",
                             "payload_bytes", "wire_bytes"])
            writer.writerows(rows)
        write_json(self.receiver / f"worker-subscriber-{index}.summary.json",
                   dict(status="completed", errors=[], received=len(rows),
                        invalid=sum(row[5] != "ok" for row in rows)))

    def analyze(self):
        return analyze_run(self.directory, self.spec, self.clocks)


def math_end(duration):
    return int((duration + 20) / 5) * 5


class FormulaTests(unittest.TestCase):
    def test_golden_delivery_examples(self):
        cases = [(20000, 4, 80000, 80000, 1, 1, 1, 0, 0, 32.768),
                 (14000, 1, 14000, 14000, .7, 1, .7, 0, 0, 5.7344),
                 (19800, 1, 19602, 19800, .99, .99, .9801, 0, .01, 8.0289792),
                 (20000, 1, 19800, 20000, 1, .99, .99, 0, .01, 8.11008),
                 (20000, 1, 0, 0, 1, 0, 0, 1, 0, 0),
                 (0, 1, 0, 0, 0, None, 0, None, None, 0)]
        for sent, subscribers, window, final, send, delivery, target, missing, drain, mbps in cases:
            with self.subTest(sent=sent, window=window, subscribers=subscribers):
                metrics = delivery_metrics(20000, sent, subscribers, window, final, 20, 1024)
                for field, expected in (("send_achievement_ratio", send), ("delivery_achievement_ratio", delivery),
                                        ("end_to_end_achievement_ratio", target), ("final_missing_ratio", missing),
                                        ("drain_delivery_ratio", drain), ("throughput_mbps", mbps)):
                    if expected is None:
                        self.assertIsNone(metrics[field])
                    else:
                        self.assertAlmostEqual(metrics[field], expected)
                if sent:
                    self.assertAlmostEqual(metrics["end_to_end_achievement_ratio"],
                                           metrics["send_achievement_ratio"] * metrics["delivery_achievement_ratio"])

    def test_quantiles_and_consecutive_jitter(self):
        self.assertAlmostEqual(quantile([1, 2, 3, 4], .95), 3.85)
        self.assertAlmostEqual(quantile([1, 2, 3, 4], .99), 3.97)
        self.assertEqual(consecutive_jitter([(0, 1), (2, 10), (3, 12)]),
                         dict(jitter_ms=2, jitter_sample_count=1))

    def test_boundary_interval_and_runtime_failure(self):
        self.assertEqual(capacity_verdict(.9895, .9905, 0, True, True), "indeterminate")
        self.assertEqual(capacity_verdict(1, 1, 0, False, True), "indeterminate")
        self.assertEqual(capacity_verdict(None, None, None, False, False, True), "fail")


class ClockTests(unittest.TestCase):
    def test_four_timestamps_and_piecewise_mapping(self):
        samples = calibration("B", 2_000_000_000)
        estimate_result = estimate(samples)
        self.assertEqual(estimate_result["offset_ns"], 2_000_000_000)
        self.assertEqual(estimate_result["rtt_ns"], 199000)
        model = ClockModel(samples, dict(model_validated=True, drift_bound_ppm=0))
        corrected, error, reason = model.map(14_000_000_000)
        self.assertAlmostEqual(corrected, 12_000_000_000)
        self.assertEqual(error, 99500)
        self.assertIsNone(reason)
        self.assertTrue(model.coverage(12_000_000_000, 32_000_000_000)["valid"])

    def test_unvalidated_sparse_gaps_and_extrapolation(self):
        model = ClockModel(calibration("A"))
        self.assertFalse(model.coverage(10_000_000_000, 20_000_000_000)["valid"])
        self.assertIsNotNone(model.map(-1_000_000_000)[2])
        sparse = ClockModel(calibration("A", count=1), dict(model_validated=True))
        self.assertIn("insufficient_exchanges_in_calibration_burst", sparse.coverage(1e9, 4e9)["reasons"])
        gap_samples = [sample for sample in calibration("A") if sample["t1"] < 0 or sample["t1"] > 30e9]
        gap = ClockModel(gap_samples, dict(model_validated=True))
        self.assertIsNotNone(gap.map(20e9)[2])


class AnalysisTests(unittest.TestCase):
    def test_complete_two_node_run_and_resource_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            result = fixture.analyze()
            self.assertEqual(result["capacity_verdict"], "pass")
            self.assertEqual(result["execution_status"], "completed")
            self.assertEqual(result["counts"]["N_sent"], 2000)
            self.assertAlmostEqual(result["metrics"]["latency_p99_ms"], 1)
            self.assertEqual(result["resources"]["peak_sum_mb"], 40)
            self.assertEqual(result["resources"]["synchronous_peak_mb"], 40)

    def test_unvalidated_clock_keeps_counts_and_local_throughput(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = Fixture(temporary, validated=False).analyze()
            self.assertEqual(result["capacity_verdict"], "indeterminate")
            self.assertEqual(result["counts"]["N_recv_win"], 2000)
            self.assertGreater(result["metrics"]["throughput_mbps"], 0)
            self.assertIsNone(result["metrics"]["latency_ms"])
            self.assertIsNone(result["resources"]["synchronous_peak_mb"])

    def test_no_sends_is_not_missing_source_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=0)
            complete = fixture.analyze()
            self.assertEqual(complete["metrics"]["throughput_mbps"], 0)
            self.assertEqual(complete["metrics"]["send_achievement_ratio"], 0)
            self.assertIsNone(complete["metrics"]["final_missing_ratio"])
            (fixture.sender / "publisher-0.csv").unlink()
            missing = fixture.analyze()
            self.assertIsNone(missing["counts"]["N_sent"])
            self.assertIsNone(missing["metrics"]["throughput_mbps"])
            self.assertEqual(missing["capacity_verdict"], "indeterminate")

    def test_missing_receiver_is_not_zero_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=20)
            fixture.write_receiver(0, [])
            empty = fixture.analyze()
            self.assertEqual(empty["metrics"]["final_missing_ratio"], 1)
            (fixture.receiver / "subscriber-0.csv").unlink()
            missing = fixture.analyze()
            self.assertIsNone(missing["metrics"]["final_missing_ratio"])
            self.assertIsNone(missing["counts"]["N_recv_win"])

    def test_fanout_dedup_drain_cutoff_and_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=3, subscribers=2)
            end = fixture.start + fixture.offset + 20_000_000_000
            fixture.write_receiver(0, [fixture.receipt(0), fixture.receipt(0), fixture.receipt(2),
                                        fixture.receipt(1, end + 1_000_000_000)])
            fixture.write_receiver(1, [fixture.receipt(0), fixture.receipt(2),
                                        fixture.receipt(1, end + 2_000_000_000)])
            result = fixture.analyze()
            self.assertEqual(result["counts"]["N_recv_win"], 4)
            self.assertEqual(result["counts"]["N_recv_final"], 5)
            self.assertEqual(result["per_subscriber"][0]["counts"]["duplicate_count"], 1)
            self.assertEqual(result["per_subscriber"][0]["counts"]["out_of_order_count"], 1)
            self.assertAlmostEqual(result["metrics"]["final_missing_ratio"], 1 / 6)
            self.assertIsNone(result["metrics"]["latency_p95_ms"])
            self.assertEqual(result["latency"]["p95_quality"], "insufficient_samples")

    def test_runtime_error_is_failure_even_without_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=0)
            write_json(fixture.sender / "agent_status.json", dict(state="error", errors=["memory hard limit"]))
            (fixture.sender / "publisher-0.csv").unlink()
            result = fixture.analyze()
            self.assertEqual(result["capacity_verdict"], "fail")
            self.assertEqual(result["failure_origin"], "tested_system")
            self.assertIsNone(result["metrics"]["throughput_mbps"])

    def test_configuration_failure_is_not_capacity_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=0)
            write_json(fixture.sender / "agent_status.json",
                       dict(state="error", failure_origin="configuration", errors=["Broker path invalid"]))
            result = fixture.analyze()
            self.assertEqual(result["capacity_verdict"], "indeterminate")
            self.assertEqual(result["failure_origin"], "experiment_facility")
            self.assertEqual(result["execution_status"], "error")

    def test_queue_rejection_before_publish_is_not_an_api_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=2)
            fixture.write_receiver(0, [fixture.receipt(0)])
            with (fixture.sender / "publisher-0.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["sequence", "send_ns", "return_ns", "accepted", "rc", "phase",
                                 "payload_bytes", "api_ms", "api_called"])
                writer.writerow(fixture.source_rows[0] + [1])
                rejected = list(fixture.source_rows[1])
                rejected[3] = 0
                writer.writerow(rejected + [0])
            write_json(fixture.sender / "worker-publisher-0.summary.json",
                       dict(status="completed", errors=[], accepted=1, api_rejected=0, queue_rejected=1))
            result = fixture.analyze()
            self.assertEqual(result["counts"]["N_attempt"], 1)
            self.assertEqual(result["counts"]["N_sent"], 1)
            self.assertEqual(result["counts"]["local_queue_rejected_before_api"], 1)

    def test_unknown_acceptance_does_not_create_a_known_delivery_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=2)
            fixture.source_rows[1][3] = ""
            fixture.write_source()
            result = fixture.analyze()
            self.assertEqual(result["counts"]["submission_unknown_count"], 1)
            self.assertIsNone(result["counts"]["E"])
            self.assertIsNone(result["metrics"]["delivery_achievement_ratio"])
            self.assertIsNone(result["metrics"]["throughput_mbps"])
            self.assertEqual(result["capacity_verdict"], "indeterminate")

    def test_peak_sum_is_not_a_synchronous_peak(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            for directory, spike in ((fixture.sender, 25), (fixture.receiver, 75)):
                path = directory / "resources.jsonl"
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                rows[spike]["rss_total_mb"] = 100
                path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            result = fixture.analyze()
            self.assertEqual(result["resources"]["peak_sum_mb"], 200)
            self.assertEqual(result["resources"]["synchronous_peak_mb"], 120)

    def test_sealed_truncated_source_cannot_turn_final_loss_into_a_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            fixture.write_receiver(0, [fixture.receipt(sequence) for sequence in range(1997)])
            self.assertEqual(fixture.analyze()["capacity_verdict"], "fail")
            path = fixture.sender / "publisher-0.csv"
            lines = path.read_text().splitlines()
            path.write_text("\n".join(lines[:-3]) + "\n", encoding="utf-8")
            result = fixture.analyze()
            self.assertEqual(result["capacity_verdict"], "indeterminate")
            self.assertIsNone(result["counts"]["N_sent"])
            self.assertIn("sealed_log_count_mismatch", [issue["code"] for issue in result["artifact_issues"]])

    def test_sealed_truncated_receiver_is_measurement_failure_not_message_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            path = fixture.receiver / "subscriber-0.csv"
            lines = path.read_text().splitlines()
            path.write_text("\n".join(lines[:-3]) + "\n", encoding="utf-8")
            result = fixture.analyze()
            self.assertEqual(result["capacity_verdict"], "indeterminate")
            self.assertIsNone(result["counts"]["N_recv_final"])

    def test_seal_counters_use_correct_warmup_and_diagnostic_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            fixture.source_rows.insert(0, [0, fixture.start - 1_000_000_000,
                                           fixture.start - 999_999_000, 1, 0, 0, 1024, .001])
            fixture.write_source()
            rows = [fixture.receipt(sequence) for sequence in range(2000)]
            warmup = fixture.receipt(0, fixture.start + fixture.offset - 1_000_000_000)
            warmup[4] = 0
            diagnostic_duplicate = fixture.receipt(0, fixture.start + fixture.offset + 23_000_000_000)
            fixture.write_receiver(0, [warmup, *rows, diagnostic_duplicate])
            result = fixture.analyze()
            self.assertEqual(result["capacity_verdict"], "pass")
            self.assertEqual(result["counts"]["N_sent"], 2000)
            self.assertEqual(result["per_subscriber"][0]["counts"]["raw_rows"], 2002)

    def test_reanalysis_preserves_controller_cancellation_and_local_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            fixture.spec["local_check"] = True
            write_json(fixture.directory / "controller.json", dict(cancelled=True, errors=["Controller interrupted by user"]))
            result = fixture.analyze()
            self.assertEqual(result["execution_status"], "cancelled")
            self.assertEqual(result["capacity_verdict"], "indeterminate")
            self.assertEqual(result["run_validity"], "partial")
            self.assertTrue(result["local_check"])
            self.assertIn("Local development check; not a two-machine Wi-Fi measurement", result["limitations"])

    def test_controller_error_cannot_reappear_as_a_capacity_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary)
            write_json(fixture.directory / "controller.json", dict(cancelled=False, errors=["A release failed"]))
            result = fixture.analyze()
            self.assertEqual(result["execution_status"], "error")
            self.assertEqual(result["capacity_verdict"], "indeterminate")
            self.assertEqual(result["run_validity"], "partial")

    def test_negative_corrected_latency_invalidates_whole_main_series(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=1000)
            rows = [fixture.receipt(sequence) for sequence in range(1000)]
            rows[10][3] = rows[10][2] + fixture.offset - 100_000
            fixture.write_receiver(0, rows)
            result = fixture.analyze()
            self.assertIsNone(result["metrics"]["latency_ms"])
            self.assertIn("negative_latency_within_uncertainty", result["latency"]["reasons"])

    def test_cd4_source_buckets_do_not_credit_late_previous_cohort(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=300, rate=10, duration=30, scenario="CD4-L")
            rows = [fixture.receipt(sequence) for sequence in range(300)]
            for sequence in (298, 299):
                rows[sequence][3] = fixture.start + fixture.offset + 31_000_000_000 + sequence
            fixture.write_receiver(0, rows)
            result = fixture.analyze()
            self.assertEqual(result["cd4"]["T_hold_s"], 20)
            self.assertEqual(result["cd4"]["max_contiguous_stable_duration_s"], 20)
            self.assertEqual(result["cd4"]["source_cohort_buckets"][2]["worst_target_achievement_ratio"], .98)
            self.assertEqual(result["metrics"]["final_missing_ratio"], 0)
            self.assertEqual(result["cd4"]["sustainability_verdict"], "fail")

    def test_cd4_full_observation_is_censored_at_300_seconds(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = Fixture(temporary, sent=3000, rate=10, duration=300, scenario="CD4-L").analyze()
            self.assertEqual(result["cd4"]["T_hold_s"], 300)
            self.assertTrue(result["cd4"]["hold_censored"])
            self.assertEqual(result["cd4"]["degradation_delta_pp"], 0)
            self.assertEqual(result["cd4"]["backlog_growth_deliveries"], 0)
            self.assertEqual(result["cd4"]["sustainability_verdict"], "pass")

    def test_cd4_bucket_alignment_is_checked_separately_from_whole_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(temporary, sent=300, rate=10, duration=30, scenario="CD4-L")
            write_json(fixture.receiver / "arm.json", dict(start_ns=fixture.start + fixture.offset + 4_000_000))
            for sample in fixture.clocks:
                center = (sample["t1"] + sample["t4"]) // 2
                sample["t1"], sample["t4"] = center - 4_500_000, center + 4_500_000
            result = fixture.analyze()
            self.assertTrue(result["clock_quality"]["window_valid"])
            self.assertEqual(result["cd4"]["hold_status"], "indeterminate")
            self.assertEqual(result["cd4"]["source_cohort_buckets"][0]["window_quality"], "unmeasurable")
            self.assertIsNone(result["cd4"]["source_cohort_buckets"][0]["worst_target_achievement_ratio"])
            self.assertIsNone(result["cd4"]["degradation_slope_pp_per_min"])

    def test_cd4_backlog_uses_actual_acceptance_time_not_planned_sequence(self):
        for receipt_before_return in (False, True):
            with self.subTest(receipt_before_return=receipt_before_return), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(temporary, sent=300, rate=10, duration=30, scenario="CD4-L")
                sent = fixture.start + (9_900_000_000 if receipt_before_return else 10_050_000_000)
                returned = fixture.start + 10_051_000_000
                fixture.source_rows[99][1:3] = [sent, returned]
                fixture.write_source()
                rows = [fixture.receipt(sequence) for sequence in range(300)]
                rows[99][2] = sent
                rows[99][3] = sent + fixture.offset + 1_000_000
                fixture.write_receiver(0, rows)
                result = fixture.analyze()
                first = result["cd4"]["source_cohort_buckets"][0]
                self.assertEqual(first["N_sent"], 100)
                self.assertEqual(first["unfinished_deliveries"], 0)
                self.assertEqual(first["unfinished_lower"], 0)
                self.assertEqual(first["unfinished_upper"], 0)


if __name__ == "__main__":
    unittest.main()
