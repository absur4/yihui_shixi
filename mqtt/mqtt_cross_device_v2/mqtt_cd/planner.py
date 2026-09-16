from __future__ import annotations

from collections import defaultdict
import math
import random
import statistics

from .common import validate_case


RATES = (100, 500, 1000, 2000, 5000, 10000)
PAYLOADS = (1024, 16384, 65536, 262144, 1048576)
DIRECTIONS = ("A_to_B", "B_to_A")
PROFILE = "sender_service_tcp_v2"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _field(row, name, default=None):
    aliases = {"scenario": "scenario_id", "rate_hz": "R_cfg", "payload_bytes": "S",
               "subscribers": "P", "publishers": "N_pub", "duration_s": "T",
               "repeat": "repeat_slot"}
    sources = (row, row.get("spec", {}))
    if name == "measurement_kind":
        if any(source.get("local_check") for source in sources):
            return "local_check"
        if any(source.get("exploratory") for source in sources):
            return "exploratory"
    for source in sources:
        for key in (name, aliases.get(name, name)):
            if key in source:
                return source[key]
    return default


def _list(value):
    return [value] if isinstance(value, str) else list(value)


def _cases(scenario, direction, rate, payload=1024, subscribers=1,
           duration=20, stage="scan", repeats=5, **extra):
    if not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= 5:
        raise ValueError("repeats must be an integer from 1 to 5; formal classification requires five slots")
    level = f"r{rate}-b{payload}-p{subscribers}-{stage}"
    return [validate_case(dict(scenario=scenario, direction=direction, rate_hz=rate,
                              payload_bytes=payload, subscribers=subscribers, duration_s=duration,
                              level=level, stage=stage, repeat=i, **extra))
            for i in range(1, repeats + 1)]


def build_scan(scenarios, directions, repeats=5, seed=20260916):
    scenarios, directions = _list(scenarios), _list(directions)
    if not scenarios or len(set(scenarios)) != len(scenarios) or any(s not in ("CD1", "CD2", "CD3") for s in scenarios):
        raise ValueError("scenarios must contain unique CD1/CD2/CD3 values")
    if not directions or len(set(directions)) != len(directions) or any(d not in DIRECTIONS for d in directions):
        raise ValueError("directions must contain unique A_to_B/B_to_A values")
    cases = []
    for direction in directions:
        for scenario in scenarios:
            if scenario == "CD1":
                for rate in RATES:
                    cases.extend(_cases(scenario, direction, rate, repeats=repeats, random_seed=seed))
            elif scenario == "CD2":
                for size in PAYLOADS:
                    cases.extend(_cases(scenario, direction, 200, payload=size, repeats=repeats, random_seed=seed))
            else:
                for count in (1, 2, 4):
                    cases.extend(_cases(scenario, direction, 1000, subscribers=count, repeats=repeats, random_seed=seed))
    # Repeat blocks preserve the five predefined slots; only their internal order is shuffled.
    rng = random.Random(seed)
    schedule = []
    for repeat in range(1, repeats + 1):
        block = [c for c in cases if c["repeat"] == repeat]
        rng.shuffle(block)
        schedule.extend(block)
    return schedule


def _verdict(row):
    verdict = row.get("capacity_verdict", "indeterminate")
    if verdict not in ("pass", "fail"):
        return "indeterminate"
    if verdict == "pass":
        clock = row.get("clock_quality", {})
        if (row.get("execution_status") != "completed" or
                clock.get("model_validated") is not True or clock.get("window_valid") is not True or
                row.get("run_validity") == "invalid"):
            return "indeterminate"
    if verdict == "fail" and row.get("failure_origin") in ("infrastructure", "collector", "external", "configuration", "artifact_or_configuration", "experiment_facility"):
        return "indeterminate"
    return verdict


