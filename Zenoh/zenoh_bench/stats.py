from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    h = (len(ordered) - 1) * p
    lo, hi = math.floor(h), math.ceil(h)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (h - lo)


def latency_metrics(samples: Iterable[dict]) -> dict:
    valid = [s for s in samples if s.get("valid") and s.get("latency_ms") is not None]
    values = [float(s["latency_ms"]) for s in valid]
    if not values:
        return {k: None for k in ("latency_ms", "latency_p95_ms", "latency_p99_ms", "latency_std_ms", "jitter_ms")}
    mean = sum(values) / len(values)
    std = math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))
    streams: dict[tuple, list[dict]] = defaultdict(list)
    for sample in valid:
        streams[
            (
                sample.get("repeat"),
                sample["publisher_id"],
                sample["subscriber_id"],
            )
        ].append(sample)
    jitters: list[float] = []
    for stream in streams.values():
        ordered = sorted(stream, key=lambda x: x["sequence"])
        jitters.extend(abs(b["latency_ms"] - a["latency_ms"]) for a, b in zip(ordered, ordered[1:]))
    return {
        "latency_ms": mean,
        "latency_p95_ms": percentile(values, .95),
        "latency_p99_ms": percentile(values, .99),
        "latency_std_ms": std,
        "jitter_ms": sum(jitters) / len(jitters) if jitters else None,
    }


def round_numbers(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: round_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [round_numbers(v) for v in value]
    return value
