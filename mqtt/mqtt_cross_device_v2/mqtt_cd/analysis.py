"""Recompute version 2.0 metrics from immutable node CSV/JSONL artifacts.

Identity joins and de-duplication use a temporary on-disk SQLite database so a
large CD4 run does not require retaining every message as a Python object.
"""
from __future__ import annotations

import csv
from bisect import bisect_left
import json
import math
from pathlib import Path
import sqlite3
import tempfile

from .clock import ClockModel


def ratio(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator else None


def quantile(values, fraction):
    """Linear interpolation with h=(n-1)*fraction, including small diagnostics."""
    if not values:
        return None
    ordered = sorted(values)
    h = (len(ordered) - 1) * fraction
    lo, hi = math.floor(h), math.ceil(h)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (h - lo)


def consecutive_jitter(samples):
    """Samples are (sequence, milliseconds) from one publisher/subscriber link."""
    ordered = sorted(samples)
    differences = [abs(right[1] - left[1]) for left, right in zip(ordered, ordered[1:])
                   if right[0] == left[0] + 1]
    return {"jitter_ms": sum(differences) / len(differences) if differences else None,
            "jitter_sample_count": len(differences)}


def delivery_metrics(planned, sent, subscribers, window, final, duration_s, payload_bytes):
    expected = sent * subscribers if sent is not None else None
    expected_plan = planned * subscribers
    mbps = lambda count: count * payload_bytes * 8 / duration_s / 1e6 if count is not None else None
    delivery = ratio(window, expected)
    final_delivery = ratio(final, expected)
    return {"actual_publish_rate_hz": ratio(sent, duration_s),
            "target_offered_throughput_mbps": mbps(planned),
            "offered_throughput_mbps": mbps(sent),
            "target_delivery_throughput_mbps": mbps(expected_plan),
            "offered_delivery_throughput_mbps": mbps(expected),
            "throughput_mbps": mbps(window),
            "actual_receive_rate_hz": ratio(window, duration_s),
            "send_achievement_ratio": ratio(sent, planned),
            "delivery_achievement_ratio": delivery,
            "end_to_end_achievement_ratio": ratio(window, expected_plan),
            "window_missing_ratio": None if delivery is None else 1 - delivery,
            "final_missing_ratio": None if final_delivery is None else 1 - final_delivery,
            "drain_delivery_ratio": ratio(final - window, expected)
            if final is not None and window is not None else None,
            "final_target_achievement_ratio": ratio(final, expected_plan)}


def capacity_verdict(lower, upper, missing, window_valid, counts_valid, runtime_failure=False):
    if runtime_failure:
        return "fail"
    if not window_valid or not counts_valid or lower is None or upper is None:
        return "indeterminate"
    if upper < .99 or (missing is not None and missing > .001 + 1e-12):
        return "fail"
    if lower >= .99 and missing is not None and missing <= .001 + 1e-12:
        return "pass"
    return "indeterminate"


def _load_json(path, issues, required=True):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        if required:
            issues.append({"code": "missing_or_invalid_artifact", "path": str(path),
                           "detail": str(error)})
        return None


def _float(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _slope(times_minutes, values):
    if len(values) < 2 or any(value is None for value in values):
        return None
    xbar, ybar = sum(times_minutes) / len(values), sum(values) / len(values)
    denominator = sum((x - xbar) ** 2 for x in times_minutes)
    return (sum((x - xbar) * (y - ybar) for x, y in zip(times_minutes, values)) / denominator
            if denominator else None)


def _clock_quality(models, starts, duration, drain, config):
    reasons = []
    sender, receiver = models
    a, b = starts
    covers = []
    if a is None or b is None:
        reasons.append("missing_actual_local_window")
    else:
        covers = [model.coverage(start, start + int((duration + drain) * 1e9))
                  for model, start in zip(models, starts)]
        for cover in covers:
            reasons.extend(cover["reasons"])
    uncertainty = None
    if covers and all(cover["uncertainty_ns"] is not None for cover in covers):
        startup_errors = []
        for offset in (0, int(duration * 1e9)):
            source_ref = sender.map(a + offset)[0]
            receiver_ref = receiver.map(b + offset)[0]
            if source_ref is not None and receiver_ref is not None:
                startup_errors.append(abs(receiver_ref - source_ref))
        if len(startup_errors) == 2:
            uncertainty = sum(cover["uncertainty_ns"] for cover in covers) + max(startup_errors)
    window_limit_ms = min(float(config.get("window_limit_ms", 20)), duration)
    if uncertainty is None:
        reasons.append("window_uncertainty_unknown")
    elif uncertainty / 1e6 > window_limit_ms:
        reasons.append("window_uncertainty_exceeds_limit")
    return {"model_validated": all(model.model_validated for model in models),
            "window_valid": not reasons, "latency_valid": False,
            "u_window_ms": uncertainty / 1e6 if uncertainty is not None else None,
            "window_limit_ms": window_limit_ms,
            "latency_limit_ms": float(config.get("latency_limit_ms", 1)),
            "reasons": sorted(set(reasons))}


def _import_source(db, path, spec, start, issues):
    duration_ns = int(spec["duration_s"] * 1e9)
    end = start + duration_ns if start is not None else None
    cutoff = end + int(spec["drain_s"] * 1e9) if end is not None else None
    planned = math.ceil(spec["rate_hz"] * spec["duration_s"] - 1e-9)
    complete = True
    skipped = {"warmup_rows": 0, "invalid_rows": 0, "duplicate_source_rows": 0,
               "local_queue_rejected_before_api": 0, "raw_formal_accepted": 0,
               "raw_formal_api_rejected": 0, "raw_rows": 0}
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"sequence", "send_ns", "return_ns", "accepted", "phase", "payload_bytes"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("source CSV header missing required fields")
            for row in reader:
                skipped["raw_rows"] += 1
                try:
                    if int(row["phase"]) != 1:
                        skipped["warmup_rows"] += 1
                        continue
                    if row.get("api_called", "1") == "0":
                        if row["accepted"].lower() in ("1", "true"):
                            raise ValueError("non-API event cannot be an accepted submission")
                        skipped["local_queue_rejected_before_api"] += 1
                        continue
                    sequence, send_ns = int(row["sequence"]), int(row["send_ns"])
                    returned = int(row["return_ns"]) if row["return_ns"] else None
                    if (not 0 <= sequence < planned or int(row["payload_bytes"]) != spec["payload_bytes"]
                            or start is None or not start <= send_ns < end
                            or (returned is not None and returned < send_ns)):
                        raise ValueError("source identity or measurement window mismatch")
                    accepted = {"1": 1, "true": 1, "0": 0, "false": 0}.get(row["accepted"].lower())
                    skipped["raw_formal_accepted"] += accepted == 1
                    skipped["raw_formal_api_rejected"] += accepted == 0
                    if accepted == 1 and (returned is None or returned >= cutoff):
                        accepted = None
                    cursor = db.execute("INSERT OR IGNORE INTO sends VALUES (?,?,?,?,?)",
                                        (sequence, send_ns, returned, accepted, _float(row.get("api_ms"))))
                    if not cursor.rowcount:
                        skipped["duplicate_source_rows"] += 1
                        complete = False
                except (ValueError, TypeError, KeyError, OverflowError):
                    skipped["invalid_rows"] += 1
                    complete = False
        db.commit()
    except (OSError, ValueError, csv.Error) as error:
        issues.append({"code": "source_log_unavailable", "path": str(path), "detail": str(error)})
        complete = False
    if start is None:
        complete = False
    attempted, sent, unknown, late = db.execute(
        "SELECT COUNT(*),SUM(accepted=1),SUM(accepted IS NULL),SUM(accepted=1 AND return_ns>=?) FROM sends",
        (end or 0,)).fetchone()
    return {"complete": complete, "N_planned": planned,
            "N_attempt": attempted if complete else None,
            "N_sent": (sent or 0) if complete else None,
            "submission_unknown_count": (unknown or 0) if complete else None,
            "late_accept_count": (late or 0) if complete else None,
            "observed_counts": {"N_attempt": attempted, "N_sent": sent or 0,
                                "submission_unknown_count": unknown or 0, "late_accept_count": late or 0},
            **skipped}


def _import_receiver(db, path, subscriber, spec, source_complete, issues):
    counts = dict(duplicate_count=0, out_of_order_count=0, corrupt_count=0, foreign_count=0,
                  unparsable_count=0, warmup_rows=0, source_inconsistency_count=0,
                  raw_valid_receipt_count=0, unknown_submission_receipt_count=0,
                  raw_rows=0, raw_invalid_rows=0)
    complete, maximum = True, -1
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"publisher_id", "sequence", "send_ns", "receive_ns", "phase", "validation",
                        "payload_bytes", "wire_bytes"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("receiver CSV header missing required fields")
            for row in reader:
                counts["raw_rows"] += 1
                validation = row.get("validation", "").lower()
                if validation not in ("ok", "valid", "true", "1"):
                    counts["raw_invalid_rows"] += 1
                    if any(word in validation for word in ("foreign", "run", "publisher")):
                        counts["foreign_count"] += 1
                    elif any(word in validation for word in ("crc", "length", "payload", "corrupt")):
                        counts["corrupt_count"] += 1
                    else:
                        counts["unparsable_count"] += 1
                    continue
                try:
                    if int(row["phase"]) != 1:
                        counts["warmup_rows"] += 1
                        continue
                    publisher = int(row["publisher_id"])
                    sequence, send_ns, receive_ns = (int(row[key]) for key in
                                                     ("sequence", "send_ns", "receive_ns"))
                    if (int(row["payload_bytes"]) != spec["payload_bytes"]
                            or int(row["wire_bytes"]) != spec["payload_bytes"] + 64):
                        counts["corrupt_count"] += 1
                        continue
                    if publisher != 0 or sequence < 0:
                        counts["foreign_count"] += 1
                        continue
                    counts["raw_valid_receipt_count"] += 1
                    source = db.execute("SELECT send_ns,accepted FROM sends WHERE sequence=?",
                                        (sequence,)).fetchone()
                    if source is None or source[0] != send_ns:
                        counts["foreign_count"] += 1
                        continue
                    if source[1] is None:
                        counts["unknown_submission_receipt_count"] += 1
                        continue
                    if source[1] != 1:
                        counts["source_inconsistency_count"] += 1
                        complete = False
                        continue
                    cursor = db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?)",
                                        (subscriber, sequence, receive_ns))
                    if not cursor.rowcount:
                        counts["duplicate_count"] += 1
                        # Defensive against unsorted CSV exports: first arrival means earliest timestamp.
                        db.execute("UPDATE receipts SET receive_ns=MIN(receive_ns,?) "
                                   "WHERE subscriber=? AND sequence=?", (receive_ns, subscriber, sequence))
                        continue
                    if sequence < maximum:
                        counts["out_of_order_count"] += 1
                    maximum = max(maximum, sequence)
                except (KeyError, TypeError, ValueError, OverflowError):
                    counts["unparsable_count"] += 1
                    complete = False
        db.commit()
    except (OSError, ValueError, csv.Error) as error:
        issues.append({"code": "receiver_log_unavailable", "path": str(path), "detail": str(error)})
        complete = False
    counts["complete"] = complete and source_complete
    return counts