def _stats(values):
    values = [v for v in values if _number(v)]
    return dict(valid_runs=len(values), median=statistics.median(values) if len(values) >= 4 else None,
                minimum=min(values) if len(values) >= 4 else None,
                maximum=max(values) if len(values) >= 4 else None,
                spread=max(values)-min(values) if len(values) >= 4 else None)


def _load_evidence(row):
    clock = row.get("clock_quality", {})
    receivers = row.get("per_subscriber", [])
    window_valid = clock.get("model_validated") is True and clock.get("window_valid") is True
    target_failed = window_valid and any(_number(s.get("achievement_upper")) and s["achievement_upper"] < .99 for s in receivers)
    metrics = row.get("metrics", {})
    blocked = metrics.get("sender_blocked") is True or metrics.get("sending_blocked") is True
    backlog = metrics.get("backlog_growth_deliveries", (row.get("cd4") or {}).get("backlog_growth_deliveries"))
    return target_failed or blocked or (window_valid and _number(backlog) and backlog > 0)


def classify_conditions(results):
    grouped = defaultdict(list)
    names = ("middleware", "profile", "scenario", "direction", "rate_hz", "payload_bytes",
             "publishers", "subscribers", "duration_s", "stage", "level", "measurement_kind", "comparison_fingerprint")
    defaults = ("MQTT", PROFILE, None, None, None, 1024, 1, 1, 20, "scan", "", "formal", None)
    for row in results:
        key = tuple(_field(row, name, default) for name, default in zip(names, defaults))
        grouped[key].append(row)
    output = []
    for key, rows in grouped.items():
        condition = dict(zip(names, key))
        slots = {}
        duplicate_slots = []
        for row in rows:
            slot = _field(row, "repeat")
            if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= 5:
                duplicate_slots.append("invalid_repeat_slot")
                continue
            if slot not in slots:
                slots[slot] = row
            else:
                old = slots[slot]
                replacement = row.get("replaces_run_id", _field(row, "replaces_run_id"))
                facility_invalid = old.get("run_validity") == "invalid" and old.get("failure_origin") in (
                    "infrastructure", "collector", "external", "configuration", "artifact_or_configuration", "experiment_facility")
                if facility_invalid and replacement == old.get("run_id") and not _field(old, "replaces_run_id"):
                    slots[slot] = row
                else:
                    duplicate_slots.append(slot)
        selected = list(slots.values())
        passed = sum(_verdict(row) == "pass" for row in selected)
        failed = sum(_verdict(row) == "fail" for row in selected)
        status = "unstable_or_insufficient"
        if len(slots) == 5 and not duplicate_slots:
            status = "stable_pass" if passed >= 4 else "stable_fail" if failed >= 4 else status
        keys = set().union(*(row.get("metrics", {}).keys() for row in selected)) if selected else set()
        metrics = {}
        for name in sorted(keys):
            values = []
            for row in selected:
                value = row.get("metrics", {}).get(name)
                quality = row.get("metric_quality", {}).get(name)
                if quality in ("unmeasurable", "insufficient_samples", "incomplete"):
                    continue
                if name.startswith("latency_") and not row.get("clock_quality", {}).get("latency_valid", False):
                    continue
                if _number(value):
                    values.append(value)
            metrics[name] = _stats(values)
        condition.update(status=status, passes=passed, failures=failed,
                         indeterminate=len(selected)-passed-failed, slots_present=len(slots),
                         overload_evidence_runs=sum(_load_evidence(row) for row in selected),
                         missing_slots=sorted(set(range(1, 6))-set(slots)), duplicate_slots=duplicate_slots,
                         execution_status_counts={state: sum(row.get("execution_status") == state for row in selected)
                                                  for state in sorted({str(row.get("execution_status")) for row in selected})},
                         run_ids=[row.get("run_id") for row in rows], metrics=metrics)
        output.append(condition)
    return sorted(output, key=lambda row: tuple(str(row[name]) for name in names))


