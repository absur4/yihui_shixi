"""Unified five-scenario publisher/subscriber measurement engine."""

import hashlib
import json
import os
import platform
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psutil
import vsoa

from standalone.io_utils import read_json, write_json
from standalone.processes import ProcessGroup
from standalone.resources import ResourceMeter
from standalone.statistics import METRICS, UNITS, aggregate, stats
from standalone.version import VERSION


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def measure_run(config, folder, log_folder, run_id, repeat):
    test_start = utc_now()
    started_ns = time.perf_counter_ns()
    middleware_pids = {}
    infrastructure_pids = {"controller": os.getpid()}
    discovery_samples = []
    discovery_condition_evidence = None
    transient_errors = []
    publisher_ports = [config["port"] + index for index in range(config["publisher_count"])]
    position_port = config["port"] + config["publisher_count"]
    impaired = config["transport_mode"] == "udp" and (config.get("force_proxy") or config.get("blackout_duration_seconds") or config["scenario_name"] == "weak_network_recovery"
               or any(config[key] for key in ("loss_rate", "network_delay_ms", "network_jitter_ms")))
    with ProcessGroup(folder, log_folder) as group:
        whole_limit = config["startup_timeout_seconds"] + config["duration_seconds"] + config["drain_seconds"] + config["recovery_timeout_seconds"] + 40
        watchdog = threading.Timer(whole_limit, group.close)
        watchdog.daemon = True
        watchdog.start()
        try:
            for identifier, port in enumerate(publisher_ports):
                group.start({"kind": "publisher", "identifier": identifier, "port": port, "config": config})
            for identifier in range(config["publisher_count"]):
                ready = group.wait(folder / f"publisher-{identifier}.ready.json", config["startup_timeout_seconds"])
                middleware_pids[f"publisher-{identifier}"] = ready["pid"]
            position_process = group.start({"kind": "position", "port": position_port, "config": config,
                                            "entries": {f"publisher-{identifier}": port for identifier, port in enumerate(publisher_ports)}})
            middleware_pids["position"] = position_process.pid
            vsoa.pos("127.0.0.1", position_port)
            deadline = time.monotonic() + config["startup_timeout_seconds"]
            while True:
                group.check()
                try:
                    found = vsoa.lookup("publisher-0")
                except ConnectionResetError as error:
                    transient_errors.append(str(error))
                    found = None
                if found == ("127.0.0.1", publisher_ports[0]):
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Position name resolution startup timeout")
                time.sleep(0.01)
            resolved_ports = []
            for identifier, expected_port in enumerate(publisher_ports):
                for sample in range(10):
                    began = time.perf_counter_ns()
                    found = vsoa.lookup(f"publisher-{identifier}")
                    discovery_samples.append((time.perf_counter_ns() - began) / 1e6)
                    if found != ("127.0.0.1", expected_port):
                        raise ValueError("Position endpoint differs from configured publisher")
                resolved_ports.append(found[1])
            condition = config.get("discovery_condition")
            if condition == "discovery_failure":
                began_failure = time.perf_counter_ns()
                missing_endpoint = vsoa.lookup("__vsoa_missing_service__")
                discovery_condition_evidence = {"expected": "not_found", "observed": missing_endpoint,
                                                "elapsed_ms": (time.perf_counter_ns() - began_failure) / 1e6,
                                                "passed": missing_endpoint is None}
            elif condition == "startup_timeout":
                began_timeout = time.perf_counter_ns()
                deadline_timeout = time.monotonic() + 0.1
                attempts = 0
                while time.monotonic() < deadline_timeout:
                    attempts += 1
                    if vsoa.lookup("__vsoa_startup_timeout__") is not None:
                        break
                discovery_condition_evidence = {"expected": "timeout_without_false_endpoint", "attempts": attempts,
                                                "elapsed_ms": (time.perf_counter_ns() - began_timeout) / 1e6,
                                                "passed": vsoa.lookup("__vsoa_startup_timeout__") is None}
            elif condition:
                discovery_condition_evidence = {"expected": condition, "resolved_endpoint_count": len(resolved_ports),
                                                "passed": len(resolved_ports) == config["publisher_count"]}
            endpoints = [list(resolved_ports) for subscriber in range(config["subscriber_count"])]
            if impaired:
                mappings = []
                for subscriber in range(config["subscriber_count"]):
                    for publisher in range(config["publisher_count"]):
                        proxy_port = position_port + 1 + subscriber * config["publisher_count"] + publisher
                        mappings.append({"proxy_port": proxy_port, "publisher_port": publisher_ports[publisher]})
                        endpoints[subscriber][publisher] = proxy_port
                proxy_process = group.start({"kind": "proxy", "config": config, "mappings": mappings})
                proxy_ready = group.wait(folder / "proxy-0.ready.json", config["startup_timeout_seconds"])
                infrastructure_pids["udp-proxy"] = proxy_ready.get("pid", proxy_process.pid)
            for identifier in range(config["subscriber_count"]):
                group.start({"kind": "subscriber", "identifier": identifier, "endpoints": endpoints[identifier], "config": config})
            for identifier in range(config["subscriber_count"]):
                ready = group.wait(folder / f"subscriber-{identifier}.ready.json", config["startup_timeout_seconds"])
                middleware_pids[f"subscriber-{identifier}"] = ready["pid"]
            startup_ms = (time.perf_counter_ns() - started_ns) / 1e6
            warmup_start_ns = time.perf_counter_ns() + 300_000_000
            start_ns = warmup_start_ns + int(config.get("warmup_seconds", 0) * 1e9)
            end_ns = start_ns + int(config["duration_seconds"] * 1e9)
            meter = ResourceMeter({"middleware": middleware_pids, "test_infrastructure": infrastructure_pids},
                                  start_ns, end_ns, config["resource_sample_interval_seconds"])
            write_json(folder / "start.json", {"warmup_start_ns": warmup_start_ns, "start_ns": start_ns, "end_ns": end_ns})
            publisher_reports = []
            for identifier in range(config["publisher_count"]):
                publisher_reports.append(group.wait(folder / f"publisher-{identifier}.sent.json", config["duration_seconds"] + 10))
            write_json(folder / "counts.json", {"publishers": [report["messages_sent"] for report in publisher_reports]})
            subscriber_reports = []
            for identifier in range(config["subscriber_count"]):
                subscriber_reports.append(group.wait(folder / f"subscriber-{identifier}.result.json",
                                                     config["drain_seconds"] + config["recovery_timeout_seconds"] + 10))
            resources = meter.finish(3)
            write_json(folder / "resource_samples.json", resources)
            write_json(folder / "finish.json", {"finished_ns": time.perf_counter_ns()})
            proxy_report = group.wait(folder / "proxy-0.result.json", 5) if impaired else None
        finally:
            watchdog.cancel()
    from array import array
    for report in subscriber_reports:
        for field in ("latencies_ms", "jitter_samples_ms"):
            if report.get(field + "_file"):
                values = array("d")
                values.frombytes((folder / report[field + "_file"]).read_bytes())
                report[field] = values
    latencies = [value for report in subscriber_reports for value in report["latencies_ms"]]
    jitter = [value for report in subscriber_reports for value in report["jitter_samples_ms"]]
    latency_distribution, jitter_distribution = stats(latencies), stats(jitter)
    sent = sum(report["messages_sent"] for report in publisher_reports)
    received = sum(report["initial_received"] for report in subscriber_reports)
    expected = sent * config["subscriber_count"]
    final_received = sum(report["final_received"] for report in subscriber_reports)
    within_window = sum(report["measurement_window_received"] for report in subscriber_reports)
    primary_bytes = within_window * config["message_size_bytes"]
    first_send_ns = min((report["first_successful_send_ns"] for report in publisher_reports if report.get("first_successful_send_ns")), default=None)
    last_send_ns = max((report["last_successful_send_ns"] for report in publisher_reports if report.get("last_successful_send_ns")), default=None)
    last_receive_ns = max((report["last_counted_receive_ns"] for report in subscriber_reports if report.get("last_counted_receive_ns")), default=None)
    send_window_seconds = (last_send_ns - first_send_ns) / 1e9 if first_send_ns and last_send_ns and last_send_ns > first_send_ns else None
    delivery_window_seconds = (last_receive_ns - first_send_ns) / 1e9 if first_send_ns and last_receive_ns and last_receive_ns > first_send_ns else None
    errors = []
    if sent == 0:
        errors.append("No messages were sent")
    for report in subscriber_reports:
        if report["invalid_messages"] or report["out_of_range_messages"]:
            errors.append(f"Invalid subscriber content: {report['subscriber']}")
        errors.extend(report["recovery_errors"])
    if proxy_report:
        for relay in proxy_report["relays"]:
            errors.extend(relay["errors"])
    fingerprint_fields = {key: config[key] for key in ("duration_seconds", "message_size_bytes", "publish_rate_hz",
                          "publisher_count", "subscriber_count", "transport_mode", "loss_rate", "network_delay_ms",
                          "network_jitter_ms", "recovery_enabled", "drain_seconds")}
    return {"schema_version": "2.1", "metric_definition_version": "1.0", "statistical_level": "run_level",
            "module_name": "vsoa", "module_version": VERSION, "run_id": run_id,
            "measurement_duration_seconds": config["duration_seconds"],
            "load_target_met": sent >= .95 * config["duration_seconds"] * config["publish_rate_hz"] * config["publisher_count"],
            "scenario_id": config.get("scenario_id"), "scenario_name": config["scenario_name"],
            "scenario_title": config.get("scenario_title"), "repeat": repeat,
            "status": "error" if errors else "completed",
            "payload_size_bytes": config["message_size_bytes"], "actual_payload_size_bytes": config["message_size_bytes"],
            "wire_message_size_bytes": None, "metadata_size_bytes": None,
            "publish_rate_hz": config["publish_rate_hz"], "configured_publish_rate_hz": config["publish_rate_hz"],
            "publisher_count": config["publisher_count"], "subscriber_count": config["subscriber_count"],
            "transport_mode": config["transport_mode"], "qos_profile": config.get("qos_profile", "default"),
            "network_profile": config.get("network_profile", "baseline"),
            "test_start_time": test_start, "test_end_time": utc_now(), "configuration": config,
            "comparison_fingerprint": hashlib.sha256(json.dumps(fingerprint_fields, sort_keys=True).encode()).hexdigest(),
            "latency_ms": latency_distribution["mean"], "latency_p95_ms": latency_distribution["p95"],
            "latency_p99_ms": latency_distribution["p99"], "latency_std_ms": latency_distribution["std"],
            "latency_variance_ms2": latency_distribution["variance"],
            "throughput_mbps": primary_bytes * 8 / delivery_window_seconds / 1e6 if delivery_window_seconds else None,
            "delivery_window_seconds": delivery_window_seconds, "send_window_seconds": send_window_seconds,
            "cpu_percent": resources["cpu_percent"], "memory_mb": resources["memory_mb"],
            "packet_loss": (expected - received) / expected if expected else None,
            "startup_time_ms": startup_ms, "discovery_time_ms": stats(discovery_samples)["mean"],
            "jitter_ms": jitter_distribution["mean"], "messages_sent": sent, "messages_received": received,
            "messages_planned": (config.get("message_count") or int(config["duration_seconds"] * config["publish_rate_hz"])) * config["publisher_count"],
            "messages_not_sent": max(0, (config.get("message_count") or int(config["duration_seconds"] * config["publish_rate_hz"])) * config["publisher_count"] - sent),
            "duplicate_count": sum(report.get("duplicate_messages", 0) for report in subscriber_reports),
            "corrupted_count": sum(report.get("corrupted_count", 0) for report in subscriber_reports),
            "unparseable_count": sum(report.get("unparseable_count", 0) for report in subscriber_reports),
            "out_of_order_count": sum(report.get("out_of_order_count", report.get("out_of_order_messages", 0)) for report in subscriber_reports),
            "expected_deliveries": expected, "messages_received_in_send_window": within_window,
            "messages_received_after_recovery": final_received,
            "final_packet_loss": (expected - final_received) / expected if expected else None,
            "application_recovered": sum(report["application_recovered"] for report in subscriber_reports),
            "late_native_deliveries": sum(report["late_native_deliveries"] for report in subscriber_reports),
            "application_retry_requests": sum(report["application_retry_requests"] for report in subscriber_reports),
            "recovery_time_ms": max((report["recovery_time_ms"] or 0 for report in subscriber_reports), default=0),
            "achieved_publish_rate_hz_per_publisher": sent / config["duration_seconds"] / config["publisher_count"],
            "offered_throughput_mbps": sent * config["message_size_bytes"] * 8 / send_window_seconds / 1e6 if send_window_seconds else None,
            "offered_source_payload_mbps": sent * config["message_size_bytes"] * 8 / send_window_seconds / 1e6 if send_window_seconds else None,
            "discovery_supported": True,
            "discovery_condition_evidence": discovery_condition_evidence,
            "measurement_window": {"warmup_start": warmup_start_ns, "measurement_start": start_ns,
                                   "send_end": end_ns, "drain_end": end_ns + int(config["drain_seconds"] * 1e9)},
            "statistics": {"latency_ms": latency_distribution, "jitter_ms": jitter_distribution,
                           "cpu_percent": resources["cpu_statistics"], "memory_mb": resources["memory_statistics"],
                           "discovery_time_ms": stats(discovery_samples)},
            "publisher_reports": publisher_reports,
            "publisher_metrics": publisher_reports,
            "subscriber_metrics": [{"subscriber": report["subscriber"], "messages_sent": report["expected_deliveries"],
                                    "messages_received": report["initial_received"],
                                    "messages_received_after_recovery": report["final_received"],
                                    "throughput_mbps": report["measurement_window_received"] * config["message_size_bytes"] * 8 / config["duration_seconds"] / 1e6,
                                    "packet_loss": (report["expected_deliveries"] - report["initial_received"]) / report["expected_deliveries"] if report["expected_deliveries"] else None,
                                    "final_packet_loss": (report["expected_deliveries"] - report["final_received"]) / report["expected_deliveries"] if report["expected_deliveries"] else None,
                                    "out_of_order_count": report.get("out_of_order_count", report.get("out_of_order_messages", 0)),
                                    "latency_ms": stats(report["latencies_ms"]), "jitter_ms": stats(report["jitter_samples_ms"])}
                                   for report in subscriber_reports],
            "link_metrics": [link for report in subscriber_reports for link in report.get("links", [])],
            "subscriber_reports": [{key: value for key, value in report.items()
                                    if key not in {"latencies_ms", "jitter_samples_ms", "recovery_latencies_ms"}}
                                   for report in subscriber_reports],
            "network_impairment": ({"injected_count": sum(relay["seen"] for relay in proxy_report["relays"]),
                                    "forwarded_count": sum(relay["forwarded"] for relay in proxy_report["relays"]),
                                    "dropped_count": sum(relay["dropped"] for relay in proxy_report["relays"]),
                                    "blackout_dropped_count": sum(relay["blackout_dropped"] for relay in proxy_report["relays"]),
                                    "recovery_time_ms": max((report["recovery_time_ms"] or 0 for report in subscriber_reports), default=0)}
                                   if proxy_report else None),
            "middleware_resources": resources["middleware"],
            "test_infrastructure_resources": resources["test_infrastructure"],
            "total_process_resources": resources["total"],
            "proxy_report": proxy_report, "position_startup_transient_errors": transient_errors,
            "resource_process_ids": resources["process_ids"], "errors": errors,
            "artifacts_directory": f"artifacts/{run_id}"}