def _seal_counter(observed, summary, counter, path, issues, required=True):
    """A closed CSV is incomplete if the independent worker counters disagree."""
    expected = summary.get(counter) if isinstance(summary, dict) else None
    if expected is None and not required:
        return True
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        issues.append({"code": "missing_seal_counter", "path": str(path), "counter": counter})
        return False
    if expected != observed:
        issues.append({"code": "sealed_log_count_mismatch", "path": str(path), "counter": counter,
                       "expected": expected, "observed": observed})
        return False
    return True


def _build_latencies(db, models, receiver_start, spec):
    source_model, receiver_model = models
    limit = float(spec.get("clock", {}).get("latency_limit_ms", 1)) * 1e6
    states = {index: {"main": set(), "final": set(), "negative_count": 0,
                      "max_uncertainty_ms": None, "cohort_main": {}}
              for index in range(spec["subscribers"])}
    if receiver_start is None:
        for state in states.values():
            state["main"].add("missing_actual_local_window")
            state["final"].add("missing_actual_local_window")
        return states
    end = receiver_start + int(spec["duration_s"] * 1e9)
    cutoff = end + int(spec["drain_s"] * 1e9)
    for subscriber, sequence, received, sent in db.execute(
            "SELECT r.subscriber,r.sequence,r.receive_ns,s.send_ns FROM receipts r "
            "JOIN sends s USING(sequence) WHERE r.receive_ns<?", (cutoff,)):
        main = int(receiver_start <= received < end)
        cohort = math.floor(sequence / spec["rate_hz"] / 10)
        state = states[subscriber]
        reasons = []
        if not source_model.model_validated or not receiver_model.model_validated:
            reasons.append("model_unvalidated")
        source_ref, source_u, source_reason = source_model.map(sent)
        recv_ref, recv_u, recv_reason = receiver_model.map(received)
        reasons.extend(reason for reason in (source_reason, recv_reason) if reason)
        latency = None
        if source_ref is not None and recv_ref is not None:
            latency = (recv_ref - source_ref) / 1e6
            uncertainty = source_u + recv_u
            state["max_uncertainty_ms"] = max(state["max_uncertainty_ms"] or 0, uncertainty / 1e6)
            if uncertainty > limit:
                reasons.append("latency_uncertainty_exceeds_limit")
            if latency < 0:
                state["negative_count"] += 1
                reasons.append("clock_consistency_failed" if latency * 1e6 < -uncertainty
                               else "negative_latency_within_uncertainty")
        if reasons:
            state["final"].update(reasons)
            if main:
                state["main"].update(reasons)
                state["cohort_main"].setdefault(cohort, set()).update(reasons)
        if latency is not None:
            db.execute("INSERT INTO latencies VALUES (?,?,?,?,?)", (subscriber, sequence, latency, main,
                        cohort))
    db.commit()
    db.execute("CREATE INDEX latencies_values ON latencies(subscriber,main,ms)")
    db.execute("CREATE INDEX latencies_sequence ON latencies(subscriber,sequence)")
    return states