def capacity_boundaries(results):
    grouped = defaultdict(list)
    for condition in classify_conditions(results):
        if (condition["measurement_kind"] == "formal" and condition["scenario"] == "CD1" and condition["payload_bytes"] == 1024 and
                condition["subscribers"] == condition["publishers"] == 1):
            grouped[(condition["middleware"], condition["profile"], condition["direction"], condition["comparison_fingerprint"])].append(condition)
    output = []
    for (middleware, profile, direction, fingerprint), all_conditions in grouped.items():
        confirmed = [c for c in all_conditions if c["duration_s"] == 60 and c["stage"] != "scan"]
        passed = sorted({c["rate_hz"] for c in confirmed if c["status"] == "stable_pass"})
        failed = sorted({c["rate_hz"] for c in confirmed if c["status"] == "stable_fail"})
        unknown = sorted({c["rate_hz"] for c in confirmed if c["status"] == "unstable_or_insufficient"})
        coarse_conflicts = sorted({c["rate_hz"] for c in confirmed for raw in all_conditions
                                   if raw["stage"] == "scan" and raw["rate_hz"] == c["rate_hz"] and
                                   {raw["status"], c["status"]} == {"stable_pass", "stable_fail"}})
        highest = max((c["rate_hz"] for c in confirmed), default=None)
        rpass, rfail = max(passed, default=None), min(failed, default=None)
        uncertain = bool(rpass is not None and any(rate <= rpass for rate in failed))
        uncertain = uncertain or bool(unknown) or bool(coarse_conflicts)
        if not confirmed:
            status = "unconfirmed"
        elif uncertain:
            status = "non_monotonic_or_uncertain"
        elif rpass is None:
            status = "below_range" if failed else "non_monotonic_or_uncertain"
        elif rfail is None:
            status = "not_reached"
        else:
            status = "bounded"
        output.append(dict(middleware=middleware, profile=profile, direction=direction,
                           comparison_fingerprint=fingerprint,
                           bound_status=status, validated=status in ("bounded", "not_reached", "below_range"),
                           R_pass=None if uncertain else rpass, R_fail=None if uncertain else rfail,
                           R_sustainable=None if uncertain else rpass, R_sat=None if uncertain else rfail,
                           highest_tested_rate_hz=highest, observed_pass_rates=passed,
                           observed_fail_rates=failed, uncertain_rates=unknown,
                           coarse_confirmation_conflicts=coarse_conflicts,
                           interval_ratio=rfail/rpass if status == "bounded" else None,
                           confirmed_condition_count=len(confirmed), source="60s_five_slot_confirmation"))
    return output


def _resume_condition(condition, repeats, seed):
    if condition["duplicate_slots"]:
        return []
    proposed = _cases(condition["scenario"], condition["direction"], condition["rate_hz"],
                      payload=condition["payload_bytes"], subscribers=condition["subscribers"],
                      duration=condition["duration_s"], stage=condition["stage"], repeats=repeats,
                      random_seed=seed)
    # Retain the original identity so resumed slots join that condition, not a new one.
    for case in proposed:
        case["level"] = condition["level"]
    return [case for case in proposed if case["repeat"] in condition["missing_slots"]]


