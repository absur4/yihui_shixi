"""Validate report structure, numeric units and metric invariants without extra dependencies."""

import argparse
import json
import math
from pathlib import Path

from run_all import CASES


def require(condition, message):
    if not condition:
        raise ValueError(message)


def walk(value, path="measurements"):
    if isinstance(value, dict):
        if "value" in value and "unit" in value:
            require(isinstance(value["unit"], str), f"{path}: unit is not a string")
            require(value["value"] is None or isinstance(value["value"], (int, float)),
                    f"{path}: measurement value is not numeric/null")
            if value["unit"] == "ratio" and value["value"] is not None:
                require(0 <= value["value"] <= 1, f"{path}: ratio outside [0,1]")
        if "p99" in value and "count" in value:
            required = {"count", "unit", "min", "mean", "p50", "p95", "p99", "max", "stddev"}
            require(required <= value.keys(), f"{path}: incomplete distribution")
            require(isinstance(value["count"], int) and value["count"] >= 0, f"{path}: invalid count")
            statistics = [value[key] for key in ("min", "mean", "p50", "p95", "p99", "max", "stddev")]
            if value["count"] == 0:
                require(all(number is None for number in statistics), f"{path}: empty distribution must use null")
            else:
                require(all(isinstance(number, (int, float)) for number in statistics), f"{path}: nonnumeric statistic")
                require(value["min"] <= value["p50"] <= value["p95"] <= value["p99"] <= value["max"],
                        f"{path}: unordered percentiles")
                require(value["min"] - 1e-9 <= value["mean"] <= value["max"] + 1e-9, f"{path}: mean outside bounds")
                require(value["stddev"] >= 0, f"{path}: negative standard deviation")
        for key, child in value.items():
            walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walk(child, f"{path}[{index}]")
    elif isinstance(value, float):
        require(math.isfinite(value), f"{path}: non-finite number")


def validate(result):
    required = {"schema_version", "middleware", "metric", "layer", "status", "timestamp_utc",
                "environment", "configuration", "topology", "measurements", "limitations", "error"}
    require(required <= result.keys(), "Missing required report fields")
    require(result["schema_version"] == "1.0" and result["middleware"] == "vsoa", "Wrong schema or middleware")
    require(result["metric"] in CASES, "Unknown metric")
    require(result["layer"] in {"feature", "performance"}, "Unknown layer")
    require(result["status"] in {"pass", "partial", "fail", "error", "skip"}, "Unknown status")
    require(isinstance(result["measurements"], dict), "Measurements must be an object")
    require(isinstance(result["limitations"], list), "Limitations must be an array")
    walk(result)
    measurements = result["measurements"]
    if {"attempted", "successful", "failed"} <= measurements.keys():
        require(measurements["attempted"] == measurements["successful"] + measurements["failed"], "Sample count mismatch")
        require(measurements["rtt"]["count"] == measurements["successful"], "RTT count mismatch")
    if "samples" in measurements and result["metric"] in {"latency", "jitter", "realtime", "reliability"}:
        require(len(measurements["samples"]) == measurements["attempted"], "Raw sample count mismatch")
    if result["metric"] == "throughput" and measurements:
        require(measurements["attempted"] == measurements["completed_valid"] + measurements["failed"], "RPC accounting mismatch")
        duration = measurements["measurement_including_drain"]["value"]
        expected = measurements["completed_valid"] * result["configuration"]["payload_bytes"] / duration
        require(math.isclose(expected, measurements["one_direction_payload_goodput"]["value"]), "Goodput formula mismatch")
    if result["metric"] == "packet_loss_recovery" and measurements:
        proxy = measurements["injection"]["all_attempts_proxy"]
        require(proxy["seen"] == proxy["dropped"] + proxy["forwarded"], "Proxy accounting mismatch")
        recovery = measurements["application_retry"]
        require(recovery["final_received_unique"] + len(recovery["remaining_missing_ids"]) == measurements["sent_unique"],
                "Loss accounting mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    options = parser.parse_args()
    paths = sorted(options.path.glob("*.json")) if options.path.is_dir() else [options.path]
    checked = 0
    failures = []
    for path in paths:
        if path.name == "summary.json":
            continue
        try:
            validate(json.loads(path.read_text(encoding="utf-8")))
            checked += 1
        except (ValueError, KeyError, TypeError, OSError) as error:
            failures.append({"file": str(path), "error": str(error)})
    print(json.dumps({"validated": checked, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures or not checked else 0


if __name__ == "__main__":
    raise SystemExit(main())
