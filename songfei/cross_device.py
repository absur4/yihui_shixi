"""Cross-device CD1-CD4 catalog and metric-definition 2.0 helpers.

This module is intentionally middleware-neutral.  It owns the experiment
namespace and fixed-window arithmetic so local S01-S12 results cannot be
silently mixed with cross-device results.
"""

from __future__ import annotations

import math
from copy import deepcopy


METRIC_VERSION = "2.0"
PROFILE = "sender_service_tcp_v2"
SCENARIO_NAMES = {
    "CD1": "消息速率扫描",
    "CD2": "载荷扫描",
    "CD3": "单远端主机多订阅实例扇出",
    "CD4": "持续负载验证",
}


def _case(scenario, condition, rate, payload=1024, subscribers=1, duration=20,
          phase="scan", generated=False):
    return {
        "scenario_name": scenario,
        "scenario_id": scenario,
        "case": condition,
        "condition_id": condition,
        "title": SCENARIO_NAMES[scenario],
        "phase": phase,
        "profile": PROFILE,
        "metric_definition_version": METRIC_VERSION,
        "payload_size_bytes": payload,
        "message_size_bytes": payload,
        "publish_rate_hz": rate,
        "publisher_count": 1,
        "subscriber_count": subscribers,
        "message_count": 0,
        "duration_seconds": duration,
        "repeats": 5,
        "case_repeats": 5,
        "random_seed": 20260916,
        "seed": 20260916,
        "warmup_seconds": 10 if scenario == "CD4" else 5,
        "drain_seconds": 5 if scenario == "CD4" else 2,
        "timeout_seconds": 30,
        "startup_timeout_seconds": 30,
        "transport_mode": "tcp",
        "qos_profile": PROFILE,
        "network_delay_ms": 0,
        "network_jitter_ms": 0,
        "network_loss_rate": 0,
        "loss_rate": 0,
        "bucket_seconds": 10 if scenario == "CD4" else None,
        "application_header_bytes": 64,
        "application_queue_max_messages": 1024,
        "application_queue_max_bytes": 64 * 1024 * 1024,
        "scheduler_policy": "fixed_schedule_skip_missed_no_catch_up",
        "clock_probe_samples": 9,
        "clock_probe_interval_seconds": 5,
        "clock_uncertainty_limit_ms": 1,
        "generated_from_boundary": generated,
    }


def catalog():
    cases = []
    for rate in (100, 500, 1000, 2000, 5000, 10000):
        cases.append(_case("CD1", f"CD1_rate_{rate}Hz", rate))
    for kib in (1, 16, 64, 256, 1024):
        cases.append(_case("CD2", f"CD2_payload_{kib}KiB", 200, kib * 1024))
    for subscribers in (1, 2, 4):
        cases.append(_case("CD3", f"CD3_fanout_{subscribers}", 1000,
                           subscribers=subscribers))
    # These three rates must be replaced by confirmed CD1 boundaries before a
    # formal run.  Keeping them in the catalog makes the UI/contract explicit.
    cases.extend([
        _case("CD4", "CD4-C_common_load", 1, duration=300,
              phase="stability_common", generated=True),
        _case("CD4", "CD4-L_relative_stable", 1, duration=300,
              phase="stability_relative", generated=True),
        _case("CD4", "CD4-H_pressure", 1, duration=300,
              phase="stability_pressure", generated=True),
    ])
    return cases


CATALOG = catalog()


def build_cases(template_index, values, matrix=False):
    try:
        index = int(template_index)
        template = CATALOG[index]
    except (TypeError, ValueError, IndexError):
        raise ValueError("无效的跨设备测试条件") from None
    selected = ([item for item in CATALOG if item["scenario_name"] == template["scenario_name"]]
                if matrix else [template])
    output = []
    for source in selected:
        case = deepcopy(source)
        # Formal CD1-CD3 values are frozen.  CD4 rate is generated from CD1 and
        # is therefore the only load field users may provide at launch time.
        if case["scenario_name"] == "CD4":
            rate = int(float((values or {}).get("publish_rate_hz", case["publish_rate_hz"])))
            if rate <= 1:
                raise ValueError("CD4 速率必须先由已确认的 CD1 边界生成，不能使用占位值 1 Hz")
            case["publish_rate_hz"] = rate
        case["direction"] = str((values or {}).get("direction") or "A_TO_B")
        if case["direction"] not in {"A_TO_B", "B_TO_A"}:
            raise ValueError("跨设备方向必须是 A_TO_B 或 B_TO_A")
        output.append(case)
    return output