def next_boundary_cases(results, directions=None, repeats=5, seed=20260916):
    conditions, cases, skipped = classify_conditions(results), [], []
    boundaries = {(b["profile"], b["direction"]): b for b in capacity_boundaries(results) if b["middleware"] == "MQTT"}
    for direction in _list(directions or DIRECTIONS):
        group = [c for c in conditions if c["measurement_kind"] == "formal" and c["scenario"] == "CD1" and c["middleware"] == "MQTT" and
                 c["profile"] == PROFILE and c["direction"] == direction and c["payload_bytes"] == 1024 and c["subscribers"] == 1]
        if len({c["comparison_fingerprint"] for c in group}) > 1:
            skipped.append(dict(direction=direction, reason="mixed_comparison_fingerprints"))
            continue
        pending = []
        for condition in group:
            if condition["duration_s"] == 60 and condition["stage"] in ("confirmation", "refinement", "extension"):
                pending.extend(_resume_condition(condition, repeats, seed))
        if pending:
            cases.extend(pending)
            continue
        coarse = [c for c in group if c["duration_s"] == 20 and c["stage"] == "scan"]
        stable_pass = [c["rate_hz"] for c in coarse if c["status"] == "stable_pass"]
        stable_fail = [c["rate_hz"] for c in coarse if c["status"] == "stable_fail"]
        confirmed = {c["rate_hz"] for c in group if c["duration_s"] == 60 and c["stage"] != "scan"}
        targets = set()
        if stable_pass:
            targets.add(max(stable_pass))
        if stable_fail:
            targets.add(min(stable_fail))
        missing = sorted(targets-confirmed)
        if missing:
            for rate in missing:
                cases.extend(_cases("CD1", direction, rate, duration=60, stage="confirmation", repeats=repeats, random_seed=seed))
            continue
        boundary = boundaries.get((PROFILE, direction), {})
        status = boundary.get("bound_status")
        rate, stage = None, "refinement"
        if status == "bounded":
            refinements = {c["rate_hz"] for c in group if c["stage"] == "refinement"}
            if len(refinements) < 3 and boundary["interval_ratio"] > 1.10:
                candidate = (boundary["R_pass"]+boundary["R_fail"])//2
                if candidate not in confirmed:
                    rate = candidate
        elif status == "not_reached" and set(RATES).issubset(stable_pass):
            for candidate in (20000, 40000):
                if candidate not in confirmed:
                    rate, stage = candidate, "extension"
                    break
        elif status == "below_range" and 100 in stable_fail:
            for candidate in (50, 10):
                if candidate not in confirmed:
                    rate, stage = candidate, "extension"
                    break
        if rate is not None:
            cases.extend(_cases("CD1", direction, rate, duration=60, stage=stage, repeats=repeats, random_seed=seed))
        else:
            skipped.append(dict(direction=direction, reason=status or "no_stable_coarse_condition"))
    random.Random(seed).shuffle(cases)
    return {"cases": cases, "skipped": skipped}


def _reference_valid(reference):
    return (reference.get("validated") is True and reference.get("profile", PROFILE) == PROFILE and
            reference.get("direction") in DIRECTIONS and reference.get("bound_status") in ("bounded", "not_reached", "below_range"))


