"""Real process crashes, socket resets, rediscovery and journal-assisted loss reconciliation."""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
import vsoa

from standalone.fault_workers import journal_rows
from standalone.io_utils import read_json, write_json
from standalone.processes import ProcessGroup
from standalone.statistics import stats
from standalone.version import VERSION


def measure_fault(config, folder, log_folder, run_id, repeat):
    folder = Path(folder)
    state = folder / "journals"
    state.mkdir(parents=True, exist_ok=True)
    began = datetime.now(timezone.utc).isoformat()
    start_ns = time.perf_counter_ns()
    events = []
    pids = {}
    previous_cpu = {}
    cpu_seconds = 0.0
    rss_samples = []
    controller = psutil.Process(os.getpid())
    controller_previous_cpu = None
    infrastructure_cpu_seconds = 0.0
    infrastructure_rss_samples = []

    def sample_resources():
        nonlocal cpu_seconds, controller_previous_cpu, infrastructure_cpu_seconds
        rss = 0
        for key, pid in list(pids.items()):
            try:
                process = psutil.Process(pid)
                times = process.cpu_times()
                current = times.user + times.system
                if pid in previous_cpu:
                    cpu_seconds += max(0, current - previous_cpu[pid])
                previous_cpu[pid] = current
                rss += process.memory_info().rss
            except psutil.Error:
                pass
        rss_samples.append(rss / 1e6)
        try:
            times = controller.cpu_times()
            current = times.user + times.system
            if controller_previous_cpu is not None:
                infrastructure_cpu_seconds += max(0, current - controller_previous_cpu)
            controller_previous_cpu = current
            infrastructure_rss_samples.append(controller.memory_info().rss / 1e6)
        except psutil.Error:
            infrastructure_rss_samples.append(0.0)

    with ProcessGroup(folder, log_folder) as group:
        generation = {"publisher": 0, "subscriber": 0, "position": 0}

        def start(role):
            identifier = generation[role]
            generation[role] += 1
            spec = {"kind": f"fault_{role}" if role != "position" else "position", "identifier": identifier,
                    "port": config["port"] if role != "position" else config["port"] + 1,
                    "config": config, "state_directory": str(state)}
            if role == "position":
                spec["entries"] = {"fault-service": config["port"]}
            process = group.start(spec)
            if role != "position":
                info = group.wait(folder / f"fault_{role}-{identifier}.ready.json", config["startup_timeout_seconds"])
                pids[role] = info["pid"]
            else:
                pids[role] = process.pid
            return process

        def subscriber_state():
            path = state / "subscriber_state.json"
            return read_json(path) if path.exists() else {}

        def wait_for(predicate, limit=None):
            deadline = time.monotonic() + (limit or config["fault_recovery_timeout_seconds"])
            while True:
                group.check()
                sample_resources()
                if predicate():
                    return time.perf_counter_ns()
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Fault recovery deadline: {config['fault_kind']}")
                time.sleep(0.02)

        def resolve():
            try:
                return vsoa.lookup("fault-service") == ("127.0.0.1", config["port"])
            except ConnectionResetError:
                return False

        publisher = start("publisher")
        position = start("position")
        vsoa.pos("127.0.0.1", config["port"] + 1)
        wait_for(resolve)
        lookups = []
        for sample in range(10):
            before = time.perf_counter_ns()
            assert resolve()
            lookups.append((time.perf_counter_ns() - before) / 1e6)
        subscriber = start("subscriber")
        wait_for(lambda: subscriber_state().get("subscribed") and subscriber_state().get("received", 0) >= 10)
        startup_ms = (time.perf_counter_ns() - start_ns) / 1e6
        for cycle in range(config["fault_cycles"]):
            baseline = subscriber_state()["received"]
            prior_events = journal_rows(state / "events.jsonl")
            disconnect_count = sum(row["event"] == "disconnected" for row in prior_events)
            fault_ns = time.perf_counter_ns()
            sample_resources()
            kind = config["fault_kind"]
            details = {}
            if kind == "publisher_restart":
                group.stop_owned(publisher)
                detected_ns = wait_for(lambda: sum(row["event"] == "disconnected" for row in journal_rows(state / "events.jsonl")) > disconnect_count)
                time.sleep(config["fault_downtime_seconds"])
                restoration_ns = time.perf_counter_ns()
                publisher = start("publisher")
            elif kind == "subscriber_restart":
                group.stop_owned(subscriber)
                detected_ns = time.perf_counter_ns()
                time.sleep(config["fault_downtime_seconds"])
                restoration_ns = time.perf_counter_ns()
                subscriber = start("subscriber")
            elif kind == "connection_reset":
                header, payload, result = vsoa.fetch(f"vsoa://127.0.0.1:{config['port']}/disconnect", timeout=2)
                if header is None or header.status != 0:
                    raise RuntimeError("Disconnect control request failed")
                detected_ns = wait_for(lambda: sum(row["event"] == "disconnected" for row in journal_rows(state / "events.jsonl")) > disconnect_count)
                restoration_ns = fault_ns
            else:
                group.stop_owned(position)
                before_failure_query = time.perf_counter_ns()
                unavailable = not resolve()
                detected_ns = time.perf_counter_ns()
                if not unavailable:
                    raise AssertionError("Position remained resolvable after termination")
                time.sleep(config["fault_downtime_seconds"])
                details["existing_subscription_progress_during_discovery_outage"] = subscriber_state()["received"] > baseline
                restoration_ns = time.perf_counter_ns()
                position = start("position")
                wait_for(resolve)
                details["failed_lookup_duration_ms"] = (detected_ns - before_failure_query) / 1e6
            recovery_ns = wait_for(lambda: subscriber_state().get("subscribed") and subscriber_state().get("received", 0) >= baseline + 10)
            if kind == "position_restart" and not details["existing_subscription_progress_during_discovery_outage"]:
                raise AssertionError("Established stream stopped during Position-only outage")
            observed = journal_rows(state / "events.jsonl")
            after_subscriptions = sum(row["event"] == "subscribed" and row.get("success") for row in observed)
            before_subscriptions = sum(row["event"] == "subscribed" and row.get("success") for row in prior_events)
            if kind != "position_restart" and after_subscriptions <= before_subscriptions:
                raise AssertionError("Delivery resumed without a recorded subscription acknowledgement")
            events.append({"cycle": cycle + 1, "fault_kind": kind, "fault_at_ns": fault_ns,
                           "detected_at_ns": detected_ns, "restoration_started_ns": restoration_ns,
                           "delivery_confirmed_ns": recovery_ns, "detection_ms": (detected_ns - fault_ns) / 1e6,
                           "restoration_to_delivery_ms": (recovery_ns - restoration_ns) / 1e6,
                           "fault_to_delivery_ms": (recovery_ns - fault_ns) / 1e6,
                           "new_subscription_acknowledgements": after_subscriptions - before_subscriptions,
                           "passed": True, **details})
        write_json(state / "freeze.json", {})
        wait_for(lambda: (state / "source_frozen.json").exists())
        time.sleep(config["drain_seconds"])
        initial_rows = journal_rows(state / "receipts.jsonl")
        source = journal_rows(state / "source.jsonl")
        source_ids = {row["sequence"] for row in source}
        original = {row["sequence"]: row for row in initial_rows if row["valid"] and not row["duplicate"]}
        before_replay_ns = time.perf_counter_ns()
        missing = sorted(source_ids - original.keys())
        write_json(state / "replay_request.json", {"sequences": missing})
        wait_for(lambda: (state / "replay_done.json").exists())
        replay_result = read_json(state / "replay_done.json")
        if replay_result["errors"]:
            raise RuntimeError(str(replay_result["errors"]))
        wait_for(lambda: subscriber_state().get("received", 0) == len(source_ids))
        sample_resources()
        all_receipts = journal_rows(state / "receipts.jsonl")
        final_ids = {row["sequence"] for row in all_receipts if row["valid"]}
        invalid = sum(not row["valid"] for row in all_receipts)
        if invalid or final_ids != source_ids:
            raise AssertionError("Fault recovery integrity or committed-message reconciliation failed")
        duration = (before_replay_ns - start_ns) / 1e9
        elapsed = (time.perf_counter_ns() - start_ns) / 1e9
        replay_ms = (time.perf_counter_ns() - before_replay_ns) / 1e6
    latencies = [(row["receive_ns"] - row["send_ns"]) / 1e6 for row in original.values()]
    latency = stats(latencies)
    ordered = sorted(original.values(), key=lambda row: row["sequence"])
    jitter = stats([abs((current["receive_ns"] - current["send_ns"]) - (previous["receive_ns"] - previous["send_ns"])) / 1e6
                    for previous, current in zip(ordered, ordered[1:]) if current["sequence"] == previous["sequence"] + 1])
    arrivals = sorted(row["receive_ns"] for row in original.values())
    receipt_gaps = stats([(later - earlier) / 1e6 for earlier, later in zip(arrivals, arrivals[1:])])
    highest_sequence = -1
    out_of_order_count = 0
    for row in sorted(original.values(), key=lambda value: value["receive_ns"]):
        out_of_order_count += int(row["sequence"] < highest_sequence)
        highest_sequence = max(highest_sequence, row["sequence"])
    return {"schema_version": "2.1", "metric_definition_version": "1.0", "statistical_level": "run_level",
            "module_name": "vsoa", "module_version": VERSION,
            "run_id": run_id, "scenario_id": config.get("scenario_id"), "scenario_name": config["scenario_name"],
            "scenario_title": config.get("scenario_title"), "repeat": repeat, "status": "completed",
            "test_start_time": began.replace("+00:00", "Z"), "test_end_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "configuration": config,
            "payload_size_bytes": config["message_size_bytes"], "actual_payload_size_bytes": config["message_size_bytes"],
            "wire_message_size_bytes": None, "metadata_size_bytes": None,
            "publish_rate_hz": config["publish_rate_hz"], "configured_publish_rate_hz": config["publish_rate_hz"],
            "publisher_count": config["publisher_count"], "subscriber_count": config["subscriber_count"],
            "transport_mode": config["transport_mode"], "qos_profile": config.get("qos_profile", "default"),
            "network_profile": config.get("network_profile", "baseline"), "discovery_supported": True,
            "measurement_duration_seconds": duration, "comparison_fingerprint": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
            "latency_ms": latency["mean"], "latency_p95_ms": latency["p95"], "latency_p99_ms": latency["p99"],
            "latency_std_ms": latency["std"], "latency_variance_ms2": latency["variance"], "jitter_ms": jitter["mean"],
            "throughput_mbps": len(original) * config["message_size_bytes"] * 8 / duration / 1e6,
            "cpu_percent": cpu_seconds / elapsed * 100, "memory_mb": max(rss_samples),
            "packet_loss": len(missing) / len(source_ids), "final_packet_loss": 0.0,
            "startup_time_ms": startup_ms, "discovery_time_ms": stats(lookups)["mean"],
            "messages_sent": len(source_ids), "messages_received": len(original), "expected_deliveries": len(source_ids),
            "messages_planned": len(source_ids), "messages_not_sent": 0,
            "duplicate_count": sum(row["duplicate"] for row in all_receipts),
            "out_of_order_count": out_of_order_count, "corrupted_count": invalid, "unparseable_count": 0,
            "messages_received_in_send_window": len(original), "messages_received_after_recovery": len(final_ids),
            "application_retry_requests": len(missing), "application_recovered": len(missing), "recovery_time_ms": replay_ms,
            "duplicate_messages": sum(row["duplicate"] for row in all_receipts), "invalid_messages": invalid,
            "fault_events": events, "fault_recovery_statistics": stats([event["fault_to_delivery_ms"] for event in events]),
            "delivery_gap_ms": receipt_gaps, "statistics": {"latency_ms": latency, "jitter_ms": jitter},
            "middleware_resources": {"cpu_percent": cpu_seconds / elapsed * 100, "memory_mb": max(rss_samples),
                                     "process_roles": ["publisher", "subscriber", "position"]},
            "test_infrastructure_resources": {"cpu_percent": infrastructure_cpu_seconds / elapsed * 100,
                                               "memory_mb": max(infrastructure_rss_samples),
                                               "process_roles": ["controller"]},
            "total_process_resources": {"cpu_percent": (cpu_seconds + infrastructure_cpu_seconds) / elapsed * 100,
                                        "memory_mb": max(a + b for a, b in zip(rss_samples, infrastructure_rss_samples))},
            "publisher_metrics": [], "subscriber_metrics": [], "link_metrics": [], "network_impairment": None,
            "proxy_report": None, "errors": [], "artifacts_directory": f"artifacts/{run_id}",
            "recovery_semantics": {"reconnection": "native Client.robot", "resubscription": "application onconnect callback",
                                   "sequence_continuity": "application source journal, not native persistence",
                                   "receipt_continuity": "application receipt journal across subscriber restarts",
                                   "loss_reconciliation": "explicit TCP replay of committed missing IDs",
                                   "power_failure_durability": "not_tested; flush is not fsync",
                                   "scope": "crash/restart of owned local processes; not automatic failover or kernel TCP-loss injection"}}