def _latency_stats(db, where="1", params=(), reasons=(), expected=None):
    count, mean, mean_square, minimum, maximum = db.execute(
        "SELECT COUNT(*),AVG(ms),AVG(ms*ms),MIN(ms),MAX(ms) FROM latencies WHERE " + where, params).fetchone()
    errors = set(reasons)
    if expected is not None and count != expected:
        errors.add("incomplete_latency_coverage")
    if not count:
        errors.add("no_latency_samples")
    diagnostic = {}
    for name, percentile, minimum_count in (("p50", .5, 20), ("p95", .95, 200), ("p99", .99, 1000)):
        value = None
        if count:
            h = (count - 1) * percentile
            ordered = db.execute("SELECT ms FROM latencies WHERE " + where + " ORDER BY ms LIMIT ? OFFSET ?",
                                 (*params, 2, math.floor(h))).fetchall()
            value = ordered[0][0] + ((ordered[-1][0] - ordered[0][0]) * (h - math.floor(h)))
        diagnostic["latency_" + name + "_ms"] = value
        diagnostic[name + "_minimum_count"] = minimum_count
    result = {"sample_count": count, "latency_ms": mean if not errors else None,
              "latency_min_ms": minimum if not errors else None,
              "latency_max_ms": maximum if not errors else None,
              "latency_std_ms": math.sqrt(max(0, mean_square - mean * mean)) if not errors else None,
              "quality": "unmeasurable" if errors else "valid", "reasons": sorted(errors),
              "diagnostics": diagnostic}
    for name, minimum_count in (("p50", 20), ("p95", 200), ("p99", 1000)):
        result["latency_" + name + "_ms"] = diagnostic["latency_" + name + "_ms"] \
            if not errors and count >= minimum_count else None
        result[name + "_quality"] = ("unmeasurable" if errors else
                                     "valid" if count >= minimum_count else "insufficient_samples")
    # Each adjacent pair stays within one subscriber; sequence gaps do not form pairs.
    pair_count, jitter = db.execute(
        "WITH selected AS (SELECT * FROM latencies WHERE " + where + ") "
        "SELECT COUNT(*),AVG(ABS(a.ms-b.ms)) FROM selected a JOIN selected b "
        "ON a.subscriber=b.subscriber AND a.sequence=b.sequence+1", params).fetchone()
    result["jitter_sample_count"] = pair_count
    result["jitter_ms"] = jitter if not errors else None
    return result


