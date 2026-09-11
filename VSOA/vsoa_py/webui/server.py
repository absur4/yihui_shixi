"""VSOA visual benchmark web UI server.

Zero additional dependencies: uses Python stdlib for HTTP/SSE and the project's
existing test_*.py scripts as the only measurement implementation.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import mimetypes
import os
import queue
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
CATALOG_PATH = HERE / "test_catalog.json"
RESULT_ROOT = ROOT / "results" / "webui"
RESULT_ROOT.mkdir(parents=True, exist_ok=True)
CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
TESTS = {item["id"]: item for item in CATALOG}
ALLOWED_SOURCES = {str(Path(source).as_posix()) for item in CATALOG for source in item.get("sources", [])}

try:
    import psutil  # project already depends on it
except Exception:
    psutil = None


def json_dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def metric_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def dist_value(container: dict, key: str, stat: str = "mean") -> Any:
    value = container.get(key, {})
    if isinstance(value, dict):
        return metric_value(value.get(stat))
    return None


def pct_ratio(value: Any) -> Any:
    value = metric_value(value)
    return None if value is None else value * 100


def bytes_mb(value: Any) -> Any:
    value = metric_value(value)
    return None if value is None else value / 1_000_000


def extract_view(case_id: str, result: dict) -> dict:
    """Create a compact, UI-oriented summary while preserving raw result separately."""
    m = result.get("measurements") or {}
    status = result.get("status", "error")
    view: dict[str, Any] = {"status": status, "kpis": [], "rows": [], "series": [], "events": []}

    def k(label: str, value: Any, unit: str = "", tone: str = "normal"):
        view["kpis"].append({"label": label, "value": value, "unit": unit, "tone": tone})

    def row(label: str, value: Any, unit: str = ""):
        view["rows"].append({"label": label, "value": value, "unit": unit})

    if case_id in {"latency", "jitter", "realtime", "reliability"}:
        rtt = m.get("rtt", {})
        k("平均 RTT", rtt.get("mean"), "ms")
        k("P95 / P99", [rtt.get("p95"), rtt.get("p99")], "ms")
        k("成功率", pct_ratio(m.get("success_rate")), "%", "good")
        if case_id == "latency":
            one = m.get("client_to_server_handler", {})
            k("平均单向延迟", one.get("mean"), "ms")
            for key in ("min", "mean", "p50", "p95", "p99", "max", "stddev"):
                row(f"RTT {key.upper()}", rtt.get(key), "ms")
            for key in ("mean", "p95", "p99", "stddev"):
                row(f"单向 {key.upper()}", one.get(key), "ms")
        elif case_id == "jitter":
            variation = m.get("consecutive_absolute_rtt_variation", {})
            spacing = m.get("receive_minus_send_interval_absolute_error", {})
            k("RTT 标准差", metric_value(m.get("rtt_population_stddev")), "ms")
            k("相邻变化均值", variation.get("mean"), "ms")
            row("相邻 RTT 变化 P95", variation.get("p95"), "ms")
            row("收发间隔误差 P95", spacing.get("p95"), "ms")
        elif case_id == "realtime":
            k("Deadline miss", m.get("deadline_misses"), "次", "bad" if m.get("deadline_misses") else "good")
            k("Miss rate", pct_ratio(m.get("deadline_miss_rate")), "%")
            row("Deadline", metric_value(m.get("deadline")), "ms")
            row("计划释放→完成 P95", m.get("scheduled_release_to_completion", {}).get("p95"), "ms")
            row("调度迟到 P95", m.get("dispatch_lateness", {}).get("p95"), "ms")
        else:
            k("检测到超时", "是" if m.get("timeout_detected") else "否", "")
            k("超时后连接", "可用" if m.get("usable_after_timeout") else "异常", "")
            row("观察到的超时", metric_value(m.get("timeout_observed")), "ms")
            row("显式新会话恢复", m.get("manual_new_session_passed"), "")
        samples = m.get("samples") or []
        rtt_series = [s.get("rtt_ms") for s in samples if isinstance(s, dict) and s.get("ok") and isinstance(s.get("rtt_ms"), (int, float))]
        if rtt_series:
            view["series"].append({"name": "RTT", "unit": "ms", "values": rtt_series})
        if case_id == "latency":
            one_way = []
            for s in samples:
                if isinstance(s, dict) and s.get("ok") and isinstance(s.get("server_receive_ns"), int) and isinstance(s.get("send_ns"), int):
                    one_way.append((s["server_receive_ns"] - s["send_ns"]) / 1e6)
            if one_way:
                view["series"].append({"name": "单向", "unit": "ms", "values": one_way})
        if case_id == "realtime":
            lateness = []
            for s in samples:
                if isinstance(s, dict) and s.get("ok") and isinstance(s.get("scheduled_ns"), int) and isinstance(s.get("send_ns"), int):
                    lateness.append((s["send_ns"] - s["scheduled_ns"]) / 1e6)
            if lateness:
                view["series"].append({"name": "调度迟到", "unit": "ms", "values": lateness})

    elif case_id == "throughput":
        rate = metric_value(m.get("rpc_rate"))
        good = metric_value(m.get("one_direction_payload_goodput_mib"))
        bi = metric_value(m.get("bidirectional_payload_goodput"))
        k("有效 RPC 吞吐", rate, "RPC/s")
        k("单向 Goodput", good, "MiB/s")
        k("完成 / 失败", [m.get("completed_valid"), m.get("failed")], "")
        k("并发窗口", m.get("concurrency_window"), "")
        for label, key, unit in [
            ("尝试请求", "attempted", "次"), ("发送接受", "send_accepted", "次"),
            ("有效完成", "completed_valid", "次"), ("失败", "failed", "次"),
            ("RPC Rate", "rpc_rate", "RPC/s"), ("单向 Goodput", "one_direction_payload_goodput_mib", "MiB/s"),
            ("双向 Payload Goodput", "bidirectional_payload_goodput", "byte/s")]:
            row(label, metric_value(m.get(key)), unit)
        view["series"] = [{"name": "吞吐指标", "unit": "", "bars": [
            {"label": "RPC/s", "value": rate}, {"label": "MiB/s", "value": good},
            {"label": "完成", "value": m.get("completed_valid")}, {"label": "失败", "value": m.get("failed")}
        ]}]

    elif case_id == "cpu":
        processes = m.get("processes", {})
        client = processes.get("client", {})
        server = processes.get("server", {})
        c = metric_value(client.get("one_core_percent"))
        s = metric_value(server.get("one_core_percent"))
        total = metric_value(m.get("combined_one_core_percent"))
        k("Client CPU", c, "%")
        k("Server CPU", s, "%")
        k("合计单核口径", total, "%")
        k("RPC Rate", metric_value(m.get("rpc_rate")), "RPC/s")
        row("Client CPU time", metric_value(client.get("cpu_time")), "s")
        row("Server CPU time", metric_value(server.get("cpu_time")), "s")
        row("Client host capacity", metric_value(client.get("host_capacity_percent")), "%")
        row("Server host capacity", metric_value(server.get("host_capacity_percent")), "%")
        view["series"] = [{"name": "CPU", "unit": "%", "bars": [
            {"label": "Client", "value": c}, {"label": "Server", "value": s}, {"label": "Combined", "value": total}
        ]}]

    elif case_id == "memory":
        processes = m.get("processes", {})
        client = processes.get("client", {})
        server = processes.get("server", {})
        cpeak = bytes_mb(client.get("rss_sampled", {}).get("max"))
        speak = bytes_mb(server.get("rss_sampled", {}).get("max"))
        combined = bytes_mb(m.get("combined_sampled_peak_rss"))
        k("Client RSS 峰值", cpeak, "MB")
        k("Server RSS 峰值", speak, "MB")
        k("合计 RSS 峰值", combined, "MB")
        k("RPC Rate", metric_value(m.get("rpc_rate")), "RPC/s")
        row("Client RSS 增量", bytes_mb(client.get("rss_delta")), "MB")
        row("Server RSS 增量", bytes_mb(server.get("rss_delta")), "MB")
        samples = m.get("samples") or []
        cvals = [s.get("client") / 1_000_000 for s in samples if isinstance(s, dict) and isinstance(s.get("client"), (int, float))]
        svals = [s.get("server") / 1_000_000 for s in samples if isinstance(s, dict) and isinstance(s.get("server"), (int, float))]
        if cvals: view["series"].append({"name": "Client RSS", "unit": "MB", "values": cvals})
        if svals: view["series"].append({"name": "Server RSS", "unit": "MB", "values": svals})

    elif case_id == "startup_discovery":
        ready = metric_value(m.get("server_spawn_to_listener_ready"))
        first = metric_value(m.get("server_spawn_to_first_successful_rpc"))
        handshake = m.get("fresh_client_handshake", {})
        firstrpc = m.get("first_rpc_after_handshake", {})
        k("Server Ready", ready, "ms")
        k("首次可用 RPC", first, "ms")
        k("握手均值", handshake.get("mean"), "ms")
        k("首次 RPC 均值", firstrpc.get("mean"), "ms")
        row("握手 P95", handshake.get("p95"), "ms")
        row("首次 RPC P95", firstrpc.get("p95"), "ms")
        discovery = m.get("discovery", {})
        row("Position 首次解析", metric_value(discovery.get("position_spawn_to_first_resolution")), "ms")
        row("Position 热查询 P95", discovery.get("warm_lookup", {}).get("p95"), "ms")
        view["series"] = [{"name": "启动/发现", "unit": "ms", "bars": [
            {"label": "Server ready", "value": ready}, {"label": "First RPC", "value": first},
            {"label": "Handshake mean", "value": handshake.get("mean")}, {"label": "First RPC mean", "value": firstrpc.get("mean")}
        ]}]

    elif case_id == "packet_loss_recovery":
        injection = m.get("injection", {})
        app = m.get("application_retry", {})
        initial = pct_ratio(m.get("initial_loss_rate"))
        final = pct_ratio(app.get("remaining_loss_rate"))
        recovery = pct_ratio(app.get("recovery_rate"))
        observed = pct_ratio(injection.get("observed_proxy_drop_rate"))
        k("初始丢包率", initial, "%", "bad" if initial else "good")
        k("最终丢包率", final, "%", "good" if final == 0 else "bad")
        k("恢复率", recovery, "%", "good")
        k("代理实际丢弃", observed, "%")
        row("已发送唯一 ID", m.get("sent_unique"), "")
        row("首次收到", m.get("initial_received_unique"), "")
        row("恢复阶段耗时", metric_value(app.get("recovery_phase_duration")), "ms")
        row("总耗时", metric_value(m.get("total_duration")), "ms")
        for item in app.get("rounds", []) or []:
            view["events"].append({"label": f"补发第 {item.get('round')} 轮", "retransmitted": item.get("retransmitted"), "remaining": item.get("remaining_missing")})
        view["series"] = [{"name": "丢包/恢复", "unit": "%", "bars": [
            {"label": "代理丢弃", "value": observed}, {"label": "初始丢包", "value": initial},
            {"label": "最终丢包", "value": final}, {"label": "恢复率", "value": recovery}
        ]}]

    elif case_id == "discovery":
        warm = m.get("warm_lookup", {})
        first = metric_value(m.get("position_spawn_to_first_resolution"))
        k("首次解析", first, "ms")
        k("热查询均值", warm.get("mean"), "ms")
        k("热查询 P95", warm.get("p95"), "ms")
        k("未知服务", "正确返回 None" if m.get("unknown_service_returns_none") else "异常", "")
        row("发现机制", m.get("mechanism"), "")
        row("解析 endpoint", m.get("resolved_endpoint"), "")
        row("热查询 P99", warm.get("p99"), "ms")
        view["series"] = [{"name": "发现耗时", "unit": "ms", "bars": [
            {"label": "首次解析", "value": first}, {"label": "Warm mean", "value": warm.get("mean")},
            {"label": "Warm P95", "value": warm.get("p95")}, {"label": "Warm P99", "value": warm.get("p99")}
        ]}]

    elif case_id in {"communication_models", "transports", "cross_platform", "qos", "ecosystem"}:
        def status_of(value: Any) -> tuple[str, float]:
            if isinstance(value, bool): return ("通过" if value else "失败", 1 if value else 0)
            if isinstance(value, dict):
                if "passed" in value: return ("通过" if value.get("passed") else "失败", 1 if value.get("passed") else 0)
                s = value.get("status")
                if s: return (s, 1 if s in {"pass", "passed"} else 0.5 if s in {"partial", "documented_not_tested"} else 0)
            if isinstance(value, str): return (value, 0.5 if "not_" in value or "not " in value else 1)
            return (str(value), 0)

        if case_id == "communication_models":
            labels = [("RPC", m.get("rpc")), ("发布订阅", m.get("publish_subscribe")), ("TCP Datagram", m.get("tcp_datagram")), ("Quick UDP", m.get("quick_datagram")), ("Duplex Stream", m.get("duplex_stream"))]
        elif case_id == "transports":
            labels = [("TCP Datagram", m.get("tcp_datagram")), ("UDP Datagram", m.get("udp_quick_datagram")), ("UDP Publish", m.get("udp_quick_publish")), ("TCP Stream", m.get("tcp_stream")), ("TLS", m.get("tls")), ("IPv6", m.get("ipv6"))]
        elif case_id == "cross_platform":
            labels = [("本机 RPC", m.get("rpc_passed")), ("本机 Quick UDP", m.get("quick"))]
            for os_name, os_data in (m.get("platform_matrix") or {}).items(): labels.append((os_name, os_data))
        elif case_id == "qos":
            probes = m.get("priority_probes") or []
            labels = []
            if isinstance(probes, list):
                for p in probes: labels.append((f"Priority {p.get('requested', p.get('priority','?'))}", p.get('passed', p)))
            elif isinstance(probes, dict):
                for key, val in probes.items(): labels.append((f"Priority {key}", val))
            labels += [("非法值拒绝", m.get("invalid_values_rejected")), ("线缆 DSCP", m.get("wire_dscp_capture")), ("拥塞优先级", m.get("congestion_priority_effect"))]
        else:
            labels = [("版本", m.get("version")), ("License", m.get("license")), ("Python 要求", m.get("python_requirement")), ("公开 API", m.get("importable_api")), ("生产采用", m.get("production_adoption"))]
        bars = []
        passed = 0
        for label, val in labels:
            text, score = status_of(val)
            row(label, text, "")
            bars.append({"label": label, "value": score * 100})
            if score == 1: passed += 1
        k("测试状态", status.upper(), "")
        k("能力项", len(labels), "项")
        k("明确通过", passed, "项")
        k("层级", "功能", "")
        view["series"] = [{"name": "能力状态", "unit": "%", "bars": bars}]

    else:
        k("状态", status.upper(), "")

    return view


@dataclass
class Job:
    id: str
    request: dict
    created_at: str = field(default_factory=iso_now)
    status: str = "queued"
    current_case: str | None = None
    current_repeat: int | None = None
    events: list[dict] = field(default_factory=list)
    summary: dict | None = None
    cancel_requested: bool = False
    process: subprocess.Popen | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, event: str, data: dict):
        with self.condition:
            self.events.append({"id": len(self.events) + 1, "event": event, "data": data})
            self.condition.notify_all()


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def terminate_process_tree(process: subprocess.Popen | None):
    if not process or process.poll() is not None:
        return
    if psutil:
        try:
            root = psutil.Process(process.pid)
            children = root.children(recursive=True)
            for child in reversed(children):
                try: child.terminate()
                except psutil.Error: pass
            try: root.terminate()
            except psutil.Error: pass
            _, alive = psutil.wait_procs(children + [root], timeout=2)
            for item in alive:
                try: item.kill()
                except psutil.Error: pass
            return
        except Exception:
            pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try: os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            try: process.terminate()
            except Exception: pass


def validate_request(data: dict) -> dict:
    scope = data.get("scope", "current")
    current_case = data.get("case", "latency")
    if current_case not in TESTS:
        raise ValueError("Unknown test case")
    if scope == "current": cases = [current_case]
    elif scope == "feature": cases = [x["id"] for x in CATALOG if x["layer"] == "feature"]
    elif scope == "performance": cases = [x["id"] for x in CATALOG if x["layer"] == "performance"]
    elif scope == "all": cases = [x["id"] for x in CATALOG]
    else: raise ValueError("Invalid scope")

    req = {
        "scope": scope, "case": current_case, "cases": cases,
        "samples": int(data.get("samples", 200)), "warmup": int(data.get("warmup", 20)),
        "payload_bytes": int(data.get("payload_bytes", 256)), "duration": float(data.get("duration", 2.0)),
        "timeout": float(data.get("timeout", 2.0)), "case_timeout": float(data.get("case_timeout", 120.0)),
        "window": int(data.get("window", 32)), "interval_ms": float(data.get("interval_ms", 10.0)),
        "deadline_ms": float(data.get("deadline_ms", 10.0)), "loss_rate": float(data.get("loss_rate", 0.1)),
        "seed": int(data.get("seed", 42)), "retries": int(data.get("retries", 3)),
        "sample_interval": float(data.get("sample_interval", 0.05)), "repeat": int(data.get("repeat", 1)),
    }
    if not 1 <= req["samples"] <= 100000: raise ValueError("samples must be 1..100000")
    if not 0 <= req["warmup"] <= 100000: raise ValueError("warmup must be 0..100000")
    if not 1 <= req["payload_bytes"] <= 60000: raise ValueError("payload_bytes must be 1..60000")
    if not 0 < req["duration"] <= 3600: raise ValueError("duration must be >0 and <=3600")
    if not 0 < req["timeout"] <= 600: raise ValueError("timeout must be >0 and <=600")
    if not 0 < req["case_timeout"] <= 7200: raise ValueError("case_timeout must be >0 and <=7200")
    if not 1 <= req["window"] <= 100000: raise ValueError("window must be >=1")
    if not 0 < req["interval_ms"] <= 60000: raise ValueError("interval_ms must be >0")
    if not 0 < req["deadline_ms"] <= 600000: raise ValueError("deadline_ms must be >0")
    if not 0 <= req["loss_rate"] <= 1: raise ValueError("loss_rate must be 0..1")
    if not 0 <= req["retries"] <= 100: raise ValueError("retries must be 0..100")
    if not 0 < req["sample_interval"] <= 60: raise ValueError("sample_interval must be >0")
    if not 1 <= req["repeat"] <= 20: raise ValueError("repeat must be 1..20")
    return req


def build_command(case_id: str, output_path: Path, req: dict) -> list[str]:
    meta = TESTS[case_id]
    args = [sys.executable, str(ROOT / meta["script"])]
    common = {
        "--samples": req["samples"], "--warmup": req["warmup"], "--payload-bytes": req["payload_bytes"],
        "--duration": req["duration"], "--timeout": req["timeout"], "--case-timeout": req["case_timeout"],
        "--window": req["window"], "--interval-ms": req["interval_ms"], "--deadline-ms": req["deadline_ms"],
        "--loss-rate": req["loss_rate"], "--seed": req["seed"], "--retries": req["retries"],
        "--sample-interval": req["sample_interval"], "--output": output_path,
    }
    for key, value in common.items(): args += [key, str(value)]
    return args


def run_job(job: Job):
    req = job.request
    outdir = RESULT_ROOT / job.id
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "request.json").write_bytes(json_dumps(req))
    job.status = "running"
    total_runs = len(req["cases"]) * req["repeat"]
    job.emit("job_started", {"job_id": job.id, "created_at": job.created_at, "cases": req["cases"], "repeat": req["repeat"], "total_runs": total_runs, "output_dir": str(outdir)})
    all_results: list[dict] = []
    completed = 0

    try:
        for case_index, case_id in enumerate(req["cases"], 1):
            meta = TESTS[case_id]
            for repeat_index in range(1, req["repeat"] + 1):
                if job.cancel_requested:
                    raise InterruptedError("User cancelled the benchmark")
                job.current_case, job.current_repeat = case_id, repeat_index
                output_path = outdir / f"{case_id}.{repeat_index}.json"
                log_path = outdir / f"{case_id}.{repeat_index}.log"
                command = build_command(case_id, output_path, req)
                job.emit("case_started", {
                    "case": meta, "case_index": case_index, "case_total": len(req["cases"]),
                    "repeat": repeat_index, "repeat_total": req["repeat"], "completed_runs": completed,
                    "total_runs": total_runs, "command": subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command),
                })
                job.emit("log", {"level": "info", "line": f"第 {repeat_index}/{req['repeat']} 轮 · {meta['code']} {meta['name']} 已启动"})
                started = time.monotonic()
                popen_kwargs = {"cwd": ROOT, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8", "errors": "replace"}
                if os.name != "nt": popen_kwargs["start_new_session"] = True
                process = subprocess.Popen(command, **popen_kwargs)
                job.process = process
                captured: list[str] = []
                reader_done = threading.Event()

                def reader():
                    try:
                        assert process.stdout is not None
                        with log_path.open("w", encoding="utf-8") as log_file:
                            for line in process.stdout:
                                log_file.write(line)
                                if len(captured) < 80:
                                    captured.append(line.rstrip())
                    finally:
                        reader_done.set()

                threading.Thread(target=reader, daemon=True).start()
                last_heartbeat = -1
                while process.poll() is None:
                    if job.cancel_requested:
                        terminate_process_tree(process)
                        raise InterruptedError("User cancelled the benchmark")
                    elapsed = time.monotonic() - started
                    if elapsed > req["case_timeout"] + 8:
                        terminate_process_tree(process)
                        job.emit("log", {"level": "error", "line": f"{meta['name']} 超过 WebUI 外层超时，已终止进程树"})
                        break
                    seconds = int(elapsed)
                    if seconds != last_heartbeat and seconds % 2 == 0:
                        last_heartbeat = seconds
                        job.emit("heartbeat", {"case_id": case_id, "repeat": repeat_index, "elapsed_seconds": round(elapsed, 1)})
                    time.sleep(0.15)
                try: exit_code = process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    terminate_process_tree(process); exit_code = 1
                reader_done.wait(timeout=2)
                job.process = None
                elapsed = time.monotonic() - started
                if output_path.exists():
                    try: result = json.loads(output_path.read_text(encoding="utf-8"))
                    except Exception as exc: result = {"schema_version":"1.0","middleware":"vsoa","metric":case_id,"status":"error","measurements":{},"error":{"type":type(exc).__name__,"message":str(exc)}}
                else:
                    result = {"schema_version":"1.0","middleware":"vsoa","metric":case_id,"status":"error","measurements":{},"error":{"type":"MissingResult","message":f"No result JSON; inspect {log_path.name}"}}
                result["webui"] = {"repeat": repeat_index, "exit_code": exit_code, "wall_seconds": round(elapsed, 4), "result_file": output_path.name, "log_file": log_path.name}
                view = extract_view(case_id, result)
                all_results.append(result)
                completed += 1
                job.emit("case_result", {
                    "case_id": case_id, "case": meta, "repeat": repeat_index, "repeat_total": req["repeat"],
                    "completed_runs": completed, "total_runs": total_runs, "status": result.get("status", "error"),
                    "view": view, "result": result,
                })
                job.emit("log", {"level": "success" if result.get("status") in {"pass","partial"} else "error", "line": f"第 {repeat_index}/{req['repeat']} 轮完成：{meta['name']} → {result.get('status')}，耗时 {elapsed:.3f}s"})

        counts = {status: sum(r.get("status") == status for r in all_results) for status in ("pass","partial","fail","error","skip")}
        summary = {"schema_version":"webui-1.0","job_id":job.id,"created_at":job.created_at,"finished_at":iso_now(),"status":"completed","request":req,"counts":counts,"results":all_results}
        (outdir / "summary.json").write_bytes(json_dumps(summary))
        job.summary = summary
        job.status = "completed"
        job.emit("job_done", {"job_id":job.id,"counts":counts,"total_runs":total_runs,"output_dir":str(outdir)})
    except InterruptedError as exc:
        terminate_process_tree(job.process)
        job.process = None
        job.status = "cancelled"
        summary = {"schema_version":"webui-1.0","job_id":job.id,"created_at":job.created_at,"finished_at":iso_now(),"status":"cancelled","request":req,"results":all_results,"reason":str(exc)}
        (outdir / "summary.json").write_bytes(json_dumps(summary))
        job.summary = summary
        job.emit("job_cancelled", {"job_id":job.id,"reason":str(exc),"completed_runs":completed,"total_runs":total_runs})
    except Exception as exc:
        terminate_process_tree(job.process)
        job.process = None
        job.status = "error"
        traceback_text = traceback.format_exc()
        (outdir / "webui_error.log").write_text(traceback_text, encoding="utf-8")
        job.emit("job_error", {"job_id":job.id,"type":type(exc).__name__,"message":str(exc)})


def health_payload() -> dict:
    packages = {}
    for name in ("vsoa", "psutil"):
        try: packages[name] = importlib.metadata.version(name)
        except Exception: packages[name] = None
    available_scripts = sum((ROOT / item["script"]).is_file() for item in CATALOG)
    return {"online": True, "python": sys.version.split()[0], "packages": packages, "available_scripts": available_scripts, "total_scripts": len(CATALOG), "root": str(ROOT), "time": iso_now()}


def list_history() -> list[dict]:
    items = []
    if not RESULT_ROOT.exists(): return items
    for folder in sorted((p for p in RESULT_ROOT.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        summary_file = folder / "summary.json"
        request_file = folder / "request.json"
        summary = None
        request = None
        try:
            if summary_file.exists(): summary = json.loads(summary_file.read_text(encoding="utf-8"))
            if request_file.exists(): request = json.loads(request_file.read_text(encoding="utf-8"))
        except Exception: pass
        items.append({"job_id":folder.name,"modified":datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(timespec="seconds"),"status":(summary or {}).get("status","incomplete"),"counts":(summary or {}).get("counts"),"request":request})
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "VSOA-WebUI/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_bytes(self, data: bytes, content_type: str, status=HTTPStatus.OK, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for k, v in headers.items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, value: Any, status=HTTPStatus.OK):
        self.send_bytes(json_dumps(value), "application/json; charset=utf-8", status)

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000: raise ValueError("Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_bytes((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            rel = unquote(path[len("/static/"):])
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND); return
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self.send_bytes(target.read_bytes(), mime)
            return
        if path == "/api/health":
            self.send_json(health_payload()); return
        if path == "/api/tests":
            self.send_json({"tests": CATALOG}); return
        if path == "/api/history":
            self.send_json({"history": list_history()}); return
        if path.startswith("/api/source/"):
            case_id = unquote(path.split("/", 3)[3]) if len(path.split("/", 3)) > 3 else ""
            query = parse_qs(parsed.query)
            source = query.get("path", [None])[0]
            meta = TESTS.get(case_id)
            if not meta or not source or source not in meta.get("sources", []) or source not in ALLOWED_SOURCES:
                self.send_json({"error":"source not allowed"}, HTTPStatus.BAD_REQUEST); return
            target = (ROOT / source).resolve()
            if ROOT.resolve() not in target.parents or not target.is_file():
                self.send_json({"error":"source missing"}, HTTPStatus.NOT_FOUND); return
            self.send_json({"case_id":case_id,"path":source,"source":target.read_text(encoding="utf-8", errors="replace")}); return
        if path.startswith("/api/job/"):
            job_id = unquote(path.split("/")[-1])
            with JOBS_LOCK: job = JOBS.get(job_id)
            if job:
                self.send_json({"job_id":job.id,"status":job.status,"current_case":job.current_case,"current_repeat":job.current_repeat,"request":job.request,"summary":job.summary,"event_count":len(job.events)}); return
            summary_file = RESULT_ROOT / job_id / "summary.json"
            if summary_file.is_file(): self.send_bytes(summary_file.read_bytes(), "application/json; charset=utf-8"); return
            self.send_json({"error":"job not found"}, HTTPStatus.NOT_FOUND); return
        if path.startswith("/api/export/"):
            job_id = unquote(path.split("/")[-1])
            target = RESULT_ROOT / job_id / "summary.json"
            if not target.is_file(): self.send_json({"error":"summary not found"}, HTTPStatus.NOT_FOUND); return
            self.send_bytes(target.read_bytes(), "application/json; charset=utf-8", headers={"Content-Disposition":f'attachment; filename="vsoa-webui-{job_id}.json"'}); return
        if path.startswith("/api/events/"):
            job_id = unquote(path.split("/")[-1])
            with JOBS_LOCK: job = JOBS.get(job_id)
            if not job:
                self.send_json({"error":"job not found"}, HTTPStatus.NOT_FOUND); return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_id = 0
            try:
                header_last = self.headers.get("Last-Event-ID")
                if header_last: last_id = int(header_last)
            except Exception: last_id = 0
            try:
                while True:
                    with job.condition:
                        pending = [ev for ev in job.events if ev["id"] > last_id]
                        if not pending:
                            if job.status in {"completed","cancelled","error"}: break
                            job.condition.wait(timeout=10)
                            pending = [ev for ev in job.events if ev["id"] > last_id]
                    if not pending:
                        self.wfile.write(b": keepalive\n\n"); self.wfile.flush(); continue
                    for ev in pending:
                        payload = json.dumps(ev["data"], ensure_ascii=False, allow_nan=False)
                        data = f"id: {ev['id']}\nevent: {ev['event']}\ndata: {payload}\n\n".encode("utf-8")
                        self.wfile.write(data); self.wfile.flush(); last_id = ev["id"]
                self.wfile.write(b"event: stream_end\ndata: {}\n\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError): pass
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self.read_json_body()
            if parsed.path == "/api/run":
                req = validate_request(data)
                job = Job(id=uuid.uuid4().hex[:12], request=req)
                with JOBS_LOCK: JOBS[job.id] = job
                threading.Thread(target=run_job, args=(job,), daemon=True).start()
                self.send_json({"job_id":job.id,"request":req}, HTTPStatus.ACCEPTED); return
            if parsed.path.startswith("/api/cancel/"):
                job_id = unquote(parsed.path.split("/")[-1])
                with JOBS_LOCK: job = JOBS.get(job_id)
                if not job: self.send_json({"error":"job not found"}, HTTPStatus.NOT_FOUND); return
                job.cancel_requested = True
                terminate_process_tree(job.process)
                job.emit("log", {"level":"warn","line":"收到取消请求，正在终止当前测试及其子进程..."})
                self.send_json({"job_id":job_id,"cancel_requested":True}); return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error":str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error":f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main():
    parser = argparse.ArgumentParser(description="VSOA visual benchmark WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open browser after server starts")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"VSOA WebUI: {url}")
    print(f"Project root: {ROOT}")
    print("Press Ctrl+C to stop.")
    if args.open: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__":
    main()
