"""Shared CLI, JSON reporting, process isolation and measurement helpers."""

import argparse
import contextlib
import importlib.metadata
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import psutil
import vsoa

ROOT = Path(__file__).resolve().parents[1]
SPAWN_LOCK = threading.Lock()
CANCELLED = threading.Event()


def distribution(values, unit="ms"):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "unit": unit, "min": None, "mean": None,
                "p50": None, "p95": None, "p99": None, "max": None, "stddev": None}

    def quantile(fraction):
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {"count": len(ordered), "unit": unit, "min": ordered[0],
            "mean": statistics.fmean(ordered), "p50": quantile(0.5),
            "p95": quantile(0.95), "p99": quantile(0.99), "max": ordered[-1],
            "stddev": statistics.pstdev(ordered)}


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and > 0")
    return number


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def arguments(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--samples", type=positive_int, default=200)
    parser.add_argument("--warmup", type=nonnegative_int, default=20)
    parser.add_argument("--payload-bytes", type=positive_int, default=256)
    parser.add_argument("--duration", type=positive, default=2.0)
    parser.add_argument("--timeout", type=positive, default=2.0)
    parser.add_argument("--case-timeout", type=positive, default=120.0)
    parser.add_argument("--window", type=positive_int, default=32)
    parser.add_argument("--interval-ms", type=positive, default=10.0)
    parser.add_argument("--deadline-ms", type=positive, default=10.0)
    parser.add_argument("--loss-rate", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--retries", type=nonnegative_int, default=3)
    parser.add_argument("--sample-interval", type=positive, default=0.05)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    if options.payload_bytes > 60000:
        parser.error("payload-bytes must be <= 60000, leaving room for UDP headers and parameters")
    if not 0 <= options.loss_rate <= 1:
        parser.error("loss-rate must be between 0 and 1")
    return options


def metric(value, unit):
    return {"value": value, "unit": unit}


def entry(metric_id, layer, description, callback):
    options = arguments(description)
    deadline_done = threading.Event()
    report_lock = threading.Lock()
    reported = False
    CANCELLED.clear()

    def watchdog():
        if not deadline_done.wait(options.case_timeout):
            with SPAWN_LOCK:
                CANCELLED.set()
                children = psutil.Process().children(recursive=True)
                for child in reversed(children):
                    with contextlib.suppress(psutil.Error):
                        child.kill()
                psutil.wait_procs(children, timeout=3)
            emit("error", {}, ["Whole-case timeout exceeded"], {"type": "TimeoutError"})
            os._exit(1)

    def write_report(status, measurements, limitations, error=None):
        result = {"schema_version": "1.0", "middleware": "vsoa", "metric": metric_id,
                  "layer": layer, "status": status,
                  "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                  "environment": {"os": platform.platform(), "python": platform.python_version(),
                                  "vsoa": importlib.metadata.version("vsoa"),
                                  "psutil": importlib.metadata.version("psutil"),
                                  "logical_cpus": psutil.cpu_count(), "machine": platform.machine()},
                  "configuration": {key: str(value) if isinstance(value, Path) else value
                                    for key, value in vars(options).items()},
                  "topology": "IPv4 loopback; isolated server process; single client process",
                  "measurements": measurements, "limitations": limitations, "error": error}
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if options.output:
            options.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = options.output.with_suffix(options.output.suffix + ".tmp")
            temporary.write_text(text + "\n", encoding="utf-8")
            temporary.replace(options.output)
        print(text, flush=True)

    def emit(status, measurements, limitations, error=None):
        nonlocal reported
        with report_lock:
            if reported:
                return
            if CANCELLED.is_set():
                status, measurements = "error", {}
                limitations, error = ["Whole-case timeout exceeded"], {"type": "TimeoutError"}
            write_report(status, measurements, limitations, error)
            reported = True

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        status, measurements, limitations = callback(options)
        exit_code = 1 if status in {"fail", "error"} else 0
        emit(status, measurements, limitations)
        if CANCELLED.is_set():
            exit_code = 1
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        emit("error", {}, ["Test did not complete; no successful measurement inferred"],
             {"type": type(error).__name__, "message": str(error)})
        exit_code = 1
    finally:
        deadline_done.set()
    return exit_code


def free_port():
    for attempt in range(50):
        with socket.socket() as tcp_socket:
            tcp_socket.bind(("127.0.0.1", 0))
            port = tcp_socket.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
                try:
                    udp_socket.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
    raise RuntimeError("Unable to reserve a matching TCP/UDP port")


class Worker:
    def __init__(self, kind="server", target=None):
        self.port = free_port()
        self.kind = kind
        self.temporary = tempfile.TemporaryDirectory(prefix="vsoa-bench-")
        self.ready = Path(self.temporary.name) / "ready.json"
        self.log = open(Path(self.temporary.name) / "worker.log", "w+", encoding="utf-8")
        command = [sys.executable, "-m", "bench.worker", kind, "--port", str(self.port)]
        if target is not None:
            command += ["--target", str(target)]
        else:
            command += ["--ready", str(self.ready)]
        self.started_ns = time.perf_counter_ns()
        with SPAWN_LOCK:
            if CANCELLED.is_set():
                self.log.close()
                self.temporary.cleanup()
                raise TimeoutError("Case cancelled before worker launch")
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=self.log, stderr=self.log)

    def check(self):
        if self.process.poll() is not None:
            self.log.seek(0)
            raise RuntimeError("VSOA worker exited: " + self.log.read())

    def wait_ready(self, timeout=10):
        deadline = time.monotonic() + timeout
        while not self.ready.exists():
            self.check()
            if time.monotonic() >= deadline:
                raise TimeoutError("VSOA server did not become ready")
            time.sleep(0.005)
        return (time.perf_counter_ns() - self.started_ns) / 1e6

    def close(self):
        descendants = []
        with contextlib.suppress(psutil.Error):
            descendants = psutil.Process(self.process.pid).children(recursive=True)
        for child in reversed(descendants):
            with contextlib.suppress(psutil.Error):
                child.terminate()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        gone, alive = psutil.wait_procs(descendants, timeout=3)
        for child in alive:
            with contextlib.suppress(psutil.Error):
                child.kill()
        psutil.wait_procs(alive, timeout=3)
        self.log.close()
        for attempt in range(20):
            try:
                self.temporary.cleanup()
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()