def _resources(node_dirs, starts, models, spec, clock_quality, issues):
    result = {"nodes": {}, "peak_sum_mb": None, "synchronous_peak_mb": None,
              "synchronous_peak_quality": "unmeasurable"}
    aligned = {}
    period_ns = spec.get("sample_interval_s", .2) * 1e9
    duration_ns = spec["duration_s"] * 1e9
    for node, directory in node_dirs.items():
        start = starts[node]
        rows, malformed = [], False
        try:
            with (directory / "resources.jsonl").open(encoding="utf-8-sig") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or not isinstance(row.get("time_ns"), (int, float)):
                            raise ValueError("missing sample timestamp")
                        rows.append(row)
                    except (ValueError, TypeError):
                        malformed = True
        except OSError as error:
            malformed = True
            issues.append({"code": "resources_unavailable", "path": str(directory / "resources.jsonl"),
                           "detail": str(error)})
        rows.sort(key=lambda item: item["time_ns"])
        roles, covered, host_sum, host_weight, rss_weight, peak = {}, 0, 0, 0, 0, None
        buckets = [{"start_s": index * 10, "end_s": min((index + 1) * 10, spec["duration_s"]),
                    "memory_peak_mb": None, "cpu_sum": 0, "cpu_weight": 0}
                   for index in range(math.ceil(spec["duration_s"] / 10))]
        prior, aligned[node] = None, []
        for row in rows:
            timestamp = row["time_ns"]
            left = timestamp - period_ns if prior is None else prior
            if timestamp - left > 1.5 * period_ns:
                left = timestamp - period_ns
            prior = timestamp
            if start is None:
                continue
            overlap_left, overlap_right = max(left, start), min(timestamp, start + duration_ns)
            weight = max(0, overlap_right - overlap_left)
            if not weight:
                continue
            covered += weight
            host = _float(row.get("host_cpu_percent"))
            rss = _float(row.get("rss_total_mb"))
            if host is not None:
                host_sum += host * weight
                host_weight += weight
            if rss is not None:
                rss_weight += weight
                peak = max(peak or 0, rss)
                mapped, _, reason = models[node].map(timestamp)
                if mapped is not None and not reason:
                    aligned[node].append((mapped, rss))
            role_values = row.get("roles", {})
            if not isinstance(role_values, dict):
                role_values, malformed = {}, True
            for name, values in role_values.items():
                if not isinstance(values, dict):
                    malformed = True
                    continue
                stats = roles.setdefault(name, {"cpu_sum": 0, "cpu_weight": 0, "memory_sum": 0,
                                                "memory_weight": 0, "cpu_peak_percent": None,
                                                "memory_peak_mb": None})
                cpu, memory = _float(values.get("cpu_percent")), _float(values.get("memory_mb"))
                if cpu is not None:
                    stats["cpu_sum"] += cpu * weight
                    stats["cpu_weight"] += weight
                    stats["cpu_peak_percent"] = max(stats["cpu_peak_percent"] or 0, cpu)
                if memory is not None:
                    stats["memory_sum"] += memory * weight
                    stats["memory_weight"] += weight
                    stats["memory_peak_mb"] = max(stats["memory_peak_mb"] or 0, memory)
            for bucket in buckets:
                overlap = max(0, min(overlap_right, start + bucket["end_s"] * 1e9)
                              - max(overlap_left, start + bucket["start_s"] * 1e9))
                if overlap:
                    if host is not None:
                        bucket["cpu_sum"] += host * overlap
                        bucket["cpu_weight"] += overlap
                    if rss is not None:
                        bucket["memory_peak_mb"] = max(bucket["memory_peak_mb"] or 0, rss)
        coverage = min(1, covered / duration_ns)
        for stats in roles.values():
            cpu_weight, memory_weight = stats.pop("cpu_weight"), stats.pop("memory_weight")
            cpu_average, memory_average = ratio(stats.pop("cpu_sum"), cpu_weight), ratio(stats.pop("memory_sum"), memory_weight)
            stats["cpu_coverage_ratio"], stats["memory_coverage_ratio"] = cpu_weight / duration_ns, memory_weight / duration_ns
            stats["cpu_average_diagnostic_percent"] = cpu_average
            stats["memory_average_diagnostic_mb"] = memory_average
            stats["cpu_average_percent"] = cpu_average if cpu_weight / duration_ns >= .95 and not malformed else None
            stats["memory_average_mb"] = memory_average if memory_weight / duration_ns >= .95 and not malformed else None
            stats["quality"] = "valid" if min(cpu_weight, memory_weight) / duration_ns >= .95 and not malformed else "incomplete"
        for bucket in buckets:
            weight = bucket.pop("cpu_weight")
            bucket["host_cpu_average_percent"] = ratio(bucket.pop("cpu_sum"), weight)
            bucket["cpu_coverage_ratio"] = weight / ((bucket["end_s"] - bucket["start_s"]) * 1e9)
        host_average = ratio(host_sum, host_weight)
        required_roles = {"publisher", "infrastructure"} if node == spec.get("nodes", {}).get("sender", "A") else {"subscriber"}
        missing_roles = sorted(required_roles - roles.keys())
        valid = (not malformed and coverage >= .95 and min(host_weight, rss_weight) / duration_ns >= .95
                 and not missing_roles and all(roles[role]["quality"] == "valid" for role in required_roles))
        result["nodes"][node] = {"roles": roles, "host_cpu_average_percent": host_average if valid else None,
                                 "host_cpu_average_diagnostic_percent": host_average,
                                 "memory_peak_mb": peak if valid else None,
                                 "memory_peak_observed_mb": peak, "coverage_ratio": coverage,
                                 "missing_roles": missing_roles,
                                 "quality": "valid" if valid else "incomplete", "buckets": buckets,
                                 "cpu_high_load": (host_average is not None and host_average > 85) or any(
                                     bucket["end_s"] - bucket["start_s"] == 10
                                     and bucket["cpu_coverage_ratio"] >= .95
                                     and (bucket["host_cpu_average_percent"] or 0) > 85 for bucket in buckets),
                                 "memory_growth_slope_mb_per_min": _slope(
                                     [(bucket["start_s"] + bucket["end_s"]) / 120 for bucket in buckets],
                                     [bucket["memory_peak_mb"] for bucket in buckets]) if valid else None}
    peaks = [node["memory_peak_mb"] for node in result["nodes"].values()]
    if all(peak is not None for peak in peaks):
        result["peak_sum_mb"] = sum(peaks)
    if clock_quality["window_valid"] and all(node["quality"] == "valid"
                                             for node in result["nodes"].values()):
        node_ids = list(node_dirs)
        first, second = aligned[node_ids[0]], aligned[node_ids[1]]
        second_times = [item[0] for item in second]
        sums = []
        for timestamp, rss in first:
            position = bisect_left(second_times, timestamp)
            candidates = second[max(0, position - 1):position + 1]
            if candidates:
                nearest = min(candidates, key=lambda item: abs(item[0] - timestamp))
                if abs(nearest[0] - timestamp) <= 1.5 * period_ns:
                    sums.append(rss + nearest[1])
        if first and len(sums) / len(first) >= .95:
            result["synchronous_peak_mb"] = max(sums)
            result["synchronous_peak_quality"] = "valid"
            result["synchronous_peak_method"] = "nearest calibrated samples within 1.5 sampling intervals"
    return result


