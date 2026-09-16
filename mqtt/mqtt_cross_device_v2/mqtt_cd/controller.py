"""Authenticated two-node coordination. Never compare raw cross-host counters."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import random
import re
import shutil
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from .common import config_hash, safe_member, source_hash, token, validate_case, write_json


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Agent redirects are not allowed")


class NodeClient:
    def __init__(self, node_id, config):
        self.node_id = node_id
        self.url = config["url"].rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.control_sent_bytes = self.control_received_bytes = 0

    def _open(self, route, payload=None, timeout=5):
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        self.control_sent_bytes += len(data or b"")
        req = urllib.request.Request(self.url+route, data=data,
            headers={"Authorization": "Bearer "+token(), "Content-Type": "application/json"},
            method="GET" if payload is None else "POST")
        try:
            return self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace").replace(token(), "[REDACTED]")
            raise RuntimeError(f"Node {self.node_id} HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Node {self.node_id} unavailable: {exc.reason}") from None

    def request(self, route, payload=None, timeout=5):
        with self._open(route, payload, timeout) as response:
            body = response.read(4*1024*1024+1)
        self.control_received_bytes += len(body)
        if len(body) > 4*1024*1024:
            raise ValueError("Oversized agent JSON response")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Agent response must be an object")
        return value

    def calibrate(self, samples, count=9):
        for _ in range(count):
            t1 = time.perf_counter_ns()
            response = self.request("/clock", {}, timeout=3)
            t4 = time.perf_counter_ns()
            samples.append(dict(node_id=self.node_id, t1=t1, t2=response["receive_ns"],
                                t3=response["send_ns"], t4=t4))

    def artifacts(self, run_id, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        archive = destination.parent/(self.node_id+".zip.partial")
        count = 0
        try:
            with self._open("/artifact/"+run_id, timeout=30) as response, archive.open("wb") as stream:
                while chunk := response.read(1024*1024):
                    count += len(chunk)
                    if count > 2*1024**3:
                        raise ValueError("Archive exceeds 2GiB download limit")
                    stream.write(chunk)
            with zipfile.ZipFile(archive) as package:
                if sum(i.file_size for i in package.infolist()) > 8*1024**3:
                    raise ValueError("Archive exceeds 8GiB expanded limit")
                for entry in package.infolist():
                    if entry.is_dir():
                        continue
                    if stat.S_ISLNK(entry.external_attr >> 16):
                        raise ValueError("Symlinks are not accepted in artifacts")
                    parts = safe_member(entry.filename)
                    if any(p.startswith(".private") for p in parts):
                        raise ValueError("Agent included private material")
                    target = destination.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, target.open("xb") as sink:
                        shutil.copyfileobj(source, sink, 1024*1024)
        finally:
            archive.unlink(missing_ok=True)
        return count


def validate_config(config):
    if not isinstance(config, dict) or set(config.get("nodes", {})) != {"A", "B"}:
        raise ValueError("config.nodes must contain physical nodes A and B")
    for key, node in config["nodes"].items():
        url = urllib.parse.urlsplit(node["url"])
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError(f"Invalid agent URL for {key}")
        if url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("Agent URL must have no path, query or fragment")
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,253}", node.get("mqtt_host", "")):
            raise ValueError(f"Specify a reachable mqtt_host for node {key}")
    port = config.setdefault("broker_port", 1883)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("broker_port must be 1024..65535")
    clock = config.setdefault("clock", {})
    defaults = dict(model_validated=False, drift_bound_ppm=100, interval_s=5,
                    latency_limit_ms=1, window_limit_ms=20)
    for key, value in defaults.items():
        clock.setdefault(key, value)
    if not isinstance(clock["model_validated"], bool):
        raise ValueError("clock.model_validated must be a boolean")
    if clock["model_validated"] and not str(clock.get("model_validation_reference","")).strip():
        raise ValueError("Validated clocks require clock.model_validation_reference describing the calibration evidence")
    for key in ("drift_bound_ppm", "interval_s", "latency_limit_ms", "window_limit_ms"):
        value = clock[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid clock.{key}")
    if clock["interval_s"] > 10:
        raise ValueError("Clock interval must be <=10 seconds for lease and drift checks")
    if clock["latency_limit_ms"] > 1 or clock["window_limit_ms"] > 20:
        raise ValueError("The comparison profile requires latency_limit_ms<=1 and window_limit_ms<=20")
    cooldown = config.setdefault("cooldown_s", 20)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int,float)) or not math.isfinite(cooldown) or not 0 <= cooldown <= 300:
        raise ValueError("cooldown_s must be 0..300")
    return config


def health(config, allow_local=False):
    clients = {key: NodeClient(key, node) for key, node in config["nodes"].items()}
    reports = {}
    for key, client in clients.items():
        reports[key] = client.request("/health")
        if reports[key].get("node_id") != key:
            raise ValueError(f"Expected physical node {key}; agent reports {reports[key].get('node_id')}")
        if reports[key].get("source_sha256") != source_hash():
            raise ValueError(f"Node {key} code differs from this controller. Copy the same package to both computers and restart the agents")
    same = reports["A"].get("hostname") == reports["B"].get("hostname")
    if same and not allow_local:
        raise ValueError("Both agents report the same hostname. Use distinct computers; --allow-local-check is for development only")
    return clients, reports


def collect_prior_results(suite):
    if not isinstance(suite, dict):
        raise ValueError("Prior results must be a suite object")
    selected = {}
    for field in ("reference_runs", "runs"):
        rows = suite.get(field, [])
        if not isinstance(rows, list):
            raise ValueError(f"Prior suite {field} must be a list")
        for row in rows:
            if not isinstance(row, dict) or not row.get("run_id"):
                raise ValueError("Every prior run must have a run_id")
            identity = row["run_id"]
            if identity in selected and selected[identity] != row:
                raise ValueError(f"Conflicting evidence for prior run {identity}")
            selected[identity] = row
    return list(selected.values())


def case_identity(case):
    keys = ("scenario", "direction", "level", "stage", "measurement_kind", "profile", "qos",
            "rate_hz", "payload_bytes", "publishers", "subscribers", "duration_s", "warmup_s",
            "settle_s", "drain_s", "queue_messages", "queue_bytes", "sample_interval_s",
            "memory_limit_fraction", "comparison_fingerprint")
    return config_hash({key: case.get(key) for key in keys})[:24]


def wait_ready(client, run_id, timeout, heartbeat_clients):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        status = client.request("/status/"+run_id)
        if status["state"] == "ready":
            return status
        if status["state"] in ("error", "cancelled", "completed"):
            raise RuntimeError(f"Node {client.node_id} not ready: {status.get('errors', status['state'])}")
        for other in heartbeat_clients:
            if other is not client:
                other.request("/status/"+run_id)
        time.sleep(.5)
    raise TimeoutError(f"Node {client.node_id} preparation timed out")


def run_case(config, case, output, allow_local=False):
    from .analysis import analyze_run
    from .clock import estimate
    from .report import write_report

    cfg = validate_case(case)
    run_id = str(uuid.uuid4())
    directory = Path(output)/run_id
    directory.mkdir(parents=True, exist_ok=False)
    sender, receiver = ("A", "B") if cfg["direction"] == "A_to_B" else ("B", "A")
    spec = dict(cfg, run_id=run_id, nodes=dict(sender=sender, receiver=receiver),
                clock=dict(config["clock"]), metric_definition_version="2.0",
                source_sha256=source_hash(), condition_id=case_identity(cfg),
                scenario_id=cfg["scenario"], repeat_slot=cfg["repeat"],
                transport="TCP", mqtt_protocol="3.1.1", retain=False, clean_session=True,
                broker_location="sender", process_model="one_endpoint_per_process",
                control_channel="authenticated_http_poll_1s",
                local_check=bool(allow_local), created_at=dt.datetime.now(dt.timezone.utc).isoformat())
    spec["config_hash"] = config_hash(spec)
    write_json(directory/"spec.json", spec)
    clients = {k: NodeClient(k,n) for k,n in config["nodes"].items()}
    clocks, prepared, controller_errors = [], [], []
    cancelled = False
    statuses = {}
    print(f"RUN {run_id} {cfg['scenario']}/{cfg['level']} {cfg['direction']} repeat={cfg['repeat']}", flush=True)
    try:
        for key, role in ((sender, "sender"), (receiver, "receiver")):
            prepared.append(key)
            clients[key].request("/prepare", dict(run_id=run_id, role=role, case=cfg,
                broker_host=config["nodes"][sender]["mqtt_host"], broker_port=config.get("broker_port",1883)))
            wait_ready(clients[key], run_id, cfg["connect_timeout_s"]+10,
                       [clients[k] for k in prepared])
        for client in clients.values():
            client.calibrate(clocks)
        initial = {key: estimate([s for s in clocks if s["node_id"]==key]) for key in clients}
        start = time.perf_counter_ns()+int((cfg["warmup_s"]+cfg["settle_s"]+4)*1e9)
        spec["reference_start_ns"] = start
        spec["node_start_ns"] = {key: int(start+initial[key]["offset_ns"]) for key in clients}
        spec["config_hash"] = config_hash({k:v for k,v in spec.items() if k != "config_hash"})
        write_json(directory/"spec.json", spec)
        for key in (receiver, sender):
            clients[key].request("/arm/"+run_id, dict(start_ns=spec["node_start_ns"][key]))
        end = start+int((cfg["duration_s"]+cfg["drain_s"]+15)*1e9)
        next_calibration = time.monotonic()+config["clock"]["interval_s"]
        while time.perf_counter_ns() < end:
            for key in clients:
                statuses[key] = clients[key].request("/status/"+run_id)
            if any(s["state"] in ("error", "cancelled") for s in statuses.values()):
                raise RuntimeError("Node failed: "+json.dumps({k:s.get('errors',s['state']) for k,s in statuses.items()}))
            if all(s["state"] == "completed" for s in statuses.values()):
                break
            if time.monotonic() >= next_calibration:
                for client in clients.values():
                    client.calibrate(clocks)
                write_json(directory/"clocks.json", clocks)
                next_calibration = time.monotonic()+config["clock"]["interval_s"]
            time.sleep(1)
        else:
            raise TimeoutError("Run exceeded the fixed measurement and cleanup deadline")
        for client in clients.values():
            client.calibrate(clocks)
    except KeyboardInterrupt:
        cancelled = True
        controller_errors.append("Controller interrupted by user")
    except Exception as exc:
        controller_errors.append(str(exc).replace(token(), "[REDACTED]"))
    finally:
        write_json(directory/"clocks.json", clocks)
        for key in prepared:
            if controller_errors:
                try:
                    clients[key].request("/cancel/"+run_id, {}, timeout=15)
                except Exception as exc:
                    controller_errors.append(f"{key} cleanup: {str(exc).replace(token(),'[REDACTED]')}")
            try:
                clients[key].artifacts(run_id, directory/"nodes"/key)
            except Exception as exc:
                controller_errors.append(f"{key} artifacts: {str(exc).replace(token(),'[REDACTED]')}")
            try:
                clients[key].request("/release/"+run_id, {}, timeout=15)
            except Exception as exc:
                controller_errors.append(f"{key} release: {str(exc).replace(token(),'[REDACTED]')}")
        write_json(directory/"controller.json", dict(errors=controller_errors,statuses=statuses,
             cancelled=cancelled,control_json_payload_bytes={k:dict(sent=c.control_sent_bytes,received=c.control_received_bytes)
                                               for k,c in clients.items()}))
    try:
        result = analyze_run(directory, spec, clocks)
    except Exception as exc:
        result = dict(run_id=run_id,spec=spec,execution_status="error",run_validity="invalid",
                      capacity_verdict="indeterminate",metrics={},counts={},
                      controller_errors=controller_errors, local_check=bool(allow_local),
                      limitations=["Analysis failed: "+str(exc)])
        if cancelled:
            result["execution_status"] = "cancelled"
    write_json(directory/"result.json", result)
    write_report(directory/"report.html", result)
    print(f"RESULT {directory/'result.json'} status={result.get('execution_status')} capacity={result.get('capacity_verdict')}",flush=True)
    return result


def run_suite(config, cases, output, *, allow_local=False, cd4=False, refine=False,
              references=None, manual_rates=None, prior_results=None,
              selected_directions=None, generated_overrides=None, generated_repeats=5,
              planning_seed=20260916):
    from .planner import derive_cd4, next_boundary_cases, next_plateau_cases, summarize_scans
    from .report import write_report

    validate_config(config)
    selected_directions = selected_directions or ["A_to_B", "B_to_A"]
    comparison_fingerprint = config_hash(dict(
        {k:config.get(k) for k in ("nodes","broker_port","clock","environment","cooldown_s")},
        planning_seed=planning_seed))
    prior_results = collect_prior_results({"runs": prior_results or []})
    if any(r.get("spec",{}).get("comparison_fingerprint") != comparison_fingerprint for r in prior_results):
        raise ValueError("Prior results belong to another environment/configuration fingerprint")
    if references:
        refs = references.get("capacities",references.get("references",[])) if isinstance(references,dict) else references
        if not isinstance(refs,list) or any(r.get("comparison_fingerprint") != comparison_fingerprint for r in refs):
            raise ValueError("External capacities require the same comparison_fingerprint")
    _, health_reports = health(config, allow_local)
    output = Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    if (output/"result.json").exists() or (output/"schedule.json").exists():
        raise ValueError("Output already contains a suite. Choose a new output directory")
    suite = dict(schema_version="2.0",middleware="mqtt",status="running",runs=[],skipped=[],
                 experiment_id=str(uuid.uuid4()), source_sha256=source_hash(),
                 planning_seed=planning_seed,
                 configuration=config,node_health=health_reports,local_check=allow_local,
                 comparison_fingerprint=comparison_fingerprint,
                 started_at=dt.datetime.now(dt.timezone.utc).isoformat())
    suite["reference_run_ids"] = [r.get("run_id") for r in prior_results]
    suite["reference_runs"] = prior_results

    def annotate(case):
        value = dict(case, comparison_fingerprint=comparison_fingerprint,
                     experiment_id=suite["experiment_id"])
        if config["cooldown_s"] != 20:
            value["measurement_kind"] = "exploratory"
        return value

    schedule = [annotate(c) for c in cases]
    write_json(output/"schedule.json",schedule)

    def persist():
        suite["summary"] = summarize_scans(suite["reference_runs"]+suite["runs"])
        write_json(output/"experiment_manifest.json",{k:v for k,v in suite.items()
                   if k not in ("runs","reference_runs","summary")})
        write_json(output/"result.json",suite)
        write_report(output/"report.html",suite)

    last_finished = None

    def batch(batch_cases):
        nonlocal last_finished
        for index, case in enumerate(batch_cases):
            if last_finished is not None:
                delay = max(0,config.get("cooldown_s",20)-(time.monotonic()-last_finished))
                if delay:
                    print(f"COOLDOWN {delay:.1f}s",flush=True)
                    time.sleep(delay)
            result = run_case(config,case,output/"runs",allow_local)
            last_finished = time.monotonic()
            suite["runs"].append(result)
            persist()
            if result.get("execution_status") == "cancelled":
                raise KeyboardInterrupt

    def selected(extra):
        return [annotate(c) for c in extra
                if c["direction"] in selected_directions]

    try:
        batch(schedule)
        if refine:
            for _ in range(8):
                extra = next_boundary_cases(prior_results+suite["runs"],directions=selected_directions,
                                            seed=planning_seed)
                extra["cases"] = selected(extra["cases"])
                suite["skipped"].extend(extra.get("skipped",[]))
                if not extra["cases"]:
                    break
                schedule.extend(extra["cases"])
                write_json(output/"schedule.json",schedule)
                batch(extra["cases"])
            plateau = next_plateau_cases(prior_results+suite["runs"],seed=planning_seed)
            additions = selected(plateau["cases"])
            suite["skipped"].extend(plateau.get("skipped",[]))
            schedule.extend(additions)
            write_json(output/"schedule.json",schedule)
            batch(additions)
        if cd4:
            extra = derive_cd4(prior_results+suite["runs"],references=references,manual_rates=manual_rates,
                               seed=planning_seed)
            extra["cases"] = selected(extra["cases"])
            extra["cases"] = [c for c in extra["cases"] if c["repeat"] <= generated_repeats]
            if generated_overrides or generated_repeats != 5:
                updated = []
                for case in extra["cases"]:
                    original = {k:case[k] for k in ("rate_hz","payload_bytes","subscribers","duration_s","level","load_source")
                                if k in case}
                    value = validate_case(dict(case, **(generated_overrides or {}),
                                               measurement_kind="exploratory"))
                    value["derived_reference_case"] = original
                    value["load_source"] = "exploratory_override"
                    value["level"] = f"r{value['rate_hz']}-b{value['payload_bytes']}-p{value['subscribers']}-exploratory"
                    updated.append(value)
                extra["cases"] = updated
            suite["skipped"].extend(s for s in extra.get("skipped",[]) if s.get("direction") in selected_directions)
            schedule.extend(extra["cases"])
            write_json(output/"schedule.json",schedule)
            batch(extra["cases"])
        suite["status"] = "completed"
    except KeyboardInterrupt:
        suite["status"] = "cancelled"
    except Exception as exc:
        suite["status"] = "error"
        suite["error"] = str(exc).replace(token(),"[REDACTED]")
    finally:
        suite["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        persist()
    print(f"SUITE {output/'report.html'} status={suite['status']}",flush=True)
    return suite