def derive_cd4(results, references=None, manual_rates=None, seed=20260916):
    references = references or []
    if isinstance(references, dict):
        references = references.get("capacities", references.get("references", []))
    bounds = capacity_boundaries(results)
    relevant = [ref for ref in references+bounds if ref.get("profile", PROFILE) == PROFILE]
    mixed = len({ref.get("comparison_fingerprint") for ref in relevant}) > 1
    refs = {}
    for ref in references:
        if not mixed and _reference_valid(ref):
            refs[(str(ref.get("middleware", "")).upper(), ref["direction"])] = ref
    for ref in bounds:
        key = (str(ref["middleware"]).upper(), ref["direction"])
        if ref.get("profile", PROFILE) == PROFILE:
            refs.pop(key, None)
        if not mixed and _reference_valid(ref):
            refs[key] = ref
    expected = [(middleware, direction) for middleware in ("MQTT", "VSOA", "ZENOH") for direction in DIRECTIONS]
    complete = all(key in refs and _number(refs[key].get("R_pass")) and refs[key]["R_pass"] > 0 for key in expected)
    common = max(1, math.floor(.8*min(refs[key]["R_pass"] for key in expected))) if complete else None
    cases, skipped = [], []
    for direction in DIRECTIONS:
        own = refs.get(("MQTT", direction), {})
        for scenario in ("CD4-C", "CD4-L", "CD4-H"):
            rate, source, high_only = None, "validated_capacity", False
            if scenario == "CD4-C":
                rate = common
            elif scenario == "CD4-L" and _number(own.get("R_pass")) and own["R_pass"] > 0:
                rate = max(1, math.floor(.9*own["R_pass"]))
            elif scenario == "CD4-H":
                if _number(own.get("R_fail")) and own["R_fail"] > 0:
                    rate = math.ceil(1.2*own["R_fail"])
                elif own.get("bound_status") == "not_reached" and _number(own.get("highest_tested_rate_hz")):
                    rate, high_only = int(own["highest_tested_rate_hz"]), True
            manual = (manual_rates or {}).get(direction, {}).get(scenario)
            if manual is not None:
                if not isinstance(manual, int) or isinstance(manual, bool) or not 1 <= manual <= 1000000:
                    raise ValueError("manual CD4 rates must be integers in 1..1000000")
                rate, source, high_only = manual, "manual_exploratory", scenario == "CD4-H"
            if rate is None:
                skipped.append(dict(scenario=scenario, direction=direction, execution_status="not_tested",
                                    reason="mixed_comparison_fingerprints" if mixed else "all_six_validated_R_pass_required" if scenario == "CD4-C" else "validated_MQTT_direction_reference_missing"))
                continue
            if rate > 1000000:
                skipped.append(dict(scenario=scenario, direction=direction, execution_status="unsupported", reason="derived_rate_exceeds_executor_limit"))
                continue
            reference = ({"manual_rate_hz": rate} if source == "manual_exploratory" else
                         own if scenario != "CD4-C" else {f"{m}_{d}": refs[(m, d)] for m, d in expected})
            cases.extend(_cases(scenario, direction, rate, duration=300,
                                stage="manual" if source == "manual_exploratory" else "sustained",
                                warmup_s=10, drain_s=5, load_source=source, high_load_only=high_only,
                                reference=reference,
                                exploratory=source == "manual_exploratory", random_seed=seed))
    rng, schedule = random.Random(seed), []
    for repeat in range(1, 6):
        block = [case for case in cases if case["repeat"] == repeat]
        rng.shuffle(block)
        schedule.extend(block)
    return {"cases": schedule, "skipped": skipped}