def _ratio(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator else None


def percentile(values, fraction):
    """Linear-interpolated percentile used by metric definition 2.0."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * float(fraction)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_metrics(samples):
    """Calculate latency and sequence-adjacent jitter from valid samples.

    Jitter never bridges a missing sequence number.  Samples may provide
    ``latency_ms`` directly, or corrected ``send_ns``/``arrival_ns`` values.
    """
    points = []
    for item in samples or []:
        sequence = item.get("sequence", item.get("sequence_id",
                            item.get("sequence_number", item.get("seq"))))
        latency = item.get("latency_ms")
        if latency is None and item.get("send_ns") is not None and item.get("arrival_ns") is not None:
            latency = (float(item["arrival_ns"]) - float(item["send_ns"])) / 1e6
        if sequence is not None and latency is not None:
            points.append((int(sequence), float(latency)))
    points.sort(key=lambda pair: pair[0])
    values = [value for _, value in points]
    adjacent = [abs(points[index][1] - points[index - 1][1])
                for index in range(1, len(points))
                if points[index][0] == points[index - 1][0] + 1]
    if not values:
        return {}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "latency_ms": mean,
        "latency_p50_ms": percentile(values, .50),
        "latency_p95_ms": percentile(values, .95),
        "latency_p99_ms": percentile(values, .99),
        "latency_std_ms": math.sqrt(variance),
        "jitter_ms": sum(adjacent) / len(adjacent) if adjacent else None,
        "latency_sample_count": len(values),
        "jitter_pair_count": len(adjacent),
    }


def normalize_run(run, case):
    """Map an engine run to the fixed-window 2.0 contract.

    Unknown evidence stays null.  It is never inferred from receiver counts or
    replaced with zero, as required by the specification.
    """
    result = dict(run)
    duration = float(case["duration_seconds"])
    payload = int(case["payload_size_bytes"])
    publishers = int(case.get("publisher_count", 1))
    subscribers = int(case.get("subscriber_count", 1))
    planned = int(case["publish_rate_hz"] * duration * publishers)
    sent = result.get("messages_sent")
    attempted = result.get("messages_attempted", sent)
    received_window = result.get("messages_received_in_send_window")
    if received_window is None:
        received_window = result.get("messages_received")
    received_final = result.get("messages_received_after_recovery")
    if received_final is None:
        received_final = result.get("messages_received")
    sent = int(sent) if sent is not None else None
    attempted = int(attempted) if attempted is not None else None
    received_window = int(received_window) if received_window is not None else None
    received_final = int(received_final) if received_final is not None else None
    expected = sent * subscribers if sent is not None else None
    expected_plan = planned * subscribers
    link_window_counts = []
    link_final_counts = []
    for item in result.get("subscriber_reports") or []:
        if item.get("measurement_window_received") is not None:
            link_window_counts.append(int(item["measurement_window_received"]))
            link_final_counts.append(int(item.get("final_received", item["measurement_window_received"])))
    if not link_window_counts:
        for item in result.get("delivery_matrix") or result.get("link_metrics") or []:
            if item.get("received") is not None:
                link_window_counts.append(int(item["received"]))
                link_final_counts.append(int(item.get("final_received", item["received"])))
    per_link_target = [_ratio(value, planned) for value in link_window_counts]
    per_link_final_missing = [1 - _ratio(value, sent) for value in link_final_counts
                              if sent is not None and sent > 0]

    count_valid = all(value is not None for value in (sent, attempted, received_window, received_final))
    if count_valid:
        count_valid = (0 <= received_window <= received_final <= expected
                       and sent <= attempted <= planned)
    throughput = (received_window * payload * 8 / duration / 1e6
                  if received_window is not None else None)
    send_ratio = _ratio(sent, planned) if sent is not None else None
    delivery_ratio = _ratio(received_window, expected) if received_window is not None else None
    target_ratio = _ratio(received_window, expected_plan) if received_window is not None else None
    final_ratio = _ratio(received_final, expected) if received_final is not None else None

    result.update({
        "schema_version": "2.0",
        "metric_definition_version": METRIC_VERSION,
        "profile": PROFILE,
        "scenario_id": case["scenario_name"],
        "scenario_name": case["condition_id"],
        "condition_id": case["condition_id"],
        "direction": case.get("direction"),
        "configuration": dict(case),
        "measurement_duration_seconds": duration,
        "payload_size_bytes": payload,
        "publish_rate_hz": case["publish_rate_hz"],
        "transport_mode": "tcp",
        "qos_profile": PROFILE,
        "execution_mode": "multi_machine",
        "publisher_count": publishers,
        "subscriber_count": subscribers,
        "messages_planned": planned,
        "messages_attempted": attempted,
        "messages_sent": sent,
        "unique_deliveries": received_window,
        "messages_received": received_window,
        "messages_received_in_send_window": received_window,
        "messages_received_after_recovery": received_final,
        "expected_deliveries": expected,
        "planned_deliveries": expected_plan,
        "actual_publish_rate_hz": sent / duration if sent is not None else None,
        "achieved_publish_rate_hz": sent / duration if sent is not None else None,
        "target_offered_throughput_mbps": planned * payload * 8 / duration / 1e6,
        "offered_throughput_mbps": sent * payload * 8 / duration / 1e6 if sent is not None else None,
        "target_delivery_throughput_mbps": expected_plan * payload * 8 / duration / 1e6,
        "offered_delivery_throughput_mbps": expected * payload * 8 / duration / 1e6 if expected is not None else None,
        "throughput_mbps": throughput,
        "send_achievement_ratio": send_ratio,
        "delivery_achievement_ratio": delivery_ratio,
        "end_to_end_achievement_ratio": target_ratio,
        "window_missing_ratio": None if delivery_ratio is None else 1 - delivery_ratio,
        "final_missing_ratio": None if final_ratio is None else 1 - final_ratio,
        "drain_delivery_ratio": (_ratio(received_final - received_window, expected)
                                  if received_final is not None and received_window is not None else None),
        "final_target_achievement_ratio": _ratio(received_final, expected_plan),
        "per_link_target_achievement_ratio": per_link_target or None,
        "per_link_final_missing_ratio": per_link_final_missing or None,
        # Explicit compatibility aliases; 1.0 and 2.0 results must still be
        # separated by metric_definition_version.
        "packet_loss": None if delivery_ratio is None else 1 - delivery_ratio,
        "final_packet_loss": None if final_ratio is None else 1 - final_ratio,
        "count_invariants_valid": count_valid,
    })
    if result.get("latency_samples"):
        result.update(latency_metrics(result["latency_samples"]))
    result.update(verdict(result))
    return result


def condition_verdict(runs):
    """Apply the prescribed 4-of-5 stable boundary decision."""
    decisions = [run.get("capacity_verdict") for run in runs
                 if run.get("run_validity") != "invalid"]
    passes = decisions.count("pass")
    failures = decisions.count("fail")
    if passes >= 4:
        return "stable_pass"
    if failures >= 4:
        return "stable_fail"
    return "unstable_or_insufficient"


def generate_cd4_rates(common_pass_rate, own_pass_rate, own_fail_rate=None,
                       highest_tested_rate=None):
    """Generate C/L/H loads after confirmed CD1 boundaries."""
    if common_pass_rate is None or own_pass_rate is None:
        raise ValueError("CD4 requires confirmed common and middleware-specific R_pass")
    high_load_only = own_fail_rate is None
    pressure_base = own_fail_rate if own_fail_rate is not None else highest_tested_rate
    if pressure_base is None:
        raise ValueError("CD4-H requires R_fail or the highest tested CD1 rate")
    return {
        "CD4-C_common_load": max(1, math.floor(.8 * common_pass_rate)),
        "CD4-L_relative_stable": max(1, math.floor(.9 * own_pass_rate)),
        "CD4-H_pressure": max(1, math.ceil(1.2 * pressure_base)),
        "high_load_only": high_load_only,
    }


def verdict(run):
    status = run.get("status")
    if status in {"unsupported", "not_tested", "cancelled", "interrupted"}:
        return {"run_validity": "invalid", "capacity_verdict": "indeterminate",
                "metric_quality": {"counts": "incomplete", "latency": "unmeasurable"}}
    if status not in {"completed", None}:
        return {"run_validity": "partial", "capacity_verdict": "fail",
                "metric_quality": {"counts": "incomplete", "latency": "unmeasurable"}}
    if not run.get("count_invariants_valid"):
        return {"run_validity": "invalid", "capacity_verdict": "indeterminate",
                "metric_quality": {"counts": "incomplete", "latency": "unmeasurable"}}
    link_targets = run.get("per_link_target_achievement_ratio") or []
    link_final = run.get("per_link_final_missing_ratio") or []
    target = min(link_targets) if link_targets else run.get("end_to_end_achievement_ratio")
    final_missing = max(link_final) if link_final else run.get("final_missing_ratio")
    capacity = ("pass" if target is not None and target >= .99
                and final_missing is not None and final_missing <= .001 + 1e-12 else "fail")
    synchronization = run.get("clock_synchronization") or {}
    clock = (synchronization.get("maximum_uncertainty_ms")
             if synchronization.get("maximum_uncertainty_ms") is not None
             else (run.get("environment") or {}).get("max_clock_uncertainty_ms"))
    # A small single probe is not enough for a 2.0 latency claim.  Agents must
    # explicitly attest the pre/during/post direct S-R piecewise-linear model.
    clock_compliant = bool(synchronization.get("cross_device_v2_compliant"))
    latency_quality = ("valid" if clock_compliant and clock is not None and clock <= 1
                       else "unmeasurable")
    return {"run_validity": "valid" if capacity == "pass" else "partial",
            "capacity_verdict": capacity,
            "latency_slo_verdict": ("unmeasurable" if latency_quality != "valid"
                                    else "pass" if (run.get("latency_p95_ms") or math.inf) <= 50 else "fail"),
            "metric_quality": {"counts": "valid", "capacity": "valid",
                               "latency": latency_quality}}


def normalize_suite(report, cases):
    by_name = {case["condition_id"]: case for case in cases}
    normalized = []
    for run in report.get("runs") or []:
        name = run.get("condition_id") or run.get("scenario_name")
        case = by_name.get(name)
        if case is None and len(cases) == 1:
            case = cases[0]
        normalized.append(normalize_run(run, case) if case else run)
    output = dict(report)
    output.update({"schema_version": "2.0", "metric_definition_version": METRIC_VERSION,
                   "profile": PROFILE, "runs": normalized})
    return output
