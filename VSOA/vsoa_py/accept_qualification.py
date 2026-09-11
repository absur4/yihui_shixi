"""Audit executable-produced extended results, including full stability samples."""

import argparse
import hashlib
import json
import math
import time
from array import array
from pathlib import Path

from standalone.io_utils import read_json, write_json
from standalone.statistics import stats
from standalone.validate_delivery import require, validate_result


def audit(package):
    output = package / "outputs" / "qualification"
    result = read_json(output / "result.json")
    validate_result(result)
    runs = result["runs"]
    require(len(runs) == result["planned_runs"] == 155, "Default qualification requires 155 runs")
    require(len({(run["scenario_name"], run["repeat"]) for run in runs}) == 155, "Duplicate run identity")
    unsupported = [case for case in result["case_plan"] if case["status"] == "unsupported"]
    require(not unsupported, "Standard logical-message cases must all execute")
    require(len(result["scenario_summaries"]) == 31, "Executable case coverage incomplete")
    require({run["scenario_id"] for run in runs} == {f"S{index:02d}" for index in range(1, 13)}, "S01-S12 coverage incomplete")
    stable, faults, weak, load_shortfalls = [], [], [], []
    for run in runs:
        config = run["configuration"]
        folder = output / run["artifacts_directory"]
        resource = read_json(folder / "resource_samples.json") if (folder / "resource_samples.json").exists() else None
        if resource:
            timestamps = [sample["timestamp_ns"] for sample in resource["samples"]]
            maximum_sampling_gap_ms = max((later - earlier for earlier, later in zip(timestamps, timestamps[1:])), default=0) / 1e6
            require(maximum_sampling_gap_ms < 1000, f"Traffic sampling interruption: {run['run_id']} ({maximum_sampling_gap_ms} ms)")
        require(isinstance(run["out_of_order_count"], int) and run["out_of_order_count"] >= 0, "Missing out-of-order count")
        for key in ("middleware_resources", "test_infrastructure_resources", "total_process_resources"):
            require(run[key]["cpu_percent"] >= 0 and run[key]["memory_mb"] >= 0, f"Missing resource scope: {key}")
        if run["scenario_id"] in {"S01", "S02"}:
            require(run["messages_sent"] >= 10000, "Small-message scenario requires at least 10000 messages")
        if run["scenario_id"] == "S04":
            require(run["configuration"].get("fragmentation_mode") == "application", "Large-message method missing")
            require(all(item["fragments_per_message"] > 1 for item in run["publisher_metrics"]), "Large message was not fragmented")
        if run["scenario_id"] == "S05":
            require(len(run["subscriber_metrics"]) == 4 and len(run["link_metrics"]) == 4, "S05 details incomplete")
        if run["scenario_id"] == "S06":
            require(len(run["publisher_metrics"]) == 4 and len(run["link_metrics"]) == 4, "S06 details incomplete")
        if run["scenario_id"] == "S07":
            require(len(run["publisher_metrics"]) == 4 and len(run["subscriber_metrics"]) == 4 and len(run["link_metrics"]) == 16, "S07 details incomplete")
        if run.get("load_target_met") is False:
            load_shortfalls.append({"run_id": run["run_id"], "target_hz": config["publish_rate_hz"],
                                    "achieved_hz": run["achieved_publish_rate_hz_per_publisher"]})
        if config["transport_mode"] == "tcp" or config["recovery_enabled"]:
            require(run["final_packet_loss"] == 0, f"Final loss: {run['run_id']}")
        if config.get("test_kind") == "stability":
            require(resource["observed_duration_seconds"] >= 299.9, "Actual resource window shorter than 300 seconds tolerance")
            timestamps = [sample["timestamp_ns"] for sample in resource["samples"]]
            max_gap_ms = max(later - earlier for earlier, later in zip(timestamps, timestamps[1:])) / 1e6
            require(max_gap_ms < 1000, f"Stability sampling interruption: {run['run_id']} ({max_gap_ms} ms)")
            for publisher in run["publisher_reports"]:
                require(publisher["end_ns"] - publisher["start_ns"] == 300_000_000_000, "Wrong scheduled duration")
                require(publisher["finished_ns"] >= publisher["end_ns"] - 2_000_000, "Publisher stopped early")
            samples = {"latencies_ms": [], "jitter_samples_ms": []}
            for subscriber in run["subscriber_reports"]:
                for field in samples:
                    values = array("d")
                    values.frombytes((folder / subscriber[field + "_file"]).read_bytes())
                    samples[field].extend(values)
            for field, metric in (("latencies_ms", "latency_ms"), ("jitter_samples_ms", "jitter_ms")):
                exact = stats(samples[field])
                for key, value in exact.items():
                    require(math.isclose(value, run["statistics"][metric][key], rel_tol=1e-10, abs_tol=1e-10), "Full sample statistic mismatch")
            memory = [row["groups"]["middleware"]["memory_mb"] for row in resource["samples"]]
            stable.append({"run_id": run["run_id"], "observed_seconds": resource["observed_duration_seconds"],
                           "messages_sent": run["messages_sent"], "messages_received": run["messages_received"],
                           "full_sample_count": len(samples["latencies_ms"]), "exact_quantiles_verified": True,
                           "max_resource_sample_gap_ms": max_gap_ms,
                           "first_100_rss_mean_mb": stats(memory[:100])["mean"],
                           "last_100_rss_mean_mb": stats(memory[-100:])["mean"],
                           "rss_change_mb": stats(memory[-100:])["mean"] - stats(memory[:100])["mean"]})
        if config.get("test_kind") == "fault_recovery":
            require(len(run["fault_events"]) == 3, "Three fault cycles required")
            for event in run["fault_events"]:
                require(event["passed"] and event["fault_at_ns"] <= event["detected_at_ns"] <= event["delivery_confirmed_ns"], "Invalid fault timeline")
                require(event["restoration_to_delivery_ms"] <= config["fault_recovery_timeout_seconds"] * 1000, "Recovery deadline exceeded")
            faults.append({"run_id": run["run_id"], "cycles": len(run["fault_events"]),
                           "committed": run["messages_sent"], "reconciled": run["messages_received_after_recovery"]})
        if run.get("proxy_report"):
            relays = run["proxy_report"]["relays"]
            impairment = run["network_impairment"]
            require(impairment["injected_count"] == impairment["forwarded_count"] + impairment["dropped_count"], "Weak-network counters do not balance")
            if config.get("blackout_duration_seconds"):
                require(sum(relay["blackout_dropped"] for relay in relays) > 0, "No real blackout drops")
            weak.append({"run_id": run["run_id"], "configured_loss": config["loss_rate"],
                         "actual_initial_loss": run["packet_loss"], "final_loss": run["final_packet_loss"],
                         "blackout_dropped": sum(relay["blackout_dropped"] for relay in relays)})
    require(len(stable) == 5 and len(faults) == 20 and len(weak) == 65, "Phase coverage mismatch")
    require(len({(run["configuration"]["network_delay_ms"], run["configuration"]["network_jitter_ms"])
                 for run in runs if run["scenario_id"] == "S10"}) == 3, "Multiple network profiles missing")
    report = {"passed": True, "module_version": "1.2", "completed_runs": len(runs), "executable_cases": 31,
              "validation_exclusions": read_json(output / "validation_exclusions" / "rejected_runs.json") if (output / "validation_exclusions" / "rejected_runs.json").exists() else [],
              "unsupported_cases": unsupported, "repeats_per_case": 5,
              "standard_scenario_ids": [f"S{index:02d}" for index in range(1, 13)],
              "exe_sha256": hashlib.sha256((package / "vsoa.exe").read_bytes()).hexdigest(),
              "test_start_time": result["test_start_time"], "test_end_time": result["test_end_time"],
              "stability": stable, "fault_recovery": faults, "weak_network": weak,
              "load_target_shortfalls": load_shortfalls,
              "all_targets_met": not load_shortfalls,
              "scope": "Same-host Windows loopback; harness-assisted recovery, no failover/power-loss claims"}
    prior_failures = {}
    for history_path in (output / "history").glob("*.json"):
        history = read_json(history_path)
        for failed in history.get("runs", []):
            if failed.get("status") != "completed":
                prior_failures[failed["run_id"]] = {"run_id": failed["run_id"],
                                                     "scenario_id": failed.get("scenario_id"),
                                                     "scenario_name": failed["scenario_name"],
                                                     "repeat": failed["repeat"], "errors": failed.get("errors", [])}
    report["prior_failed_attempts_retained"] = list(prior_failures.values())
    write_json(package / "qualification_acceptance.json", report)
    print(json.dumps({key: report[key] for key in ("passed", "completed_runs", "executable_cases", "all_targets_met")}, indent=2))