def _cd4(db, spec, source, sender_start, receiver_start, models, clock_quality, links,
         latency_states, runtime_failure):
    duration, rate, payload, subscribers = (spec[key] for key in
                                            ("duration_s", "rate_hz", "payload_bytes", "subscribers"))
    result = {"arrival_buckets": [], "source_cohort_buckets": [], "T_hold_s": None,
              "max_contiguous_stable_duration_s": None, "hold_status": "indeterminate",
              "hold_censored": False, "degradation_delta_pp": None,
              "degradation_slope_pp_per_min": None, "latency_p95_slope_ms_per_min": None,
              "backlog_growth_deliveries": None, "sustainability_verdict": "partial"}
    if receiver_start is None:
        return result
    cutoff = receiver_start + int((duration + spec["drain_s"]) * 1e9)
    count_valid = source["complete"] and source["submission_unknown_count"] == 0 and all(
        link["counts"]["complete"] for link in links)
    quality = clock_quality["window_valid"] and count_valid
    for index in range(math.ceil(duration / 10)):
        begin, end = index * 10, min(duration, (index + 1) * 10)
        local_begin, local_end = receiver_start + int(begin * 1e9), receiver_start + int(end * 1e9)
        bucket_clock = _clock_quality(models, (sender_start + int(begin * 1e9) if sender_start is not None else None,
                                               local_begin), end - begin, 0, spec.get("clock", {}))
        bucket_quality = quality and bucket_clock["window_valid"]
        uncertainty = (bucket_clock["u_window_ms"] or 0) * 1e6
        seq_begin, seq_end = math.ceil(begin * rate - 1e-9), math.ceil(end * rate - 1e-9)
        planned = seq_end - seq_begin
        submitted = db.execute("SELECT COUNT(*) FROM sends WHERE accepted=1 AND sequence>=? AND sequence<?",
                               (seq_begin, seq_end)).fetchone()[0] if source["complete"] else None
        arrival_links, cohort_links, unfinished = [], [], []
        for link in links:
            subscriber = link["subscriber_id"]
            rows_complete = link["counts"]["complete"] and source["submission_unknown_count"] == 0
            arrived = db.execute("SELECT COUNT(*) FROM receipts WHERE subscriber=? AND receive_ns>=? "
                                 "AND receive_ns<?", (subscriber, local_begin, local_end)).fetchone()[0]
            arrival_links.append({"subscriber_id": subscriber, "unique_deliveries": arrived if rows_complete else None,
                                  "throughput_mbps": arrived * payload * 8 / (end - begin) / 1e6
                                  if rows_complete else None})
            timely, final, boundary = db.execute(
                "SELECT SUM(receive_ns<?),SUM(receive_ns<?),SUM(ABS(receive_ns-?)<=?) FROM receipts "
                "WHERE subscriber=? AND sequence>=? AND sequence<?",
                (local_end, cutoff, local_end, uncertainty, subscriber, seq_begin, seq_end)).fetchone()
            timely, final, boundary = timely or 0, final or 0, boundary or 0
            lower = max(0, timely - boundary) / planned if bucket_quality else None
            upper = min(submitted or 0, timely + boundary) / planned if bucket_quality else None
            missing = 1 - final / submitted if submitted and rows_complete else None
            verdict = capacity_verdict(lower, upper, 0, bucket_quality, count_valid, runtime_failure)
            cohort_links.append({"subscriber_id": subscriber, "timely_deliveries": timely if bucket_quality else None,
                                 "final_deliveries": final if rows_complete else None,
                                 "final_missing_ratio": missing, "achievement_lower": lower,
                                 "achievement_upper": upper, "capacity_verdict": verdict,
                                 "target_achievement_ratio": timely / planned if bucket_quality else None,
                                 "boundary_count": boundary if bucket_quality else None})
            source_cutoff = sender_start + int(end * 1e9) if sender_start is not None else 0
            # Backlog is an as-of state, unlike the retrospective planned-sequence cohort.
            # An API call accepted after this bucket must not create earlier queue backlog.
            completed_to_date = db.execute(
                "SELECT COUNT(*) FROM receipts r JOIN sends s USING(sequence) "
                "WHERE r.subscriber=? AND s.accepted=1 AND s.return_ns<? AND r.receive_ns<?",
                (subscriber, source_cutoff, local_end)).fetchone()[0]
            submitted_to_date = db.execute("SELECT COUNT(*) FROM sends WHERE accepted=1 AND return_ns<?",
                                           (source_cutoff,)).fetchone()[0]
            near_boundary = db.execute(
                "SELECT COUNT(*) FROM receipts r JOIN sends s USING(sequence) "
                "WHERE r.subscriber=? AND s.accepted=1 AND s.return_ns<? AND ABS(r.receive_ns-?)<=?",
                (subscriber, source_cutoff, local_end, uncertainty)).fetchone()[0]
            unfinished.append({"point": submitted_to_date - completed_to_date,
                               "lower": max(0, submitted_to_date - completed_to_date - near_boundary),
                               "upper": min(submitted_to_date, submitted_to_date - completed_to_date + near_boundary)})
        known_arrivals = all(item["unique_deliveries"] is not None for item in arrival_links)
        result["arrival_buckets"].append({"index": index, "start_s": begin, "end_s": end,
                                          "per_subscriber": arrival_links,
                                          "unique_deliveries": sum(item["unique_deliveries"] for item in arrival_links)
                                          if known_arrivals else None,
                                          "throughput_mbps": sum(item["throughput_mbps"] for item in arrival_links)
                                          if known_arrivals else None})
        verdicts = [item["capacity_verdict"] for item in cohort_links]
        verdict = "fail" if "fail" in verdicts else "pass" if all(value == "pass" for value in verdicts) else "indeterminate"
        errors = set().union(*(state["cohort_main"].get(index, set()) for state in latency_states.values()))
        latency = _latency_stats(db, "cohort=? AND main=1", (index,), errors) if bucket_quality else None
        result["source_cohort_buckets"].append({"index": index, "start_s": begin, "end_s": end,
                                               "complete_bucket": end - begin == 10, "N_planned": planned,
                                               "N_sent": submitted, "finalized": count_valid,
                                               "window_quality": "valid" if bucket_quality else "unmeasurable",
                                               "u_window_ms": bucket_clock["u_window_ms"],
                                               "per_subscriber": cohort_links, "capacity_verdict": verdict,
                                               "worst_target_achievement_ratio": min(
                                                   item["target_achievement_ratio"] for item in cohort_links) if bucket_quality else None,
                                               "unfinished_deliveries": sum(item["point"] for item in unfinished) if bucket_quality else None,
                                               "unfinished_lower": sum(item["lower"] for item in unfinished) if bucket_quality else None,
                                               "unfinished_upper": sum(item["upper"] for item in unfinished) if bucket_quality else None,
                                               "latency_p95_ms": latency["latency_p95_ms"] if latency else None})
    buckets = result["source_cohort_buckets"]
    if not quality:
        result["reason"] = "source_cohort_metrics_require_complete_counts_and_valid_window_mapping"
        return result
    prefix, longest, current, stopped, unknown = 0, 0, 0, False, False
    for bucket in buckets:
        passed = bucket["capacity_verdict"] == "pass" and bucket["complete_bucket"]
        if passed:
            current += bucket["end_s"] - bucket["start_s"]
            longest = max(longest, current)
            if not stopped:
                prefix = current
        else:
            current, stopped = 0, True
        unknown |= bucket["capacity_verdict"] == "indeterminate" or not bucket["complete_bucket"]
    result.update(T_hold_s=prefix, max_contiguous_stable_duration_s=longest,
                  hold_status="indeterminate" if unknown else "observed",
                  hold_censored=not stopped)
    values = [bucket["worst_target_achievement_ratio"] for bucket in buckets]
    times = [(bucket["start_s"] + bucket["end_s"]) / 120 for bucket in buckets]
    if all(bucket["complete_bucket"] and bucket["window_quality"] == "valid" for bucket in buckets):
        result["degradation_slope_pp_per_min"] = _slope(times, [100 * value for value in values])
        result["latency_p95_slope_ms_per_min"] = _slope(times, [bucket["latency_p95_ms"] for bucket in buckets])
        if len(buckets) >= 20:
            result["degradation_delta_pp"] = 100 * (sum(values[-10:]) / 10 - sum(values[:10]) / 10)
            result["backlog_growth_deliveries"] = (sum(bucket["unfinished_deliveries"] for bucket in buckets[-10:])
                                                    - sum(bucket["unfinished_deliveries"] for bucket in buckets[:10])) / 10
            result["backlog_growth_lower"] = (sum(bucket["unfinished_lower"] for bucket in buckets[-10:])
                                               - sum(bucket["unfinished_upper"] for bucket in buckets[:10])) / 10
            result["backlog_growth_upper"] = (sum(bucket["unfinished_upper"] for bucket in buckets[-10:])
                                               - sum(bucket["unfinished_lower"] for bucket in buckets[:10])) / 10
    final_ok = all(link["metrics"]["final_missing_ratio"] is not None
                   and link["metrics"]["final_missing_ratio"] <= .001 + 1e-12 for link in links)
    threshold = max(10, .001 * (source["N_sent"] or 0) * subscribers)
    if runtime_failure or any(bucket["capacity_verdict"] == "fail" for bucket in buckets) or not final_ok:
        result["sustainability_verdict"] = "fail"
    elif result["backlog_growth_deliveries"] is not None and not unknown:
        result["sustainability_verdict"] = ("pass" if result["backlog_growth_upper"] <= threshold else
                                            "fail" if result["backlog_growth_lower"] > threshold else "indeterminate")
    result["backlog_growth_limit_deliveries"] = threshold
    return result