def run_suite(config, cases, output, logs, log, selected="all", case_plan=None, resume=False):
    output.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    suite_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    began = utc_now()
    runs = []
    selected_cases = [case for case in cases if selected == "all" or case["scenario_name"] == selected]
    result_path = output / "result.json"
    if not selected_cases:
        raise ValueError("No supported scenario selected")
    identity = hashlib.sha256(json.dumps({"version": VERSION, "config": config, "cases": selected_cases}, sort_keys=True).encode()).hexdigest()
    if resume and result_path.exists():
        previous = read_json(result_path)
        if previous.get("suite_identity") != identity:
            raise ValueError("Resume configuration/suite mismatch")
        runs = [run for run in previous["runs"] if run["status"] == "completed"]
        began = previous["test_start_time"]

    def publish(status):
        summaries = [{"scenario_id": case.get("scenario_id"), "scenario_name": case["scenario_name"],
                      **aggregate([run for run in runs if run["scenario_name"] == case["scenario_name"]])}
                     for case in selected_cases]
        overall = aggregate(runs)
        result = {"schema_version": "2.1", "metric_definition_version": "1.0", "statistical_level": "suite_level",
                  "module_name": "vsoa", "module_version": VERSION, "run_id": suite_id,
                  "suite_identity": identity, "case_plan": case_plan or [],
                  "planned_runs": sum(case.get("case_repeats", config["repeats"]) for case in selected_cases),
                  "scenario_name": selected, "status": status, "test_start_time": began,
                  "test_end_time": utc_now() if status != "running" else None, **overall,
                  "summary_scope": "arithmetic means of completed run metrics; mixed scenarios are NOT a comparable score",
                  "environment": {"os": platform.platform(), "architecture": platform.machine(),
                                   "python_embedded": platform.python_version(), "vsoa": vsoa.__version__,
                                   "logical_cpus": psutil.cpu_count(),
                                   "topology": ("multi-machine IPv4, complete publisher-subscriber mesh"
                                                if config.get("execution_mode") == "multi_machine"
                                                else "IPv4 loopback, complete publisher-subscriber mesh"),
                                   "machine_name": platform.node()},
                  "units": UNITS, "configuration": config, "scenario_summaries": summaries, "runs": runs,
                  "limitations": ["Only VSOA is implemented; other middleware packages are not included",
                                   ("Cross-host one-way latency uses calibrated monotonic clock offsets; inspect clock_synchronization uncertainty"
                                    if config.get("execution_mode") == "multi_machine"
                                    else "Latency is same-host one-way publish-call to subscriber callback; not RTT/2"),
                                  "Throughput counts validated subscriber payload arriving inside the scheduled send window",
                                  "Broadcast throughput includes all subscriber deliveries; keep subscriber count identical across modules",
                                  "cpu_percent/memory_mb use middleware resources (publisher, subscriber and Position); test infrastructure is reported separately",
                                  "RSS sums may double-count shared pages; sampled peak may miss spikes",
                                  "packet_loss is initial missing deliveries after drain; final_packet_loss follows explicit TCP application replay",
                                  "Replay, deduplication and proxy handshake translation are harness logic, not native VSOA recovery",
                                  "Scheduling overload skips releases, recorded separately from network delivery loss",
                                   "No hard real-time, TLS, IPv6 or TCP kernel-loss claims"]}
        write_json(result_path, result)
        write_json(output / "history" / f"{suite_id}.json", result)
        return result

    publish("running")
    try:
        for case in selected_cases:
            for repeat in range(1, case.get("case_repeats", config["repeats"]) + 1):
                if any(run["scenario_name"] == case["scenario_name"] and run["repeat"] == repeat for run in runs):
                    continue
                effective = {**case, "seed": case["seed"] + repeat - 1,
                             "random_seed": case["seed"] + repeat - 1}
                run_id = f"{suite_id}_{case['scenario_name']}_{repeat}"
                folder = output / "artifacts" / run_id
                log(f"START {case['scenario_name']} repeat={repeat}/{config['repeats']}")
                try:
                    from standalone.faults import measure_fault
                    if effective.get("_distributed"):
                        from standalone.distributed import measure_distributed
                        measure = measure_distributed
                    else:
                        measure = measure_fault if effective.get("test_kind") == "fault_recovery" else measure_run
                    report = measure(effective, folder, logs / suite_id / f"{case['scenario_name']}_{repeat}", run_id, repeat)
                except Exception as error:
                    log(f"ERROR {type(error).__name__}: {error}")
                    report = {"schema_version": "2.1", "metric_definition_version": "1.0", "module_name": "vsoa", "run_id": run_id,
                              "scenario_id": case.get("scenario_id"), "scenario_name": case["scenario_name"],
                              "scenario_title": case.get("scenario_title"), "repeat": repeat, "status": "error",
                              "test_start_time": None, "test_end_time": utc_now(), "configuration": effective,
                              "errors": [f"{type(error).__name__}: {error}"], **{metric: None for metric in METRICS}}
                runs.append(report)
                write_json(output / "runs" / f"{run_id}.json", report)
                log(f"END {case['scenario_name']} status={report['status']} sent={report.get('messages_sent', 0)} received={report.get('messages_received', 0)}")
                publish("running")
    except KeyboardInterrupt:
        log("Cancelled by user; completed runs retained")
        return publish("cancelled")
    return publish("completed" if all(run["status"] == "completed" for run in runs) else "error")
