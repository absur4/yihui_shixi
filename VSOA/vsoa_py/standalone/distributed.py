"""Multi-machine VSOA measurement coordinator backed by authenticated agents."""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from array import array
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import vsoa

from standalone.engine import utc_now
from standalone.io_utils import write_json
from standalone.statistics import stats
from standalone.version import VERSION


class AgentClient:
    def __init__(self, url, token, timeout=10, owner=None):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.info = None
        self.offset_ns = 0
        self.clock_uncertainty_ns = 0
        self.owner = owner

    def request(self, method, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.url + path, data=data, method=method,
                          headers={"X-VSOA-Agent-Token": self.token,
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as error:
            try:
                detail = json.loads(error.read()).get("error")
            except Exception:
                detail = str(error)
            raise RuntimeError(f"agent {self.url}: {detail}") from None
        except URLError as error:
            raise ConnectionError(f"agent {self.url}: {error.reason}") from None

    def health(self):
        self.info = self.request("GET", "/v1/health")
        return self.info

    def calibrate_clock(self, samples=9):
        observations = []
        for _ in range(samples):
            before = time.perf_counter_ns()
            remote = self.request("GET", "/v1/clock")["monotonic_ns"]
            after = time.perf_counter_ns()
            observations.append((after - before, (before + after) // 2 - remote))
        rtt, offset = min(observations)
        self.offset_ns, self.clock_uncertainty_ns = offset, rtt // 2
        return {"offset_ns": offset, "uncertainty_ns": rtt // 2, "minimum_rtt_ns": rtt}

    def start(self, run_id, spec):
        return self.request("POST", "/v1/start", {"run_id": run_id, "owner": self.owner, "spec": spec})

    def put_bytes(self, run_id, name, data):
        return self.request("POST", "/v1/file", {"run_id": run_id, "name": name,
                                                   "data": base64.b64encode(data).decode("ascii")})

    def put_json(self, run_id, name, value):
        return self.put_bytes(run_id, name, json.dumps(value).encode("utf-8"))

    def get_bytes(self, run_id, name):
        query = urlencode({"run_id": run_id, "name": name})
        response = self.request("GET", "/v1/file?" + query)
        if not response.get("exists"):
            raise FileNotFoundError(name)
        return base64.b64decode(response["data"])

    def wait_bytes(self, run_id, name, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.get_bytes(run_id, name)
            except FileNotFoundError:
                status = self.request("GET", "/v1/status?" + urlencode({"run_id": run_id}))
                if status.get("errors"):
                    raise RuntimeError(f"remote worker error: {status['errors'][0]}") from None
                failed = {name: worker["returncode"] for name, worker in status.get("workers", {}).items()
                          if worker.get("returncode") not in {None, 0}}
                if failed:
                    raise RuntimeError(f"remote workers exited: {failed}")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"agent {self.url} timeout waiting for {name}") from None
                time.sleep(0.05)

    def wait_json(self, run_id, name, timeout):
        return json.loads(self.wait_bytes(run_id, name, timeout))

    def resources(self, run_id):
        return self.request("GET", "/v1/resources?" + urlencode({"run_id": run_id}))

    def stop(self, run_id):
        try:
            self.request("POST", "/v1/stop", {"run_id": run_id})
        except Exception:
            pass


class RemoteResourceMeter:
    def __init__(self, agents, run_id, start_ns, end_ns, interval):
        self.agents, self.run_id = agents, run_id
        self.start_ns, self.end_ns, self.interval = start_ns, end_ns, interval
        self.rows, self.error, self.result = [], None, None
        self.thread = threading.Thread(target=self.measure, daemon=True)
        self.thread.start()

    def snapshot(self):
        output = {}
        for index, agent in enumerate(self.agents):
            for name, values in agent.resources(self.run_id).items():
                output[f"agent-{index}/{name}"] = values
        return output

    def measure(self):
        try:
            delay = (self.start_ns - time.perf_counter_ns()) / 1e9
            if delay > 0:
                time.sleep(delay)
            previous, previous_ns = self.snapshot(), time.perf_counter_ns()
            baseline = previous
            while previous_ns < self.end_ns:
                time.sleep(min(self.interval, max(0, (self.end_ns - previous_ns) / 1e9)))
                current, current_ns = self.snapshot(), time.perf_counter_ns()
                elapsed = max((current_ns - previous_ns) / 1e9, 1e-9)
                keys = current.keys() & previous.keys()
                cpu = sum(current[k]["cpu_seconds"] - previous[k]["cpu_seconds"] for k in keys) / elapsed * 100
                memory = sum(value["rss_bytes"] for value in current.values()) / 1e6
                self.rows.append({"timestamp_ns": current_ns, "cpu_percent": cpu, "memory_mb": memory,
                                  "per_process": current})
                previous, previous_ns = current, current_ns
            elapsed = max((previous_ns - self.start_ns) / 1e9, 1e-9)
            keys = previous.keys() & baseline.keys()
            cpu_seconds = sum(previous[k]["cpu_seconds"] - baseline[k]["cpu_seconds"] for k in keys)
            cpu_values = [row["cpu_percent"] for row in self.rows]
            memory_values = [row["memory_mb"] for row in self.rows]
            summary = {"cpu_percent": cpu_seconds / elapsed * 100,
                       "memory_mb": max(memory_values, default=0), "cpu_time_seconds": cpu_seconds,
                       "observed_duration_seconds": elapsed, "cpu_statistics": stats(cpu_values),
                       "memory_statistics": stats(memory_values),
                       "process_ids": {name: value["pid"] for name, value in previous.items()}}
            empty = {"cpu_percent": 0, "memory_mb": 0, "cpu_time_seconds": 0,
                     "observed_duration_seconds": elapsed, "cpu_statistics": stats([]),
                     "memory_statistics": stats([]), "process_ids": {}}
            self.result = {"middleware": summary, "test_infrastructure": empty, "total": summary,
                           "cpu_percent": summary["cpu_percent"], "memory_mb": summary["memory_mb"],
                           "cpu_statistics": summary["cpu_statistics"],
                           "memory_statistics": summary["memory_statistics"], "samples": self.rows,
                           "process_ids": {"middleware": summary["process_ids"], "test_infrastructure": {}}}
        except Exception as error:
            self.error = error

    def finish(self, timeout=5):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("remote resource meter did not finish")
        if self.error:
            raise self.error
        return self.result


def _agents(configuration):
    token = configuration.get("token")
    definitions = configuration.get("agents") or []
    if not token:
        raise ValueError("multi-machine agent token is missing")
    clients = [AgentClient(item["url"] if isinstance(item, dict) else item, token,
                           owner=configuration.get("job_id")) for item in definitions]
    if len(clients) < 2 or len({item.url for item in clients}) < 2:
        raise ValueError("multi-machine mode requires at least two distinct agents")
    return clients


def measure_distributed(config, folder, log_folder, run_id, repeat):
    distributed = config.pop("_distributed")
    if config.get("test_kind") == "fault_recovery":
        raise ValueError("fault-recovery scenarios require distributed restart orchestration and are not supported")
    impaired = (config.get("blackout_duration_seconds") or
                any(config.get(key, 0) for key in ("loss_rate", "network_delay_ms", "network_jitter_ms")))
    if impaired:
        raise ValueError("synthetic loss/delay proxy is not supported in multi-machine mode; use the real network")
    clients = _agents(distributed)
    folder, log_folder = Path(folder), Path(log_folder)
    folder.mkdir(parents=True, exist_ok=True)
    log_folder.mkdir(parents=True, exist_ok=True)
    test_start, started_ns = utc_now(), time.perf_counter_ns()
    publisher_agents = [int(i) for i in distributed.get("publisher_agents", [])]
    subscriber_agents = [int(i) for i in distributed.get("subscriber_agents", [])]
    publisher_agents = publisher_agents or [i % len(clients) for i in range(config["publisher_count"])]
    subscriber_agents = subscriber_agents or [(i + 1) % len(clients) for i in range(config["subscriber_count"])]
    if len(publisher_agents) != config["publisher_count"] or len(subscriber_agents) != config["subscriber_count"]:
        raise ValueError("agent assignment count does not match publisher/subscriber count")
    if any(i < 0 or i >= len(clients) for i in publisher_agents + subscriber_agents):
        raise ValueError("agent assignment index is out of range")
    position_agent = int(distributed.get("position_agent", 0))
    publisher_ports = [config["port"] + i for i in range(config["publisher_count"])]
    position_port = config["port"] + config["publisher_count"]
    health, clock = [], []
    try:
        for client in clients:
            health.append(client.health())
            clock.append(client.calibrate_clock())
        clock_uncertainty_ns = max(item["uncertainty_ns"] for item in clock) * 2
        endpoints = [{"host": health[agent]["advertise_host"], "port": publisher_ports[i]}
                     for i, agent in enumerate(publisher_agents)]
        for identifier, agent_index in enumerate(publisher_agents):
            clients[agent_index].start(run_id, {"kind": "publisher", "identifier": identifier,
                "port": publisher_ports[identifier], "config": config,
                "clock_offset_ns": clients[agent_index].offset_ns,
                "clock_uncertainty_ns": clock_uncertainty_ns})
        entries = {f"publisher-{i}": endpoint for i, endpoint in enumerate(endpoints)}
        clients[position_agent].start(run_id, {"kind": "position", "port": position_port,
                                               "entries": entries, "config": config})
        for identifier, agent_index in enumerate(publisher_agents):
            clients[agent_index].wait_json(run_id, f"publisher-{identifier}.ready.json",
                                           config["startup_timeout_seconds"])
        position_host = health[position_agent]["advertise_host"]
        vsoa.pos(position_host, position_port)
        discovery_samples = []
        expected_addresses = [socket.gethostbyname(item["host"]) for item in endpoints]
        for identifier, endpoint in enumerate(endpoints):
            found = None
            deadline = time.monotonic() + config["startup_timeout_seconds"]
            while time.monotonic() < deadline:
                began = time.perf_counter_ns()
                try:
                    found = vsoa.lookup(f"publisher-{identifier}")
                except ConnectionResetError:
                    found = None
                discovery_samples.append((time.perf_counter_ns() - began) / 1e6)
                if found == (expected_addresses[identifier], endpoint["port"]):
                    break
                time.sleep(.02)
            else:
                raise TimeoutError(f"distributed Position lookup failed for publisher-{identifier}: {found}")
        for identifier, agent_index in enumerate(subscriber_agents):
            clients[agent_index].start(run_id, {"kind": "subscriber", "identifier": identifier,
                "endpoints": endpoints, "config": config, "clock_offset_ns": clients[agent_index].offset_ns,
                "clock_uncertainty_ns": clock_uncertainty_ns})
        for identifier, agent_index in enumerate(subscriber_agents):
            clients[agent_index].wait_json(run_id, f"subscriber-{identifier}.ready.json",
                                           config["startup_timeout_seconds"])
        startup_ms = (time.perf_counter_ns() - started_ns) / 1e6
        warmup_start_ns = time.perf_counter_ns() + max(2_000_000_000, len(clients) * 250_000_000)
        start_ns = warmup_start_ns + int(config.get("warmup_seconds", 0) * 1e9)
        end_ns = start_ns + int(config["duration_seconds"] * 1e9)
        control = {"warmup_start_ns": warmup_start_ns, "start_ns": start_ns, "end_ns": end_ns}
        for client in clients:
            client.put_json(run_id, "start.json", control)
        meter = RemoteResourceMeter(clients, run_id, start_ns, end_ns,
                                    config["resource_sample_interval_seconds"])
        publisher_reports = [clients[agent].wait_json(run_id, f"publisher-{i}.sent.json",
                             config["duration_seconds"] + config["warmup_seconds"] + 15)
                             for i, agent in enumerate(publisher_agents)]
        counts = {"publishers": [report["messages_sent"] for report in publisher_reports]}
        for client in clients:
            client.put_json(run_id, "counts.json", counts)
        subscriber_reports = []
        for identifier, agent_index in enumerate(subscriber_agents):
            client = clients[agent_index]
            report = client.wait_json(run_id, f"subscriber-{identifier}.result.json",
                                      config["drain_seconds"] + config["recovery_timeout_seconds"] + 15)
            for field in ("latencies_ms", "jitter_samples_ms"):
                filename = report.get(field + "_file")
                if filename:
                    raw = client.wait_bytes(run_id, filename, 5)
                    (folder / filename).write_bytes(raw)
                    values = array("d")
                    values.frombytes(raw)
                    report[field] = list(values)
            subscriber_reports.append(report)
            write_json(folder / f"subscriber-{identifier}.result.json", report)
        resources = meter.finish()
        for client in clients:
            client.put_json(run_id, "finish.json", {"finished_ns": time.perf_counter_ns()})
        for identifier, report in enumerate(publisher_reports):
            write_json(folder / f"publisher-{identifier}.sent.json", report)
        write_json(folder / "resource_samples.json", resources)
    finally:
        for client in clients:
            client.stop(run_id)

    latencies = [value for report in subscriber_reports for value in report.get("latencies_ms", [])]
    jitter = [value for report in subscriber_reports for value in report.get("jitter_samples_ms", [])]
    latency_distribution, jitter_distribution = stats(latencies), stats(jitter)
    sent = sum(report["messages_sent"] for report in publisher_reports)
    received = sum(report["initial_received"] for report in subscriber_reports)
    final_received = sum(report["final_received"] for report in subscriber_reports)
    expected = sent * config["subscriber_count"]
    within_window = sum(report["measurement_window_received"] for report in subscriber_reports)
    first_send = min((r["first_successful_send_ns"] for r in publisher_reports if r.get("first_successful_send_ns")), default=None)
    last_send = max((r["last_successful_send_ns"] for r in publisher_reports if r.get("last_successful_send_ns")), default=None)
    last_receive = max((r["last_counted_receive_ns"] for r in subscriber_reports if r.get("last_counted_receive_ns")), default=None)
    send_window = (last_send - first_send) / 1e9 if first_send and last_send and last_send > first_send else None
    delivery_window = (last_receive - first_send) / 1e9 if first_send and last_receive and last_receive > first_send else None
    errors = [error for report in subscriber_reports for error in report.get("recovery_errors", [])]
    if not sent:
        errors.append("No messages were sent")
    return {"schema_version": "2.1", "metric_definition_version": "1.0", "statistical_level": "run_level",
        "module_name": "vsoa", "module_version": VERSION, "run_id": run_id,
        "scenario_id": config.get("scenario_id"), "scenario_name": config["scenario_name"],
        "scenario_title": config.get("scenario_title"), "repeat": repeat,
        "status": "error" if errors else "completed", "test_start_time": test_start, "test_end_time": utc_now(),
        "measurement_duration_seconds": config["duration_seconds"], "configuration": config,
        "execution_mode": "multi_machine", "publisher_count": config["publisher_count"],
        "subscriber_count": config["subscriber_count"], "payload_size_bytes": config["message_size_bytes"],
        "actual_payload_size_bytes": config["message_size_bytes"], "publish_rate_hz": config["publish_rate_hz"],
        "configured_publish_rate_hz": config["publish_rate_hz"], "transport_mode": config["transport_mode"],
        "qos_profile": config.get("qos_profile", "default"), "network_profile": "physical_network",
        "latency_ms": latency_distribution["mean"], "latency_p95_ms": latency_distribution["p95"],
        "latency_p99_ms": latency_distribution["p99"], "latency_std_ms": latency_distribution["std"],
        "latency_variance_ms2": latency_distribution["variance"], "jitter_ms": jitter_distribution["mean"],
        "throughput_mbps": within_window * config["message_size_bytes"] * 8 / delivery_window / 1e6 if delivery_window else None,
        "delivery_window_seconds": delivery_window, "send_window_seconds": send_window,
        "cpu_percent": resources["cpu_percent"], "memory_mb": resources["memory_mb"],
        "packet_loss": (expected - received) / expected if expected else None,
        "final_packet_loss": (expected - final_received) / expected if expected else None,
        "startup_time_ms": startup_ms, "discovery_time_ms": stats(discovery_samples)["mean"],
        "messages_sent": sent, "messages_received": received, "messages_received_after_recovery": final_received,
        "messages_planned": (config.get("message_count") or int(config["duration_seconds"] * config["publish_rate_hz"])) * config["publisher_count"],
        "messages_not_sent": max(0, (config.get("message_count") or int(config["duration_seconds"] * config["publish_rate_hz"])) * config["publisher_count"] - sent),
        "expected_deliveries": expected, "messages_received_in_send_window": within_window,
        "duplicate_count": sum(r.get("duplicate_messages", 0) for r in subscriber_reports),
        "corrupted_count": sum(r.get("corrupted_count", 0) for r in subscriber_reports),
        "unparseable_count": sum(r.get("unparseable_count", 0) for r in subscriber_reports),
        "out_of_order_count": sum(r.get("out_of_order_count", 0) for r in subscriber_reports),
        "application_recovered": sum(r.get("application_recovered", 0) for r in subscriber_reports),
        "late_native_deliveries": sum(r.get("late_native_deliveries", 0) for r in subscriber_reports),
        "application_retry_requests": sum(r.get("application_retry_requests", 0) for r in subscriber_reports),
        "recovery_time_ms": max((r.get("recovery_time_ms") or 0 for r in subscriber_reports), default=0),
        "achieved_publish_rate_hz_per_publisher": sent / config["duration_seconds"] / config["publisher_count"],
        "load_target_met": sent >= .95 * config["duration_seconds"] * config["publish_rate_hz"] * config["publisher_count"],
        "discovery_supported": True, "discovery_condition_evidence": None,
        "statistics": {"latency_ms": latency_distribution, "jitter_ms": jitter_distribution,
                       "cpu_percent": resources["cpu_statistics"], "memory_mb": resources["memory_statistics"],
                       "discovery_time_ms": stats(discovery_samples)},
        "publisher_reports": publisher_reports, "publisher_metrics": publisher_reports,
        "subscriber_reports": subscriber_reports, "subscriber_metrics": subscriber_reports,
        "link_metrics": [link for report in subscriber_reports for link in report.get("links", [])],
        "middleware_resources": resources["middleware"], "test_infrastructure_resources": resources["test_infrastructure"],
        "total_process_resources": resources["total"], "resource_process_ids": resources["process_ids"],
        "network_impairment": None, "proxy_report": None, "errors": errors,
        "clock_synchronization": {"method": "minimum-RTT monotonic offset", "agents": clock,
                                  "maximum_uncertainty_ms": max(x["uncertainty_ns"] for x in clock) / 1e6},
        "nodes": health, "artifacts_directory": f"artifacts/{run_id}"}