class Client:
    def __init__(self, port, timeout=2):
        self.native = vsoa.Client()
        self.timeout = timeout
        self.thread = None
        started = time.perf_counter_ns()
        result = self.native.connect(f"vsoa://127.0.0.1:{port}", timeout=timeout)
        self.connect_ms = (time.perf_counter_ns() - started) / 1e6
        if result != vsoa.Client.CONNECT_OK:
            self.native.close()
            raise ConnectionError(f"VSOA connect returned {result}")
        self.thread = threading.Thread(target=self.native.run, daemon=True)
        self.thread.start()

    def rpc(self, path="/echo", params=None, data=None, timeout=None):
        received = threading.Event()
        response = []

        def callback(client, header, payload):
            response.append((header, payload, time.perf_counter_ns()))
            received.set()

        limit = self.timeout if timeout is None else timeout
        started = time.perf_counter_ns()
        sent = self.native.call(path, payload={"param": params or {}, "data": data},
                                callback=callback, timeout=limit)
        if not sent or not received.wait(limit + 0.25):
            raise TimeoutError(f"RPC {path} did not complete within {limit}s")
        header, payload, finished = response[0]
        if header is None:
            raise TimeoutError(f"RPC {path} timed out")
        if header.status != 0:
            raise RuntimeError(f"RPC {path} returned status {header.status}")
        return header, payload, started, finished

    def close(self):
        self.native.close()
        if self.thread:
            self.thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()


@contextlib.contextmanager
def connected(options):
    with Worker() as server:
        startup_ms = server.wait_ready()
        with Client(server.port, options.timeout) as client:
            yield server, client, startup_ms


def echo_samples(client, options, paced=False):
    payload = bytes((index % 251 for index in range(options.payload_bytes)))
    for sequence in range(options.warmup):
        client.rpc(params={"sequence": -sequence - 1}, data=payload)
    rows = []
    period = options.interval_ms / 1000
    scheduled = time.perf_counter()
    for sequence in range(options.samples):
        if paced:
            delay = scheduled - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        schedule_ns = int(scheduled * 1e9) if paced else None
        try:
            header, reply, started, finished = client.rpc(params={"sequence": sequence}, data=payload)
            valid = reply.param["sequence"] == sequence and bytes(reply.data or b"") == payload
            rows.append({"sequence": sequence, "ok": valid, "send_ns": started,
                         "receive_ns": finished, "server_receive_ns": reply.param["server_receive_ns"],
                         "scheduled_ns": schedule_ns, "rtt_ms": (finished - started) / 1e6})
        except (TimeoutError, RuntimeError) as error:
            rows.append({"sequence": sequence, "ok": False, "error": str(error)})
        if paced:
            scheduled += period
    return rows


def sample_summary(rows):
    successful = [row for row in rows if row["ok"]]
    return {"attempted": len(rows), "successful": len(successful),
            "failed": len(rows) - len(successful),
            "success_rate": metric(len(successful) / len(rows) if rows else 0, "ratio"),
            "rtt": distribution([row["rtt_ms"] for row in successful])}


def position_lookup(server, options):
    with Worker("position", target=server.port) as position:
        vsoa.pos("127.0.0.1", position.port)
        deadline = time.monotonic() + 10
        transient_errors = []
        while True:
            position.check()
            try:
                found = vsoa.lookup("benchmark-service")
            except ConnectionResetError as error:
                transient_errors.append(str(error))
                found = None
            if found == ("127.0.0.1", server.port):
                ready_ms = (time.perf_counter_ns() - position.started_ns) / 1e6
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Position server did not resolve registered service")
            time.sleep(0.01)
        timings = []
        for sequence in range(options.samples):
            started = time.perf_counter_ns()
            if vsoa.lookup("benchmark-service") != ("127.0.0.1", server.port):
                raise RuntimeError("Position returned an unexpected endpoint")
            timings.append((time.perf_counter_ns() - started) / 1e6)
        missing = vsoa.lookup("unregistered-service")
        return {"mechanism": "explicit VSOA Position UDP name lookup; static registry callback",
                "resolved_endpoint": ["127.0.0.1", server.port],
                "unknown_service_returns_none": missing is None,
                "startup_transient_errors": transient_errors,
                "position_spawn_to_first_resolution": metric(ready_ms, "ms"),
                "warm_lookup": distribution(timings)}
