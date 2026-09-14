from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from .metrics import STANDARD_METRIC_FIELDS

GROUP_FIELDS = (
    "module_name",
    "scenario_name",
    "payload_size_bytes",
    "publish_rate_hz",
    "publisher_count",
    "subscriber_count",
    "transport_mode",
    "qos_profile",
    "network_profile",
)


def _numeric_summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "std_population": statistics.pstdev(values),
    }


def summarize_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        key = tuple(run.get(field) for field in GROUP_FIELDS)
        grouped[key].append(run)

    summaries: list[dict[str, Any]] = []
    for key, group in grouped.items():
        passed = [run for run in group if run.get("status") == "passed"]
        metrics: dict[str, Any] = {}
        for field in STANDARD_METRIC_FIELDS:
            values = [
                float(run[field])
                for run in passed
                if isinstance(run.get(field), (int, float))
                and not isinstance(run.get(field), bool)
            ]
            metrics[field] = _numeric_summary(values)
        summaries.append(
            {
                "group_key": dict(zip(GROUP_FIELDS, key, strict=True)),
                "condition_ids": sorted({run["condition_id"] for run in group}),
                "attempted_repeats": len(group),
                "valid_repeats": len(passed),
                "formal_minimum_met": len(passed) >= 5,
                "metrics": metrics,
            }
        )
    summaries.sort(
        key=lambda item: (
            str(item["group_key"].get("scenario_name")),
            int(item["group_key"].get("payload_size_bytes") or 0),
            float(item["group_key"].get("publish_rate_hz") or 0),
        )
    )
    return summaries
