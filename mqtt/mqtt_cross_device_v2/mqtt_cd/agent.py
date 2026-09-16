"""Authenticated LAN agent with locally configured executables and bounded child lifetimes."""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import zipfile

from .common import read_json, run_id, source_hash, token, validate_case, write_json
from .processes import contain_children, spawn, stop_process

TERMINAL = {"completed", "error", "cancelled"}
MAX_BODY = 256 * 1024


class AgentError(Exception):
    def __init__(self, message, status=400, failure_origin=None):
        super().__init__(message)
        self.status = status
        self.failure_origin = failure_origin or ("dependency" if status == 503 else "configuration")


class RunFailure(Exception):
    def __init__(self, message, origin, stage=None):
        super().__init__(message)
        self.origin = origin
        self.stage = stage


def run_stage(run, now_ns=None):
    if run.arm_start is None:
        return "ready" if run.state == "ready" else "preparing"
    now = time.perf_counter_ns() if now_ns is None else now_ns
    case = run.spec["case"]
    warmup = run.arm_start - round((case["warmup_s"] + case["settle_s"]) * 1e9)
    settle = run.arm_start - round(case["settle_s"] * 1e9)
    end = run.arm_start + round(case["duration_s"] * 1e9)
    cutoff = end + round(case["drain_s"] * 1e9)
    if now < warmup:
        return "armed"
    if now < settle:
        return "warmup"
    if now < run.arm_start:
        return "settle"
    if now < end:
        return "formal"
    return "drain" if now < cutoff else "diagnostic"


def runtime_origin(stage):
    return "dut" if stage in ("formal", "drain") else "configuration"


def children_reaped(run):
    return all(process.poll() is not None for _, process in run.processes)


def _host(value):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise AgentError("broker_host must be a hostname or IP address")
    return value


def _secret_free(value):
    secret = os.environ.get("MQTT_CD_TOKEN", "")
    message = str(value)
    return message.replace(secret, "<redacted>") if secret else message


def dependencies():
    result = {}
    for package, module in (("paho-mqtt", "paho.mqtt.client"), ("psutil", "psutil")):
        try:
            found = importlib.util.find_spec(module) is not None
            version = importlib.metadata.version(package) if found else None
        except (ModuleNotFoundError, importlib.metadata.PackageNotFoundError):
            found, version = False, None
        result[package] = dict(available=found, version=version)
    return result


def _password_tool(broker, configured=None):
    if configured:
        return Path(configured).resolve()
    suffix = ".exe" if os.name == "nt" else ""
    return broker.with_name("mosquitto_passwd" + suffix) if broker else None


def mosquitto_password_hash(password, salt=None, iterations=1000):
    # Mosquitto libcommon/password_common.c: legacy-compatible $7$ PBKDF2-SHA512 format.
    salt = secrets.token_bytes(12) if salt is None else salt
    if len(salt) != 12 or iterations < 1:
        raise ValueError("Mosquitto fallback needs a 12-byte salt and positive iterations")
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations, dklen=64)
    return "$7$" + str(iterations) + "$" + base64.b64encode(salt).decode("ascii") + "$" + base64.b64encode(digest).decode("ascii")


@dataclass
class Run:
    run_id: str
    folder: Path
    spec: dict
    state: str = "preparing"
    errors: list = field(default_factory=list)
    workers: list = field(default_factory=list)
    processes: list = field(default_factory=list)
    streams: list = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    last_contact: float = field(default_factory=time.monotonic)
    arm_start: int | None = None
    thread: threading.Thread | None = None
    cancel_reason: str | None = None
    broker_version: str | None = None
    stage: str = "preparing"
    failure_origin: str | None = None
    failed_at_ns: int | None = None
    resource_incomplete_samples: int = 0


