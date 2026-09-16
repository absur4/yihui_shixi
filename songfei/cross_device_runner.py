"""Execute Songfei cross-device CD1-CD4 jobs without mixing local S01-S12 data."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VSOA_PY = ROOT / "VSOA" / "vsoa_py"
for path in (ROOT, VSOA_PY):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cross_device import METRIC_VERSION, PROFILE, normalize_run, normalize_suite


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def prepare_vsoa_cases(source_cases, distributed):
    """Give the VSOA engine unique names while preserving the CD grouping.

    Its resume/skip key is ``scenario_name + repeat``; using only CD1, CD2,
    etc. would incorrectly skip every later condition in the same scenario.
    """
    engine_cases = []
    for source in source_cases:
        case = dict(source)
        case["scenario_group"] = source["scenario_name"]
        case["scenario_name"] = source["condition_id"]
        case["_distributed"] = {
            **distributed,
            "publisher_agents": [0],
            "subscriber_agents": [1] * int(case["subscriber_count"]),
            "position_agent": 0,
        }
        engine_cases.append(case)
    return engine_cases


def run_vsoa(spec, token):
    from standalone.engine import run_suite

    output, logs = Path(spec["output"]), Path(spec["logs"])
    config = dict(spec.get("config") or {})
    source_cases = [dict(case) for case in spec["cases"]]
    distributed = {**spec["distributed"], "token": token}
    config.update(execution_mode="multi_machine", metric_definition_version=METRIC_VERSION,
                  profile=PROFILE)
    engine_cases = prepare_vsoa_cases(source_cases, distributed)
    report = run_suite(config, engine_cases, output, logs, log, "all", spec.get("plan") or [], False)
    for run in report.get("runs") or []:
        attempted = run.get("messages_sent")
        publisher_reports = run.get("publisher_reports") or []
        accepted = sum(item.get("publish_calls_accepted", item.get("messages_sent", 0))
                       for item in publisher_reports) if publisher_reports else attempted
        run["messages_attempted"] = attempted
        run["messages_sent"] = accepted
    report = normalize_suite(report, source_cases)
    write_json(output / "result.json", report)
    for run in report.get("runs") or []:
        if run.get("run_id"):
            write_json(output / "runs" / f"{run['run_id']}.json", run)
    return report


def run_zenoh(spec, token):
    from Zenoh.zenoh_bench.distributed import DistributedOrchestrator

    output = Path(spec["output"])
    output.mkdir(parents=True, exist_ok=True)
    sender, receiver = spec["distributed"]["agents"][:2]
    all_runs = []
    started = utc_now()
    for case in spec["cases"]:
        cfg = {
            "scenario_name": case["condition_id"],
            "payload_size_bytes": case["payload_size_bytes"],
            "message_count": 0,
            "publish_rate_hz": case["publish_rate_hz"],
            "publisher_count": 1,
            "subscriber_count": case["subscriber_count"],
            "transport_mode": "TCP",
            "qos_profile": "reliable-block",
            "warmup_seconds": case["warmup_seconds"],
            "drain_seconds": case["drain_seconds"],
            "duration_seconds": case["duration_seconds"],
            "repeats": case["case_repeats"],
            "random_seed": case["random_seed"],
            "discovery_timeout_seconds": 30,
            "recovery_timeout_seconds": 30,
        }
        nodes = [
            {"name": "S", "url": sender, "advertise_host": sender.split("//", 1)[-1].split(":", 1)[0],
             "publishers": 1, "subscribers": 0, "base_port": 7447},
            {"name": "R", "url": receiver, "advertise_host": receiver.split("//", 1)[-1].split(":", 1)[0],
             "publishers": 0, "subscribers": case["subscriber_count"], "base_port": 7547},
        ]
        log(f"START {case['condition_id']} repeats={case['case_repeats']}")
        result = DistributedOrchestrator(output / "artifacts", token).run(
            cfg, nodes, lambda stage, current, total, message: log(
                f"{case['condition_id']} {stage} {current}/{total}: {message}"))
        run_root = output / "artifacts" / str((result.get("config") or {}).get("run_id", ""))
        detailed_runs = [json.loads(path.read_text(encoding="utf-8"))
                         for path in sorted(run_root.glob("repeat-*/result.json"))]
        raw_runs = detailed_runs or result.get("runs") or result.get("repeat_results") or [result]
        for repeat, raw in enumerate(raw_runs, 1):
            raw = dict(raw)
            counts = raw.get("counts") or {}
            metrics = raw.get("metrics") or {}
            repeat_nodes = [node for node in result.get("nodes", [])
                            if node.get("repeat") in {None, repeat}]
            uncertainties = [node.get("clock_uncertainty_ms") for node in repeat_nodes
                             if node.get("clock_uncertainty_ms") is not None]
            raw.update(metrics)
            raw.setdefault("run_id", f"{result.get('run_id', case['condition_id'])}-r{repeat:02d}")
            raw.setdefault("repeat", repeat)
            raw.setdefault("status", "error" if raw.get("errors") else "completed")
            raw.setdefault("messages_attempted", counts.get("sent"))
            raw.setdefault("messages_sent", counts.get("emitted", counts.get("sent")))
            raw.setdefault("messages_received", counts.get("received"))
            raw.setdefault("messages_received_in_send_window", counts.get("received"))
            raw.setdefault("messages_received_after_recovery", counts.get("received"))
            raw.setdefault("duplicate_count", counts.get("duplicates", 0))
            raw.setdefault("out_of_order_count", counts.get("out_of_order", 0))
            raw.setdefault("corrupted_count", counts.get("corrupt", 0))
            raw["latency_samples"] = (raw.get("samples") or
                                      [sample for sample in result.get("samples", [])
                                       if sample.get("repeat") == repeat])
            raw["environment"] = {
                **(result.get("environment") or {}),
                "max_clock_uncertainty_ms": max(uncertainties) if uncertainties else None,
            }
            all_runs.append(normalize_run(raw, case))
    report = {"schema_version": "2.0", "metric_definition_version": METRIC_VERSION,
              "profile": PROFILE, "module_name": "zenoh", "status": "completed",
              "test_start_time": started, "test_end_time": utc_now(), "runs": all_runs,
              "planned_runs": sum(case["case_repeats"] for case in spec["cases"]),
              "completed_runs": len(all_runs), "limitations": []}
    write_json(output / "result.json", report)
    for run in all_runs:
        write_json(output / "runs" / f"{run['run_id']}.json", run)
    return report


def unsupported_report(spec, middleware, reason):
    output = Path(spec["output"])
    runs = []
    for case in spec["cases"]:
        for repeat in range(1, case["case_repeats"] + 1):
            run = normalize_run({"run_id": f"{case['condition_id']}-r{repeat:02d}",
                                 "repeat": repeat, "status": "unsupported",
                                 "messages_sent": None, "messages_received": None,
                                 "limitations": [reason]}, case)
            runs.append(run)
    report = {"schema_version": "2.0", "metric_definition_version": METRIC_VERSION,
              "profile": PROFILE, "module_name": middleware, "status": "completed",
              "runs": runs, "planned_runs": len(runs), "completed_runs": 0,
              "limitations": [reason]}
    write_json(output / "result.json", report)
    return report


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        print("用法: cross_device_runner.py <spec.json>", file=sys.stderr)
        return 2
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    token = os.environ.get("SONGFEI_AGENT_TOKEN")
    if not token:
        raise ValueError("SONGFEI_AGENT_TOKEN is required for cross-device mode")
    middleware = spec.get("middleware")
    total = sum(case["case_repeats"] for case in spec["cases"])
    log(f"CROSS-DEVICE 2.0 开始：{middleware}，{len(spec['cases'])} 个条件，共 {total} 轮")
    if middleware == "vsoa":
        report = run_vsoa(spec, token)
    elif middleware == "zenoh":
        report = run_zenoh(spec, token)
    else:
        report = unsupported_report(
            spec, middleware,
            "MQTT 跨设备 Agent 尚未提供可验证的 2.0 固定窗口提交证据；按规范标记 unsupported，不回退到本机或伪造结果。")
    log(f"RESULT {Path(spec['output']) / 'result.json'} status={report['status']}")
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