def summarize_scans(results):
    conditions = classify_conditions(results)
    grouped = defaultdict(list)
    for row in conditions:
        if row["measurement_kind"] == "formal":
            grouped[(row["middleware"], row["profile"], row["direction"], row["comparison_fingerprint"])].append(row)
    knees, plateaus = [], []
    for (middleware, profile, direction, fingerprint), rows in grouped.items():
        identity = dict(middleware=middleware, profile=profile, direction=direction, comparison_fingerprint=fingerprint)
        scan = sorted((r for r in rows if r["scenario"] == "CD1" and r["stage"] == "scan" and r["duration_s"] == 20 and
                       r["payload_bytes"] == 1024 and r["subscribers"] == r["publishers"] == 1), key=lambda r:r["rate_hz"])
        baseline = next((r["metrics"].get("latency_p95_ms", {}).get("median") for r in scan if r["rate_hz"] == 100), None)
        knee, knee_status = None, "unmeasurable"
        if _number(baseline) and baseline > 0:
            knee_status = "not_observed"
            higher = [r for r in scan if r["rate_hz"] > 100]
            for index, row in enumerate(higher):
                p95 = row["metrics"].get("latency_p95_ms", {}).get("median")
                if not _number(p95) or p95 <= baseline*3:
                    continue
                if index+1 < len(higher):
                    nxt = higher[index+1]["metrics"].get("latency_p95_ms", {}).get("median")
                    if _number(nxt) and nxt > baseline*3:
                        knee, knee_status = row["rate_hz"], "observed_candidate"
                        break
                else:
                    knee, knee_status = row["rate_hz"], "candidate"
        knees.append(dict(identity, R_knee=knee, status=knee_status, baseline_p95_ms=baseline))
        payloads = sorted((r for r in rows if r["scenario"] == "CD2" and r["duration_s"] == 20 and r["stage"] == "scan" and
                           r["rate_hz"] == 200 and r["subscribers"] == r["publishers"] == 1), key=lambda r:r["payload_bytes"])
        candidates, collapses = [], []
        for i in range(2, len(payloads)):
            triple = payloads[i-2:i+1]
            values = [r["metrics"].get("throughput_mbps", {}).get("median") for r in triple]
            if not all(_number(v) for v in values) or min(values[:2]) <= 0:
                continue
            changes = [values[1]/values[0]-1, values[2]/values[1]-1]
            if any(change < -.05 for change in changes):
                collapses.append([r["payload_bytes"] for r in triple])
            if all(abs(change) <= .05 for change in changes):
                candidates.append(dict(payload_bytes=[r["payload_bytes"] for r in triple], throughput_mbps=values,
                                       overload_evidence=triple[-1]["overload_evidence_runs"] >= 4))
        # A short scan can suggest a plateau; only a separate long-window confirmation establishes it.
        plateau, confirmed_values = None, None
        for candidate in candidates:
            conf = [next((r for r in rows if r["scenario"] == "CD2" and r["duration_s"] == 60 and r["stage"] != "scan" and
                          r["rate_hz"] == 200 and r["subscribers"] == r["publishers"] == 1 and r["payload_bytes"] == size), None) for size in candidate["payload_bytes"]]
            if not all(conf) or any(r["slots_present"] != 5 or r["duplicate_slots"] for r in conf):
                continue
            vals = [r["metrics"].get("throughput_mbps", {}).get("median") for r in conf]
            if (all(_number(v) for v in vals) and min(vals[:2]) > 0 and conf[-1]["overload_evidence_runs"] >= 4 and
                    abs(vals[1]/vals[0]-1) <= .05 and abs(vals[2]/vals[1]-1) <= .05):
                plateau, confirmed_values = statistics.mean(vals), vals
                break
        plateaus.append(dict(identity, B_plateau=plateau, components_mbps=confirmed_values,
                             status="confirmed" if plateau is not None else "candidate" if candidates else "collapse" if collapses else "not_observed_or_insufficient",
                             candidates=candidates, collapses=collapses, network_limited="unknown"))
    return {"conditions": conditions, "boundaries": capacity_boundaries(results), "latency_knees": knees, "payload_plateaus": plateaus}


def next_plateau_cases(results, repeats=5, seed=20260916):
    summary, cases, skipped = summarize_scans(results), [], []
    for plateau in summary["payload_plateaus"]:
        if plateau["middleware"] != "MQTT" or plateau["profile"] != PROFILE:
            continue
        if len({p["comparison_fingerprint"] for p in summary["payload_plateaus"] if
                p["middleware"] == "MQTT" and p["profile"] == PROFILE and p["direction"] == plateau["direction"]}) > 1:
            skipped.append(dict(direction=plateau["direction"], reason="mixed_comparison_fingerprints"))
            continue
        candidate = next((c for c in plateau["candidates"] if c["overload_evidence"]), None)
        if not candidate or plateau["status"] == "confirmed":
            skipped.append(dict(direction=plateau["direction"], reason=plateau["status"]))
            continue
        for size in candidate["payload_bytes"]:
            existing = [c for c in summary["conditions"] if c["scenario"] == "CD2" and c["middleware"] == "MQTT" and c["profile"] == PROFILE and
                        c["measurement_kind"] == "formal" and c["direction"] == plateau["direction"] and
                        c["duration_s"] == 60 and c["stage"] != "scan" and c["payload_bytes"] == size and
                        c["rate_hz"] == 200 and c["subscribers"] == c["publishers"] == 1]
            if existing:
                for condition in existing:
                    cases.extend(_resume_condition(condition, repeats, seed))
            else:
                cases.extend(_cases("CD2", plateau["direction"], 200, payload=size, duration=60,
                                    stage="confirmation", repeats=repeats, random_seed=seed))
    random.Random(seed).shuffle(cases)
    return {"cases": cases, "skipped": skipped}
