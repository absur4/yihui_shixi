"""Unified S01-S12 VSOA qualification scenario expansion."""

import copy
from standalone.configuration import numeric, validate_case

DEFAULT_QUALIFICATION = {"repeats": 5, "formal_repeats": 10, "stability_seconds": 300.0,
                         "fault_cycles": 3, "fault_downtime_seconds": 0.5,
                         "fault_recovery_timeout_seconds": 8.0}
PHASES = ("latency", "size_scan", "throughput", "rate_scan", "topology", "stability",
          "weak_network", "startup_discovery", "fault_recovery", "correctness")


def qualification_cases(config, phase="all"):
    settings = {**copy.deepcopy(DEFAULT_QUALIFICATION), **config.get("qualification", {})}
    if set(settings) != set(DEFAULT_QUALIFICATION):
        raise ValueError("Unknown qualification field")
    for key, minimum, maximum, integer in (("repeats", 5, 30, True), ("formal_repeats", 10, 30, True),
        ("stability_seconds", 300, 3600, False), ("fault_cycles", 1, 10, True),
        ("fault_downtime_seconds", .1, 10, False), ("fault_recovery_timeout_seconds", 1, 60, False)):
        numeric(settings, key, minimum, maximum, integer)
    base = {key: copy.deepcopy(value) for key, value in config.items() if key not in {"scenarios", "qualification"}}
    base.update(message_size_bytes=1024, payload_size_bytes=1024, message_count=0, publish_rate_hz=1000,
                publisher_count=1, subscriber_count=1, transport_mode="tcp", loss_rate=0.0,
                network_loss_rate=0.0, network_delay_ms=0.0, network_jitter_ms=0.0,
                recovery_enabled=False, duration_seconds=10.0, case_repeats=settings["repeats"], test_kind="traffic")
    cases, plan = [], []

    def add(scenario_id, name, category, title, **overrides):
        if phase not in {"all", category}:
            return
        effective = {**copy.deepcopy(base), **overrides, "scenario_id": scenario_id,
                     "scenario_name": name, "scenario_title": title, "phase": category}
        effective["payload_size_bytes"] = effective["message_size_bytes"]
        effective["network_loss_rate"] = effective["loss_rate"]
        validate_case(effective)
        cases.append(effective)
        plan.append({"scenario_id": scenario_id, "scenario_name": name, "scenario_title": title,
                     "phase": category, "status": "planned", "reason": None,
                     "configuration": effective, "planned_repeats": effective["case_repeats"]})

    for rate in (100, 1000):
        add("S01", f"S01_1KiB_{rate}Hz", "latency", f"1 KiB @ {rate} Hz point-to-point latency",
            duration_seconds=10000 / rate, message_count=10000, publish_rate_hz=rate)
    for size in (1024, 4096, 16384, 32768, 49152, 65536, 262144, 1048576):
        add("S02", f"S02_{size}B", "size_scan", f"Payload size scan {size} bytes",
            message_size_bytes=size, publish_rate_hz=20, duration_seconds=10.0,
            fragmentation_mode="application" if size > 60000 else "native")
    for size, rate in ((65536, 100), (262144, 20), (1048576, 5)):
        add("S03", f"S03_{size}B_max_stable", "throughput", f"Large-message stable candidate {size} bytes",
            message_size_bytes=size, publish_rate_hz=rate, duration_seconds=10.0,
            rate_mode="maximum_stable_candidate", fragmentation_mode="application")
    for rate in (100, 1000, 5000, 10000):
        add("S04", f"S04_rate_{rate}Hz", "rate_scan", f"Rate scan {rate} Hz", publish_rate_hz=rate,
            duration_seconds=10.0, rate_mode="maximum_stable_candidate" if rate == 10000 else "fixed")
    add("S05", "S05_1P_4S_broadcast", "topology", "1 publisher / 4 subscriber broadcast", subscriber_count=4)
    add("S06", "S06_4P_1S_fanin", "topology", "4 publisher / 1 subscriber fan-in", publisher_count=4)
    add("S07", "S07_4P_4S_mesh", "topology", "4 publisher / 4 subscriber concurrent mesh", publisher_count=4, subscriber_count=4)
    add("S08", "S08_stability_300s", "stability", "300-second sustained stability",
        duration_seconds=settings["stability_seconds"], test_kind="stability", sample_capacity=10000)
    for name, loss, delay, jitter in (("baseline", 0, 0, 0), ("light", .01, 5, 5),
                                      ("medium", .05, 20, 20), ("heavy", .10, 50, 50)):
        add("S09", f"S09_{name}", "weak_network", f"Weak network profile {name}", transport_mode="udp",
            loss_rate=loss, network_delay_ms=delay, network_jitter_ms=jitter,
            recovery_enabled=True, force_proxy=True, network_profile=name)
    for condition in ("cold_start", "warm_start", "multi_endpoint", "simultaneous_discovery", "startup_timeout", "discovery_failure"):
        multi = condition in {"multi_endpoint", "simultaneous_discovery"}
        add("S10", f"S10_{condition}", "startup_discovery", condition.replace("_", " ").title(),
            case_repeats=max(10, settings["repeats"]), discovery_condition=condition,
            publisher_count=4 if multi else 1, subscriber_count=4 if multi else 1,
            duration_seconds=1.0, publish_rate_hz=100)
    for fault in ("publisher_restart", "subscriber_restart", "position_restart", "connection_reset"):
        add("S11", f"S11_{fault}", "fault_recovery", fault.replace("_", " ").title(), test_kind="fault_recovery",
            fault_kind=fault, publish_rate_hz=100, fault_cycles=settings["fault_cycles"],
            fault_downtime_seconds=settings["fault_downtime_seconds"],
            fault_recovery_timeout_seconds=settings["fault_recovery_timeout_seconds"], recovery_enabled=True)
    add("S11", "S11_network_outage", "fault_recovery", "Brief UDP network outage", duration_seconds=4.0,
        transport_mode="udp", recovery_enabled=True, force_proxy=True,
        blackout_offset_seconds=1.0, blackout_duration_seconds=1.0)
    add("S12", "S12_message_correctness", "correctness", "Envelope and payload correctness",
        correctness_checks=True, duration_seconds=10.0, publish_rate_hz=1000)
    return cases, plan