def analyze_run(run_dir: Path, spec: dict, clocks: list) -> dict:
    run_dir = Path(run_dir)
    spec = dict(spec)
    issues, runtime_errors, facility_errors = [], [], []
    controller = _load_json(run_dir / "controller.json", issues, required=False)
    controller_errors = controller.get("errors", []) if isinstance(controller, dict) else []
    sender = spec.get("nodes", {}).get("sender", "A")
    receiver = spec.get("nodes", {}).get("receiver", "B")
    config = spec.get("clock", {})
    directories = {node: run_dir / "nodes" / node for node in (sender, receiver)}
    starts, metadata, summaries, statuses = {}, {}, {}, {}
    models = {node: ClockModel([sample for sample in clocks if sample.get("node_id") == node], config)
              for node in directories}
    sealed = {}
    for node, directory in directories.items():
        arm = _load_json(directory / "arm.json", issues)
        starts[node] = arm.get("start_ns") if isinstance(arm, dict) else None
        metadata[node] = _load_json(directory / "node.json", issues)
        status = _load_json(directory / "agent_status.json", issues)
        statuses[node] = status
        failure_origin = status.get("failure_origin") if isinstance(status, dict) else None
        summary_paths = ([directory / "worker-publisher-0.summary.json"] if node == sender else
                         [directory / f"worker-subscriber-{index}.summary.json" for index in range(spec["subscribers"])])
        # Also accept the role spelling used by an agent implementation.
        summaries[node] = []
        for path in summary_paths:
            alternatives = [path, Path(str(path).replace("worker-publisher", "publisher")
                                      .replace("worker-subscriber", "subscriber"))]
            chosen = next((candidate for candidate in alternatives if candidate.exists()), path)
            item = _load_json(chosen, issues)
            summaries[node].append(item)
            if isinstance(item, dict) and (item.get("status") in ("error", "timeout") or item.get("errors")):
                evidence = {"node_id": node, "artifact": chosen.name, "evidence": item}
                if failure_origin in ("dut", "tested_system") or (
                        failure_origin is None and starts[node] is not None and item.get("status") != "cancelled"):
                    runtime_errors.append(evidence)
                else:
                    facility_errors.append(evidence)
        sealed[node] = isinstance(status, dict) and (status.get("state") == "completed"
                                                     or failure_origin == "cleanup") and all(
            isinstance(item, dict) and item.get("status", "completed") == "completed" for item in summaries[node])
        if isinstance(status, dict) and status.get("state") in ("error", "timeout"):
            evidence = {"node_id": node, "artifact": "agent_status.json", "evidence": status}
            if failure_origin in ("dut", "tested_system") or (failure_origin is None and starts[node] is not None):
                runtime_errors.append(evidence)
            else:
                facility_errors.append(evidence)
    clock_quality = _clock_quality((models[sender], models[receiver]), (starts[sender], starts[receiver]),
                                   spec["duration_s"], spec["drain_s"], config)
    clock_quality["nodes"] = {node: model.as_dict() for node, model in models.items()}
    runtime_failure = bool(runtime_errors)
    result = {key: spec.get(key) for key in ("run_id", "scenario", "direction", "rate_hz", "payload_bytes",
                                            "subscribers", "duration_s", "repeat", "profile", "level",
                                            "condition_id", "experiment_id", "stage")}
    result.update(middleware="MQTT", metric_definition_version="2.0", spec=spec, nodes=metadata,
                  worker_summaries=summaries, clock_quality=clock_quality)
    with tempfile.TemporaryDirectory(prefix="mqtt-cd-analysis-") as temporary:
        db = sqlite3.connect(str(Path(temporary) / "events.sqlite3"))
        try:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("CREATE TABLE sends(sequence INTEGER PRIMARY KEY,send_ns INTEGER,return_ns INTEGER,accepted INTEGER,api_ms REAL)")
            db.execute("CREATE TABLE receipts(subscriber INTEGER,sequence INTEGER,receive_ns INTEGER,PRIMARY KEY(subscriber,sequence))")
            db.execute("CREATE TABLE latencies(subscriber INTEGER,sequence INTEGER,ms REAL,main INTEGER,cohort INTEGER)")
            source = _import_source(db, directories[sender] / "publisher-0.csv", spec, starts[sender], issues)
            source["complete"] &= sealed[sender]
            sender_summary = summaries[sender][0] if summaries[sender] else None
            source_path = directories[sender] / "publisher-0.csv"
            # accepted excludes warmup, whereas subscriber.received covers every phase.
            for field, counter in (("raw_formal_accepted", "accepted"),
                                   ("raw_formal_api_rejected", "api_rejected"),
                                   ("local_queue_rejected_before_api", "queue_rejected")):
                source["complete"] &= _seal_counter(source[field], sender_summary, counter,
                                                     source_path, issues, required=counter == "accepted")
            if not source["complete"]:
                for key in ("N_attempt", "N_sent", "submission_unknown_count", "late_accept_count"):
                    source[key] = None
            receiver_rows = [_import_receiver(db, directories[receiver] / f"subscriber-{index}.csv", index,
                                              spec, source["complete"], issues) for index in range(spec["subscribers"])]
            for index, item in enumerate(receiver_rows):
                item["complete"] &= sealed[receiver]
                receiver_summary = summaries[receiver][index] if index < len(summaries[receiver]) else None
                receiver_path = directories[receiver] / f"subscriber-{index}.csv"
                item["complete"] &= _seal_counter(item["raw_rows"], receiver_summary, "received",
                                                   receiver_path, issues)
                item["complete"] &= _seal_counter(item["raw_invalid_rows"], receiver_summary, "invalid",
                                                   receiver_path, issues, required=False)
            counts_valid = source["complete"] and source["submission_unknown_count"] == 0 and all(
                item["complete"] for item in receiver_rows)
            receiver_start = starts[receiver]
            end = receiver_start + int(spec["duration_s"] * 1e9) if receiver_start is not None else None
            cutoff = end + int(spec["drain_s"] * 1e9) if end is not None else None
            uncertainty = clock_quality["u_window_ms"]
            links = []
            for index, counts in enumerate(receiver_rows):
                known = counts["complete"] and end is not None and source["submission_unknown_count"] == 0
                window = final = boundary = early = after = None
                if known:
                    window, final, early, after = db.execute(
                        "SELECT SUM(receive_ns>=? AND receive_ns<?),SUM(receive_ns<?),"
                        "SUM(receive_ns<?),SUM(receive_ns>=?) FROM receipts WHERE subscriber=?",
                        (receiver_start, end, cutoff, receiver_start, cutoff, index)).fetchone()
                    window, final, early, after = (value or 0 for value in (window, final, early, after))
                    if uncertainty is not None:
                        boundary = db.execute("SELECT COUNT(*) FROM receipts WHERE subscriber=? AND "
                                              "(ABS(receive_ns-?)<=? OR ABS(receive_ns-?)<=?)",
                                              (index, receiver_start, uncertainty * 1e6, end,
                                               uncertainty * 1e6)).fetchone()[0]
                sent = source["N_sent"] if source["complete"] and source["submission_unknown_count"] == 0 else None
                metrics = delivery_metrics(source["N_planned"], sent, 1, window, final,
                                           spec["duration_s"], spec["payload_bytes"])
                lower = max(0, window - boundary) / source["N_planned"] if boundary is not None else None
                upper = min(sent, window + boundary) / source["N_planned"] if boundary is not None and sent is not None else None
                verdict = capacity_verdict(lower, upper, metrics["final_missing_ratio"],
                                           clock_quality["window_valid"], counts_valid, runtime_failure)
                counts.update(N_recv_win=window, N_recv_final=final,
                              pre_window_unique_count=early, after_cutoff_unique_count=after)
                links.append({"subscriber_id": index, "counts": counts, "metrics": metrics,
                              "boundary_count": boundary, "achievement_lower": lower,
                              "achievement_upper": upper, "capacity_verdict": verdict})
            total_window = sum(link["counts"]["N_recv_win"] for link in links) if all(
                link["counts"]["N_recv_win"] is not None for link in links) else None
            total_final = sum(link["counts"]["N_recv_final"] for link in links) if all(
                link["counts"]["N_recv_final"] is not None for link in links) else None
            sent = source["N_sent"] if source["complete"] and source["submission_unknown_count"] == 0 else None
            expected = sent * spec["subscribers"] if sent is not None else None
            if counts_valid and not (0 <= total_window <= total_final <= expected
                                     and sent <= source["N_attempt"] <= source["N_planned"]):
                counts_valid = False
                issues.append({"code": "count_invariant_failed"})
                for link in links:
                    link["capacity_verdict"] = "fail" if runtime_failure else "indeterminate"
            metrics = delivery_metrics(source["N_planned"], sent, spec["subscribers"], total_window,
                                       total_final, spec["duration_s"], spec["payload_bytes"])
            api_mean, api_max, api_over_slot = db.execute(
                "SELECT AVG(api_ms),MAX(api_ms),SUM(api_ms>?) FROM sends", (1000 / spec["rate_hz"],)).fetchone()
            metrics.update(api_call_duration_mean_ms=api_mean, api_call_duration_max_ms=api_max,
                           api_calls_over_slot_count=api_over_slot,
                           sender_blocked=api_mean > 1000 / spec["rate_hz"] if api_mean is not None else None,
                           sender_blocked_definition="mean API call duration exceeds the configured inter-send interval",
                           queue_rejected=sender_summary.get("queue_rejected") if isinstance(sender_summary, dict) else None,
                           pending_at_deadline=sender_summary.get("pending_at_deadline") if isinstance(sender_summary, dict) else None)
            latency_states = _build_latencies(db, (models[sender], models[receiver]), receiver_start, spec)
            for link in links:
                index = link["subscriber_id"]
                state = latency_states[index]
                reasons = set(state["main"])
                if not link["counts"]["complete"]:
                    reasons.add("incomplete_count_evidence")
                link["latency"] = _latency_stats(db, "subscriber=? AND main=1", (index,), reasons,
                                                link["counts"]["N_recv_win"])
                link["latency_final"] = _latency_stats(db, "subscriber=?", (index,), state["final"] | reasons,
                                                      link["counts"]["N_recv_final"])
                link["metrics"].update({key: value for key, value in link["latency"].items()
                                         if key.startswith("latency_") or key.startswith("jitter_")})
            main_reasons = set().union(*(state["main"] for state in latency_states.values()))
            final_reasons = set().union(*(state["final"] for state in latency_states.values()))
            if not counts_valid:
                main_reasons.add("incomplete_count_evidence")
                final_reasons.add("incomplete_count_evidence")
            latency = _latency_stats(db, "main=1", (), main_reasons, total_window)
            latency_final = _latency_stats(db, "1", (), final_reasons, total_final)
            metrics.update({key: value for key, value in latency.items()
                            if key.startswith("latency_") or key.startswith("jitter_")})
            clock_quality["latency_valid"] = latency["quality"] == "valid"
            clock_quality["latency_reasons"] = latency["reasons"]
            verdicts = [link["capacity_verdict"] for link in links]
            verdict = "fail" if "fail" in verdicts else "pass" if all(item == "pass" for item in verdicts) else "indeterminate"
            result.update(counts={**source, "N_recv_win": total_window, "N_recv_final": total_final,
                                  "E": expected, "E_plan": source["N_planned"] * spec["subscribers"]},
                          metrics=metrics, per_subscriber=links, latency=latency, latency_final=latency_final,
                          capacity_verdict=verdict,
                          worst_subscriber_target_achievement_ratio=min(
                              link["metrics"]["end_to_end_achievement_ratio"] for link in links)
                          if total_window is not None else None,
                          latency_slo_verdict=("unmeasurable" if metrics["latency_p95_ms"] is None else
                                               "pass" if metrics["latency_p95_ms"] <= 50 else "fail"))
            if str(spec["scenario"]).startswith("CD4"):
                result["cd4"] = _cd4(db, spec, source, starts[sender], receiver_start,
                                      (models[sender], models[receiver]), clock_quality, links,
                                      latency_states, runtime_failure)
            else:
                result["cd4"] = None
        finally:
            db.close()
    result["resources"] = _resources(directories, starts, models, spec, clock_quality, issues)
    states = [status.get("state") for status in statuses.values() if isinstance(status, dict)]
    execution = ("timeout" if "timeout" in states else "error" if "error" in states or runtime_failure or controller_errors else
                 "cancelled" if "cancelled" in states else "completed" if all(sealed.values()) else "not_tested")
    resource_quality = "valid" if all(node["quality"] == "valid" for node in result["resources"]["nodes"].values()) else "incomplete"
    result.update(execution_status=execution,
                  run_validity="invalid" if not counts_valid else
                  "valid" if clock_quality["window_valid"] and resource_quality == "valid" else "partial",
                  metric_quality={"counts": "valid" if counts_valid else "incomplete",
                                  "local_throughput": "valid" if total_window is not None else "incomplete",
                                  "window": "valid" if clock_quality["window_valid"] else "unmeasurable",
                                  "latency": latency["quality"], "resources": resource_quality},
                  failure_origin="tested_system" if runtime_failure else
                  "experiment_facility" if facility_errors else "artifact_or_configuration" if not counts_valid else None,
                  runtime_errors=runtime_errors, facility_errors=facility_errors,
                  controller_errors=controller_errors, artifact_issues=issues,
                  limitations=sorted(set(clock_quality["reasons"] + latency["reasons"]
                                          + (["incomplete_artifacts"] if issues else []))),
                  metric_notes={"throughput_mbps": "Receiver local fixed formal window; aligned comparison requires window_valid.",
                                "final_missing_ratio": "Receiver local fixed cutoff; physical deadline comparability requires window_valid.",
                                "latency": "One-way, formal first valid arrivals; quantiles have minimum sample thresholds.",
                                "resource_cpu": "Process/role CPU uses one logical core=100%; host CPU is 0..100%."})
    # Keep live-run and offline reanalysis decisions identical, including late cancellation.
    if isinstance(controller, dict) and controller.get("cancelled"):
        result.update(execution_status="cancelled", capacity_verdict="indeterminate")
        if result["run_validity"] == "valid":
            result["run_validity"] = "partial"
    elif controller_errors:
        result["execution_status"] = "error"
        if result["capacity_verdict"] != "fail":
            result["capacity_verdict"] = "indeterminate"
        if result["run_validity"] == "valid":
            result["run_validity"] = "partial"
    if controller_errors:
        result["limitations"].extend(str(error) for error in controller_errors)
    if spec.get("local_check"):
        result["local_check"] = True
        result["limitations"].append("Local development check; not a two-machine Wi-Fi measurement")
    result["limitations"] = sorted(set(result["limitations"]))
    return result
