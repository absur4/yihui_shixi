"""Acceptance checker for the standalone schema and per-run accounting."""

import argparse
import json
import math
from pathlib import Path

from standalone.configuration import SCENARIOS
from standalone.statistics import METRICS


def require(value, message):
    if not value:
        raise ValueError(message)


def finite_walk(value):
    if isinstance(value, dict):
        for child in value.values():
            finite_walk(child)
    elif isinstance(value, list):
        for child in value:
            finite_walk(child)
    elif isinstance(value, float):
        require(math.isfinite(value), "Non-finite JSON number")


def validate_result(result, all_scenarios=False):
    require(result["schema_version"] == "2.1", "Unexpected schema")
    required = {*METRICS, "test_start_time", "test_end_time", "messages_sent", "messages_received", "scenario_name", "run_id"}
    require(required <= result.keys(), "Required visualization fields missing")
    finite_walk(result)
    require(result["status"] == "completed", "Suite is not completed")
    runs = result["runs"]
    require(bool(runs), "No runs")
    if all_scenarios:
        require({run["scenario_name"] for run in runs} == set(SCENARIOS), "Five scenarios not covered")
    for run in runs:
        require(run["status"] == "completed", f"Failed run: {run['run_id']}")
        require(required <= run.keys(), "Run fields missing")
        require(isinstance(run.get("out_of_order_count"), int) and run["out_of_order_count"] >= 0,
                "Independent out_of_order_count missing")
        for resource_key in ("middleware_resources", "test_infrastructure_resources", "total_process_resources"):
            resource = run.get(resource_key, {})
            require(resource.get("cpu_percent", -1) >= 0 and resource.get("memory_mb", -1) >= 0,
                    f"Resource scope missing: {resource_key}")
        config = run["configuration"]
        expected = run["messages_sent"] * config["subscriber_count"]
        require(run["expected_deliveries"] == expected, "Topology delivery count mismatch")
        require(0 <= run["messages_received"] <= run["messages_received_after_recovery"] <= expected, "Invalid delivery accounting")
        require(math.isclose(run["packet_loss"], (expected - run["messages_received"]) / expected), "Initial loss formula mismatch")
        require(math.isclose(run["final_packet_loss"], (expected - run["messages_received_after_recovery"]) / expected), "Final loss formula mismatch")
        throughput = run["messages_received_in_send_window"] * config["message_size_bytes"] * 8 / run["delivery_window_seconds"] / 1e6
        require(math.isclose(run["throughput_mbps"], throughput), "Throughput formula mismatch")
        latency = run["statistics"]["latency_ms"]
        require(latency["count"] == run["messages_received"], "Latency sample count mismatch")
        if latency["count"]:
            require(latency["min"] <= latency["p95"] <= latency["p99"] <= latency["max"], "Percentiles unordered")
            require(math.isclose(latency["std"] ** 2, latency["variance"], abs_tol=1e-12), "Population variance mismatch")
        for relay in (run["proxy_report"] or {}).get("relays", []):
            require(relay["seen"] == relay["dropped"] + relay["forwarded"] + relay["delayed_pending"], "Proxy counters do not balance")
        impairment = run.get("network_impairment")
        if impairment:
            require(impairment["injected_count"] == impairment["forwarded_count"] + impairment["dropped_count"],
                    "Normalized impairment counters do not balance")
    for scenario in result["scenario_summaries"]:
        planned = next((case["planned_repeats"] for case in result.get("case_plan", []) if case["scenario_name"] == scenario["scenario_name"]), result["configuration"]["repeats"])
        require(scenario["repeat_count"] == planned, "Repeat count mismatch")
        for metric in METRICS:
            distribution = scenario["statistics"][metric]
            require({"mean", "median", "variance", "std", "p95", "p99"} <= distribution.keys(), "Repetition statistics missing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--all-scenarios", action="store_true")
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    validate_result(result, args.all_scenarios)
    print(json.dumps({"valid": True, "runs": len(result["runs"]), "scenarios": len(result["scenario_summaries"])}))


if __name__ == "__main__":
    main()
