"""Explicit units and population statistics shared by runs and comparisons."""

import math
import statistics

METRICS = ("latency_ms", "latency_p95_ms", "latency_p99_ms", "latency_std_ms", "throughput_mbps",
           "cpu_percent", "memory_mb", "packet_loss", "startup_time_ms", "discovery_time_ms", "jitter_ms")
UNITS = {"latency_ms": "ms", "latency_p95_ms": "ms", "latency_p99_ms": "ms", "latency_std_ms": "ms",
         "throughput_mbps": "Mbit/s (decimal, validated subscriber payload)",
         "cpu_percent": "% (100 = one logical core, summed workload processes)",
         "memory_mb": "MB (decimal, sampled peak summed workload RSS)", "packet_loss": "ratio [0,1] before app recovery",
         "startup_time_ms": "ms", "discovery_time_ms": "ms", "jitter_ms": "ms"}


def stats(values):
    values = sorted(values)
    if not values:
        return {key: 0 if key == "count" else None for key in
                ("count", "min", "max", "mean", "median", "variance", "std", "p95", "p99")}

    def quantile(fraction):
        position = (len(values) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {"count": len(values), "min": values[0], "max": values[-1], "mean": statistics.fmean(values),
            "median": statistics.median(values),
            "variance": statistics.pvariance(values), "std": statistics.pstdev(values),
            "p95": quantile(0.95), "p99": quantile(0.99)}


def aggregate(runs):
    output = {"repeat_count": len(runs), "statistics": {}}
    for name in METRICS:
        distribution = stats([run[name] for run in runs if run.get(name) is not None and run["status"] == "completed"])
        output[name] = distribution["mean"]
        output["statistics"][name] = distribution
    output["messages_sent"] = sum(run.get("messages_sent", 0) for run in runs)
    output["messages_received"] = sum(run.get("messages_received", 0) for run in runs)
    output["failed_run_count"] = sum(run["status"] != "completed" for run in runs)
    return output
