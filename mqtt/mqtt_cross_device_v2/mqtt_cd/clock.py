"""Four-timestamp clock diagnostics and conservative, bracketed clock mapping."""
from __future__ import annotations

from bisect import bisect_right
import math


def _exchange(sample):
    t1, t2, t3, t4 = (int(sample[key]) for key in ("t1", "t2", "t3", "t4"))
    if t4 <= t1 or t3 < t2:
        raise ValueError("nonmonotonic clock exchange")
    rtt = (t4 - t1) - (t3 - t2)
    if rtt < 0:
        raise ValueError("negative network RTT")
    return {"offset_ns": ((t2 - t1) + (t3 - t4)) / 2,
            "rtt_ns": rtt, "uncertainty_ns": rtt / 2,
            "reference_ns": (t1 + t4) // 2, "node_ns": (t2 + t3) // 2}


def estimate(samples):
    """Best-RTT offset estimate for scheduling, not a clock-accuracy certificate."""
    valid = []
    for sample in samples:
        try:
            valid.append(_exchange(sample))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not valid:
        raise ValueError("no valid four-timestamp clock exchanges")
    result = dict(min(valid, key=lambda item: item["rtt_ns"]))
    result["sample_count"] = len(valid)
    return result


class ClockModel:
    """Map a node's monotonic clock into the controller's reference clock.

    Every interpolation is bracketed by measured centers. A declared validated
    drift bound is required for comparison-quality values; raw offset estimates
    remain useful for diagnostics when that validation is absent.
    """

    def __init__(self, samples, config=None):
        self.config = config or {}
        self.model_validated = self.config.get("model_validated") is True
        self.reasons = []
        self.centers = []
        self.segments = []
        interval = float(self.config.get("interval_s", 5))
        self.max_gap_ns = float(self.config.get("max_gap_s", 2 * interval + 1)) * 1e9
        drift = float(self.config.get("drift_bound_ppm", 100))
        timestamp_error = float(self.config.get("timestamp_error_ns", 0))
        residual_bound = float(self.config.get("validation_residual_ns", 0))
        if not all(math.isfinite(x) and x >= 0 for x in
                   (interval, drift, timestamp_error, residual_bound, self.max_gap_ns)):
            raise ValueError("clock error model parameters must be finite and nonnegative")
        if not self.model_validated:
            self.reasons.append("model_unvalidated")
        exchanges = []
        for sample in samples:
            try:
                exchanges.append(_exchange(sample))
            except (KeyError, TypeError, ValueError, OverflowError):
                self.reasons.append("invalid_clock_exchange")
        exchanges.sort(key=lambda item: item["reference_ns"])
        groups = []
        burst_gap = min(1e9, interval * 1e9 / 2)
        for exchange in exchanges:
            if not groups or exchange["reference_ns"] - groups[-1][-1]["reference_ns"] > burst_gap:
                groups.append([])
            groups[-1].append(exchange)
        for group in groups:
            center = dict(min(group, key=lambda item: item["rtt_ns"]))
            center["sample_count"] = len(group)
            center["observed_residual_ns"] = max(
                max(0, abs(item["offset_ns"] - center["offset_ns"])
                    - item["uncertainty_ns"] - center["uncertainty_ns"])
                for item in group)
            self.centers.append(center)
        self.node_centers = [item["node_ns"] for item in self.centers]
        if len(self.centers) < 2:
            self.reasons.append("insufficient_calibration_centers")
        for left, right in zip(self.centers, self.centers[1:]):
            gap = right["reference_ns"] - left["reference_ns"]
            node_gap = right["node_ns"] - left["node_ns"]
            reason = None
            if gap <= 0 or node_gap <= 0:
                reason = "nonmonotonic_clock_mapping"
            elif min(left["sample_count"], right["sample_count"]) < 5:
                reason = "insufficient_exchanges_in_calibration_burst"
            elif gap > self.max_gap_ns:
                reason = "calibration_gap"
            # Half-interval distance is the furthest point from an observation.
            model_error = drift * 1e-6 * gap / 2 + residual_bound
            uncertainty = (max(left["uncertainty_ns"], right["uncertainty_ns"])
                           + timestamp_error + model_error
                           + max(left["observed_residual_ns"], right["observed_residual_ns"]))
            self.segments.append({"node_start_ns": left["node_ns"],
                                  "node_end_ns": right["node_ns"],
                                  "reference_start_ns": left["reference_ns"],
                                  "reference_end_ns": right["reference_ns"],
                                  "uncertainty_ns": uncertainty,
                                  "model_error_ns": model_error, "reason": reason})

    def map(self, node_ns):
        if len(self.node_centers) < 2:
            return None, None, "insufficient_calibration_centers"
        if node_ns < self.node_centers[0] or node_ns > self.node_centers[-1]:
            return None, None, "outside_calibration_coverage"
        index = min(bisect_right(self.node_centers, node_ns) - 1, len(self.segments) - 1)
        segment = self.segments[index]
        if segment["reason"]:
            return None, None, segment["reason"]
        fraction = ((node_ns - segment["node_start_ns"])
                    / (segment["node_end_ns"] - segment["node_start_ns"]))
        reference = (segment["reference_start_ns"] + fraction
                     * (segment["reference_end_ns"] - segment["reference_start_ns"]))
        return reference, segment["uncertainty_ns"], None

    def coverage(self, start_ns, end_ns):
        values = [self.map(start_ns), self.map(end_ns)]
        for segment in self.segments:
            if segment["node_start_ns"] < end_ns and segment["node_end_ns"] > start_ns:
                values.append((0, segment["uncertainty_ns"], segment["reason"]))
        reasons = sorted({item[2] for item in values if item[2]})
        if not self.model_validated:
            reasons.append("model_unvalidated")
        return {"valid": not reasons, "reasons": reasons,
                "uncertainty_ns": None if any(item[1] is None for item in values)
                else max(item[1] for item in values)}

    def as_dict(self):
        return {"model_validated": self.model_validated, "centers": self.centers,
                "segments": self.segments, "reasons": sorted(set(self.reasons)),
                "clock_drift_ms": ((self.centers[-1]["offset_ns"] - self.centers[0]["offset_ns"]) / 1e6
                                   if len(self.centers) >= 2 else None),
                "assumptions": {key: self.config.get(key) for key in
                                ("drift_bound_ppm", "timestamp_error_ns", "validation_residual_ns",
                                 "model_validation_reference")}}