class NodeService:
    def __init__(self, node_id, data_dir, broker_executable=None, broker_bind="0.0.0.0",
                 broker_password_executable=None, lease_seconds=30):
        if node_id not in ("A", "B"):
            raise ValueError("node_id must be A or B")
        self.node_id = node_id
        self.source_sha256 = source_hash()
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.broker = Path(broker_executable).resolve() if broker_executable else None
        self.password_tool = _password_tool(self.broker, broker_password_executable)
        self.broker_bind = _host(broker_bind)
        self.lease_seconds = lease_seconds
        self.lock = threading.RLock()
        self.current = None
        self.shutdown_event = threading.Event()

    def health(self):
        caps = dict(managed_broker=bool(self.broker and self.broker.is_file()),
                    password_tool=bool(self.password_tool and self.password_tool.is_file()),
                    password_hash_fallback="mosquitto_sha512_pbkdf2",
                    mqtt_protocol="3.1.1", roles=["sender", "receiver"],
                    auth="shared_bearer_token", timestamp="perf_counter_ns", envelope_bytes=64,
                    single_active_run=True, lease_seconds=self.lease_seconds)
        return dict(node_id=self.node_id, hostname=socket.gethostname(), pid=os.getpid(),
                    version="2.0", source_sha256=self.source_sha256, capabilities=caps, dependencies=dependencies())

    def touch(self):
        with self.lock:
            if self.current and not self.current.done.is_set():
                self.current.last_contact = time.monotonic()

    def prepare(self, body):
        try:
            identifier = run_id(body["run_id"])
            role = body["role"]
            case = validate_case(body["case"])
            host = _host(body["broker_host"])
            port = body["broker_port"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError(_secret_free(exc)) from exc
        if role not in ("sender", "receiver"):
            raise AgentError("role must be sender or receiver")
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise AgentError("broker_port must be an integer from 1024 to 65535")
        if not isinstance(body.get("case"), dict):
            raise AgentError("case must be an object")
        # Reject credentials rather than serializing accidental secrets into downloadable specs.
        if any(any(word in str(key).lower() for word in ("token", "password", "secret")) for key in case):
            raise AgentError("case must not contain credentials")
        if role == "sender" and (not self.broker or not self.broker.is_file()):
            raise AgentError("Sender requires local --broker-executable pointing to Mosquitto", 503)
        missing = [name for name, info in dependencies().items() if not info["available"]]
        if missing:
            raise AgentError("Missing Python dependencies: " + ", ".join(missing), 503)
        with self.lock:
            if self.shutdown_event.is_set():
                raise AgentError("Agent is shutting down", 503)
            if self.current and (not self.current.done.is_set() or not children_reaped(self.current)):
                raise AgentError("Another run is active; cancel/release it first", 409)
            folder = self.data_dir / identifier
            if folder.exists():
                raise AgentError("run_id already exists; use a new UUID", 409)
            spec = dict(run_id=identifier, role=role, case=case, broker_host=host,
                        broker_port=port, node_id=self.node_id)
            folder.mkdir()
            write_json(folder / "spec.json", spec)
            run = Run(identifier, folder, spec)
            self.current = run
            self._node_info(run)
            run.thread = threading.Thread(target=self._execute, args=(run,), name="mqtt-cd-run", daemon=True)
            run.thread.start()
            return self._status(run)

    def _node_info(self, run):
        import psutil
        health = self.health()
        data = dict(node_id=self.node_id, hostname=socket.gethostname(), os=platform.platform(),
                    python=platform.python_version(), logical_cpu_count=psutil.cpu_count(),
                    total_memory_bytes=psutil.virtual_memory().total, role=run.spec["role"],
                    broker_version=run.broker_version, capabilities=health["capabilities"],
                    source_sha256=health["source_sha256"], dependencies=health["dependencies"],
                    errors=run.errors, process_cpu_convention="one fully occupied logical CPU is 100 percent",
                    resources_include_agent=False, resources_include_broker=run.spec["role"] == "sender",
                    stage=run.stage, failure_origin=run.failure_origin, failed_at_ns=run.failed_at_ns,
                    resource_incomplete_samples=run.resource_incomplete_samples)
        write_json(run.folder / "node.json", data)

    def _lookup(self, identifier):
        try:
            identifier = run_id(identifier)
        except (ValueError, AttributeError) as exc:
            raise AgentError("Invalid run UUID", 400) from exc
        with self.lock:
            if self.current and self.current.run_id == identifier:
                return self.current
        raise AgentError("Unknown current run", 404)

    def _status(self, run):
        return dict(run_id=run.run_id, state=run.state, errors=list(run.errors),
                    stage=run.stage, failure_origin=run.failure_origin, failed_at_ns=run.failed_at_ns,
                    resource_incomplete_samples=run.resource_incomplete_samples,
                    workers=[dict(role=role, index=index, pid=process.pid, exit_code=process.poll(),
                                  ready=(run.folder / f"worker-{role}-{index}.ready.json").exists())
                             for role, index, process in run.workers],
                    cancel_requested=run.stop.is_set(), archived=run.done.is_set() and children_reaped(run))

    def status(self, identifier):
        with self.lock:
            return self._status(self._lookup(identifier))

    def arm(self, identifier, body):
        with self.lock:
            run = self._lookup(identifier)
            if run.state != "ready":
                raise AgentError("Run must be ready before arm", 409)
            start_ns = body.get("start_ns")
            if isinstance(start_ns, bool) or not isinstance(start_ns, int):
                raise AgentError("start_ns must be an integer local monotonic timestamp")
            prep_s = run.spec["case"]["warmup_s"] + run.spec["case"]["settle_s"]
            warmup_delay = (start_ns - time.perf_counter_ns()) / 1e9 - prep_s
            if not .05 <= warmup_delay <= 120:
                raise AgentError("Arm must allow warmup + settle plus 0.05..120 seconds lead time")
            write_json(run.folder / "arm.json", dict(start_ns=start_ns))
            run.arm_start = start_ns
            run.state = "armed"
            run.stage = "armed"
            return self._status(run)

    def cancel(self, identifier, reason="Controller cancelled"):
        run = self._lookup(identifier)
        with self.lock:
            if not run.done.is_set():
                run.cancel_reason = reason
                if run.failure_origin is None:
                    run.failure_origin = "controller"
                    run.stage = run_stage(run)
                    run.failed_at_ns = time.perf_counter_ns()
                (run.folder / "cancel").touch()
                run.stop.set()
        run.done.wait(10)
        with self.lock:
            return self._status(run)

    def release(self, identifier):
        run = self._lookup(identifier)
        if not run.done.is_set():
            return self.cancel(identifier, "Controller released active run")
        return self._status(run)

    def artifact(self, identifier):
        run = self._lookup(identifier)
        if not run.done.is_set() or run.state not in TERMINAL or not children_reaped(run):
            raise AgentError("Artifacts are available only after all workers are reaped", 409)
        destination = self.data_dir / f"{run.run_id}.zip"
        # The archive is built after cleanup; private broker configuration is never exported.
        with self.lock:
            if not destination.exists():
                with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(run.folder.rglob("*")):
                        relative = path.relative_to(run.folder)
                        if path.is_file() and not path.is_symlink() and ".private" not in relative.parts:
                            archive.write(path, relative.as_posix())
            return destination

    def _start_broker(self, run):
        private = run.folder / ".private"
        private.mkdir()
        if os.name != "nt":
            private.chmod(0o700)
        password_file = private / "passwords"
        if self.password_tool and self.password_tool.is_file():
            command = [str(self.password_tool), "-b", "-c", str(password_file), "benchmark", token()]
            result = subprocess.run(command, capture_output=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                raise RuntimeError("mosquitto_passwd could not create the local hashed password file")
        else:
            password_file.write_text("benchmark:" + mosquitto_password_hash(token()) + "\n", encoding="ascii")
        try:
            version = subprocess.run([str(self.broker), "-h"], capture_output=True, timeout=5,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            text = (version.stdout + version.stderr).decode("utf-8", errors="replace")
            run.broker_version = text.splitlines()[0][:200] if text else "unknown"
        except (OSError, subprocess.SubprocessError):
            run.broker_version = "unknown"
        case = run.spec["case"]
        config = "\n".join((f"listener {run.spec['broker_port']} {self.broker_bind}",
                            "allow_anonymous false", f"password_file {password_file.as_posix()}",
                            "persistence false", "retain_available false",
                            f"max_inflight_messages {case['queue_messages']}",
                            f"max_queued_messages {case['queue_messages']}",
                            f"max_queued_bytes {case['queue_bytes']}",
                            "log_type error", "log_type warning", "connection_messages true", ""))
        config_path = private / "mosquitto.conf"
        config_path.write_text(config, encoding="utf-8")
        log = (run.folder / "broker.log").open("wb")
        run.streams.append(log)
        broker = spawn([str(self.broker), "-c", str(config_path)], stdout=log, stderr=subprocess.STDOUT)
        run.processes.append(("infrastructure", broker))
        until = time.monotonic() + case["connect_timeout_s"]
        probe_host = "127.0.0.1" if self.broker_bind == "0.0.0.0" else ("::1" if self.broker_bind == "::" else self.broker_bind)
        while time.monotonic() < until and not run.stop.is_set():
            if time.monotonic() - run.last_contact > self.lease_seconds:
                run.cancel_reason = "Controller lease expired during Broker startup"
                run.failure_origin = "controller"
                run.failed_at_ns = time.perf_counter_ns()
                run.stop.set()
                break
            if broker.poll() is not None:
                raise RuntimeError("Managed Mosquitto exited during startup; see broker.log")
            try:
                with socket.create_connection((probe_host, run.spec["broker_port"]), timeout=.25):
                    return
            except OSError:
                run.stop.wait(.05)
        raise TimeoutError("Managed Mosquitto did not open the configured port")

    def _execute(self, run):
        import psutil
        resources = None
        terminal_state = "error"
        monitored = {}
        try:
            if run.spec["role"] == "sender":
                self._start_broker(run)
            if run.stop.is_set():
                raise RuntimeError(run.cancel_reason or "Cancelled during preparation")
            role = "publisher" if run.spec["role"] == "sender" else "subscriber"
            count = run.spec["case"]["publishers" if role == "publisher" else "subscribers"]
            package_root = Path(__file__).resolve().parent.parent
            for index in range(count):
                log = (run.folder / f"worker-{role}-{index}.log").open("wb")
                run.streams.append(log)
                process = spawn([sys.executable, "-m", "mqtt_cd.worker", "--spec", str(run.folder / "spec.json"),
                                 "--role", role, "--index", str(index)], cwd=package_root,
                                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                run.workers.append((role, index, process))
                run.processes.append((role, process))
            for proc_role, process in run.processes:
                handle = psutil.Process(process.pid)
                handle.cpu_percent(None)
                monitored[process.pid] = (proc_role, handle)
            psutil.cpu_percent(None)
            psutil.cpu_percent(None, percpu=True)
            resources = (run.folder / "resources.jsonl").open("w", encoding="utf-8", buffering=1)
            prepare_deadline = time.monotonic() + run.spec["case"]["connect_timeout_s"] + 5
            next_sample = 0
            total_memory = psutil.virtual_memory().total
            while not run.stop.is_set():
                now = time.monotonic()
                run.stage = run_stage(run)
                if now - run.last_contact > self.lease_seconds:
                    run.cancel_reason = "Controller lease expired"
                    run.failure_origin = "controller"
                    run.failed_at_ns = time.perf_counter_ns()
                    run.stop.set()
                    break
                all_exited = all(process.poll() is not None for _, _, process in run.workers)
                if all_exited:
                    failures = []
                    failure_origin = None
                    failure_stage = None
                    for worker_role, index, process in run.workers:
                        summary_path = run.folder / f"worker-{worker_role}-{index}.summary.json"
                        summary = read_json(summary_path) if summary_path.exists() else {}
                        if process.returncode or summary.get("status") != "completed":
                            failures.append(f"{worker_role}-{index}: " + "; ".join(summary.get("errors") or ["worker exited without completed summary"]))
                            failure_origin = failure_origin or summary.get("failure_origin")
                            failure_stage = failure_stage or summary.get("stage")
                    if failures:
                        raise RunFailure(" | ".join(failures), failure_origin or runtime_origin(run.stage), failure_stage)
                    terminal_state = "completed"
                    break
                if any(process.poll() is not None for _, _, process in run.workers):
                    # Normal endpoints may finish a few milliseconds apart at their common deadline.
                    if run.arm_start is None or time.perf_counter_ns() < run.arm_start + round(run.spec["case"]["duration_s"] * 1e9):
                        failed = next((read_json(run.folder / f"worker-{r}-{i}.summary.json")
                                       for r, i, p in run.workers if p.poll() is not None
                                       and (run.folder / f"worker-{r}-{i}.summary.json").exists()), {})
                        raise RunFailure("An endpoint exited before the formal measurement finished",
                                         failed.get("failure_origin") or runtime_origin(run.stage), failed.get("stage"))
                if run.state == "preparing":
                    if all((run.folder / f"worker-{r}-{i}.ready.json").exists() for r, i, _ in run.workers):
                        with self.lock:
                            run.state = "ready"
                            run.stage = "ready"
                    elif now > prepare_deadline:
                        raise TimeoutError("Endpoint readiness timed out")
                if run.arm_start is not None:
                    if time.perf_counter_ns() >= run.arm_start:
                        with self.lock:
                            run.state = "running"
                    deadline = run.arm_start + round((run.spec["case"]["duration_s"] + run.spec["case"]["drain_s"] + 3) * 1e9)
                    if time.perf_counter_ns() > deadline:
                        raise RunFailure("Endpoint exceeded its measurement and cleanup deadline", "cleanup")
                for proc_role, process in run.processes:
                    if proc_role == "infrastructure" and process.poll() is not None:
                        raise RunFailure("Managed Broker exited before endpoint completion", runtime_origin(run.stage))
                if now >= next_sample:
                    roles = {name: dict(cpu_percent=0.0, memory_mb=0.0) for name in ("publisher", "subscriber", "infrastructure")}
                    incomplete = False
                    for proc_role, handle in monitored.values():
                        try:
                            roles[proc_role]["cpu_percent"] += handle.cpu_percent(None)
                            roles[proc_role]["memory_mb"] += handle.memory_info().rss / 1e6
                        except psutil.NoSuchProcess as exc:
                            if run.stage == "diagnostic":
                                incomplete = True
                            else:
                                raise RunFailure("Resource sampler detected an endpoint or Broker exit",
                                                 runtime_origin(run.stage)) from exc
                        except psutil.Error as exc:
                            raise RunFailure("Resource sampling failed: " + _secret_free(exc), "measurement") from exc
                    if incomplete:
                        # A finished diagnostic endpoint contributes missing data, never a false zero.
                        run.resource_incomplete_samples += 1
                        next_sample = now + run.spec["case"]["sample_interval_s"]
                        run.stop.wait(.025)
                        continue
                    rss = sum(value["memory_mb"] for value in roles.values())
                    resources.write(json.dumps(dict(time_ns=time.perf_counter_ns(), roles=roles,
                        host_cpu_percent=psutil.cpu_percent(None), host_cpu_per_core_percent=psutil.cpu_percent(None, percpu=True),
                        logical_cpu_count=psutil.cpu_count(), rss_total_mb=rss)) + "\n")
                    if rss * 1e6 > total_memory * run.spec["case"]["memory_limit_fraction"]:
                        raise RunFailure("Measured process RSS exceeded configured host memory fraction", runtime_origin(run.stage))
                    next_sample = now + run.spec["case"]["sample_interval_s"]
                run.stop.wait(.025)
            if run.stop.is_set():
                terminal_state = "cancelled"
                run.failure_origin = run.failure_origin or "controller"
                run.failed_at_ns = run.failed_at_ns or time.perf_counter_ns()
                if run.cancel_reason:
                    run.errors.append(run.cancel_reason)
        except Exception as exc:
            terminal_state = "cancelled" if run.stop.is_set() else "error"
            if isinstance(exc, RunFailure):
                run.failure_origin = run.failure_origin or exc.origin
                run.stage = exc.stage or run.stage
            elif run.failure_origin is None:
                run.failure_origin = "measurement" if isinstance(exc, (OSError, psutil.Error)) else runtime_origin(run.stage)
            run.failed_at_ns = run.failed_at_ns or time.perf_counter_ns()
            run.errors.append(_secret_free(exc))
        finally:
            if terminal_state != "completed":
                (run.folder / "cancel").touch()
            # Allow workers to persist partial summaries, then terminate owned processes if necessary.
            grace = time.monotonic() + 2
            while any(p.poll() is None for _, _, p in run.workers) and time.monotonic() < grace:
                time.sleep(.025)
            for _, process in reversed(run.processes):
                try:
                    stop_process(process)
                except (OSError, subprocess.SubprocessError) as exc:
                    run.errors.append("Process cleanup: " + _secret_free(exc))
                    terminal_state = "error"
                    run.failure_origin = run.failure_origin or "cleanup"
                    run.failed_at_ns = run.failed_at_ns or time.perf_counter_ns()
            if resources:
                resources.close()
            for stream in run.streams:
                stream.close()
            private = run.folder / ".private"
            if private.exists():
                try:
                    shutil.rmtree(private)
                except OSError:
                    run.errors.append("Private broker configuration cleanup failed; excluded from archives")
            with self.lock:
                run.state = terminal_state
                if terminal_state == "completed":
                    run.stage = "completed"
                self._node_info(run)
                write_json(run.folder / "agent_status.json", dict(self._status(run), archived=children_reaped(run)))
                run.done.set()

    def shutdown(self):
        self.shutdown_event.set()
        with self.lock:
            current = self.current
        if current and not current.done.is_set():
            self.cancel(current.run_id, "Agent shutting down")
            if current.thread:
                current.thread.join(timeout=15)


def handler_class(service, secret):
    secret_bytes = secret.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "MQTT-CD/2"
        protocol_version = "HTTP/1.0"

        def log_message(self, format, *args):
            # Do not print paths, headers, or request bodies containing credentials.
            return

        def _json(self, status, value):
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _auth(self):
            supplied = self.headers.get("Authorization", "")
            if not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:].encode("utf-8"), secret_bytes):
                raise AgentError("Unauthorized", 401)
            service.touch()

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise AgentError("Invalid Content-Length") from exc
            if not 0 <= length <= MAX_BODY:
                raise AgentError("Request body too large", 413)
            try:
                value = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, UnicodeDecodeError) as exc:
                raise AgentError("Invalid JSON body") from exc
            if not isinstance(value, dict):
                raise AgentError("JSON body must be an object")
            return value

        def _dispatch(self, method):
            try:
                receive_ns = time.perf_counter_ns()
                self.connection.settimeout(15)
                self._auth()
                path = urlsplit(self.path).path
                if method == "GET" and path == "/health":
                    value = service.health()
                elif method == "POST" and path == "/clock":
                    self._body()
                    value = dict(node_id=service.node_id, receive_ns=receive_ns, send_ns=time.perf_counter_ns())
                elif method == "POST" and path == "/prepare":
                    value = service.prepare(self._body())
                else:
                    parts = path.split("/")
                    if len(parts) != 3:
                        raise AgentError("Unknown endpoint", 404)
                    _, action, identifier = parts
                    if method == "GET" and action == "status":
                        value = service.status(identifier)
                    elif method == "GET" and action == "artifact":
                        archive_path = service.artifact(identifier)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Length", str(archive_path.stat().st_size))
                        self.end_headers()
                        with archive_path.open("rb") as stream:
                            shutil.copyfileobj(stream, self.wfile, 1024 * 1024)
                        return
                    elif method == "POST" and action == "arm":
                        value = service.arm(identifier, self._body())
                    elif method == "POST" and action in ("cancel", "release"):
                        self._body()
                        value = getattr(service, action)(identifier)
                    else:
                        raise AgentError("Unknown endpoint", 404)
                self._json(200, value)
            except AgentError as exc:
                self._json(exc.status, dict(error=_secret_free(exc), stage="preparing", failure_origin=exc.failure_origin))
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                return
            except Exception as exc:
                self._json(500, dict(error=_secret_free(exc)))

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve_agent(args):
    secret = token()
    contain_children()
    service = NodeService(args.node_id, args.data_dir, getattr(args, "broker_executable", None),
                          getattr(args, "broker_bind", "0.0.0.0"), getattr(args, "broker_password_executable", None))
    server = ThreadingHTTPServer((args.host, args.port), handler_class(service, secret))
    server.daemon_threads = True
    print(f"MQTT CD agent {args.node_id} listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.shutdown()
    return 0