def prepare_retry(package):
    output = package / "outputs" / "qualification"
    result = read_json(output / "result.json")
    require(result["status"] != "running", "Never change an active suite")
    rejected = []
    for run in result["runs"]:
        if run["status"] == "completed" and run["configuration"].get("test_kind") != "fault_recovery":
            resources = read_json(output / run["artifacts_directory"] / "resource_samples.json")
            timestamps = [sample["timestamp_ns"] for sample in resources["samples"]]
            gap_ms = max((later - earlier for earlier, later in zip(timestamps, timestamps[1:])), default=0) / 1e6
            reasons = []
            if gap_ms >= 1000:
                reasons.append("Resource sampling gap >= 1000 ms; uninterrupted traffic not demonstrated")
            if run.get("scenario_id") in {"S01", "S02"} and run["messages_sent"] < 10000:
                reasons.append("Small-message count below 10000")
            if reasons:
                rejected.append({"run": run, "validation_reason": "; ".join(reasons), "max_gap_ms": gap_ms})
    if not rejected:
        print("No stability sampling interruptions require retry")
        return
    directory = output / "validation_exclusions"
    directory.mkdir(exist_ok=True)
    write_json(directory / f"original_result_{time.time_ns()}.json", result)
    ledger = read_json(directory / "rejected_runs.json") if (directory / "rejected_runs.json").exists() else []
    write_json(directory / "rejected_runs.json", ledger + rejected)
    rejected_ids = {item["run"]["run_id"] for item in rejected}
    result["runs"] = [run for run in result["runs"] if run["run_id"] not in rejected_ids]
    result["status"] = "error"
    result["validation_retry_notice"] = "Validation rejected interrupted stability attempts; original results retained in validation_exclusions. Run identical EXE/config with --resume."
    write_json(output / "result.json", result)
    print(json.dumps({"excluded_for_retry": len(rejected), "original_evidence_preserved": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path("release/vsoa_win64_v1.2"))
    parser.add_argument("--prepare-retry", action="store_true")
    options = parser.parse_args()
    (prepare_retry if options.prepare_retry else audit)(options.package.resolve())
