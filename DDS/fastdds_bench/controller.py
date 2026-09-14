from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

import psutil

from . import (
    ADAPTER_VERSION,
    METRICS_DEFINITION_VERSION,
    MIDDLEWARE_ID,
    SCHEMA_VERSION,
    VENDOR,
)
from .config import effective_condition, load_config, select_conditions
from .metrics import STANDARD_METRIC_FIELDS, analyze_run, subscriber_latency_samples
from .payload import crc32_text, make_payload_text, read_payload_text
from .resource_monitor import ResourceMonitor
from .results import (
    map_engine_run,
    sample_document,
    suite_document,
)
from .transport import PROFILE_NAME, render_transport_xml, transport_detail
from .winjob import create_job
from .util import (
    PROJECT_ROOT,
    atomic_write_json,
    read_json,
    safe_id,
    sha256_file,
    utc_now_iso,
)


class RunFailure(RuntimeError):
    pass


class ProcessRegistry:
    """本轮端点进程的线程安全管理器。

    故障重启会主动结束并重新拉起某个端点，因此"进程列表"必须能安全地被
    故障线程修改：被主动终止的端点必须先取消登记，否则会被误判为崩溃。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: list[subprocess.Popen[Any]] = []

    def register(self, process: subprocess.Popen[Any]) -> subprocess.Popen[Any]:
        with self._lock:
            self._processes.append(process)
        return process

    def unregister(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            if process in self._processes:
                self._processes.remove(process)

    def snapshot(self) -> list[subprocess.Popen[Any]]:
        with self._lock:
            return list(self._processes)

    def pids(self) -> list[int]:
        return [process.pid for process in self.snapshot()]

    def failure(self) -> str | None:
        for process in self.snapshot():
            code = process.poll()
            if code is not None and code != 0:
                return f"Endpoint pid={process.pid} exited with code {code}"
        return None

    def wait_all(self, timeout: float) -> None:
        deadline = time.perf_counter() + timeout
        for process in self.snapshot():
            remaining = max(0.01, deadline - time.perf_counter())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise RunFailure(f"Endpoint pid={process.pid} did not stop") from exc

    def terminate_all(self) -> None:
        live = [process for process in self.snapshot() if process.poll() is None]
        for process in live:
            try:
                process.terminate()
            except OSError:
                pass
        deadline = time.perf_counter() + 5.0
        for process in live:
            remaining = max(0.0, deadline - time.perf_counter())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass


def collect_environment(config: dict[str, Any]) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    package_versions: dict[str, str | None] = {}
    for distribution in ("PyYAML", "psutil", "jsonschema", "pywin32"):
        try:
            package_versions[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            package_versions[distribution] = None
    suite = config.get("suite", {})
    version = suite.get("module_version")
    return {
        "middleware_id": MIDDLEWARE_ID,
        "middleware_label": "DDS",
        "vendor": suite.get("vendor", VENDOR),
        "version": version,
        "middleware_version": version,
        "fastdds_python_version": suite.get("fastdds_python_version"),
        "os": platform.platform(),
        "os_name": os.name,
        "machine": platform.machine(),
        "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER"),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "logical_cpus": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "total_memory_bytes": int(memory.total),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": sys.executable,
        "python_packages": package_versions,
        "fastdds_home": os.environ.get("FASTDDSHOME"),
        "topology": "same host, explicit UDPv4 or SHM transport, publisher/subscriber process pair",
        "topology_note": "DDS 无 Broker/Router 进程；端点进程 = 全部中间件进程",
        "clock": {
            "name": "time.perf_counter_ns",
            "monotonic": time.get_clock_info("perf_counter").monotonic,
            "resolution_seconds": time.get_clock_info("perf_counter").resolution,
            "same_host_required": True,
        },
        "configured_fastdds_version": suite.get("module_version"),
        "configured_fastdds_python_version": suite.get("fastdds_python_version"),
    }


def probe_fastdds_runtime() -> dict[str, Any]:
    try:
        import BenchmarkMessage
        import fastdds

        sample = BenchmarkMessage.BenchmarkMessage()
        sample.publisher_id(7)
        sample.sequence_number(11)
        sample.send_timestamp_ns(13)
        sample.payload_length(1)
        sample.checksum(0)
        sample.phase(1)
        sample.payload("x")
        if sample.publisher_id() != 7 or read_payload_text(sample) != "x":
            raise RuntimeError("Generated type setter/getter self-test failed")
        pubsub_type = BenchmarkMessage.BenchmarkMessagePubSubType()
        pubsub_type.set_name("BenchmarkMessage")
        return {
            "ok": True,
            "fastdds_module": str(Path(fastdds.__file__).resolve()),
            "benchmark_message_module": str(Path(BenchmarkMessage.__file__).resolve()),
            "type_name": pubsub_type.get_name(),
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "fastdds_module": None,
            "benchmark_message_module": None,
            "type_name": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _planned_seconds(condition: dict[str, Any], default_timeout: float) -> float:
    if int(condition["message_count"]) > 0 and float(condition["publish_rate_hz"]) > 0:
        return int(condition["message_count"]) / float(condition["publish_rate_hz"])
    if float(condition["duration_seconds"]) > 0:
        return float(condition["duration_seconds"])
    return default_timeout


def _status_files(run_dir: Path, condition: dict[str, Any]) -> list[Path]:
    result = [
        run_dir / "status" / f"subscriber_{i}.json"
        for i in range(int(condition["subscriber_count"]))
    ]
    result.extend(
        run_dir / "status" / f"publisher_{i}.json"
        for i in range(int(condition["publisher_count"]))
    )
    return result


def _read_statuses(paths: list[Path]) -> list[dict[str, Any]] | None:
    statuses: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            return None
        try:
            statuses.append(read_json(path))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
    return statuses


def _wait_status(
    paths: list[Path],
    registry: ProcessRegistry,
    predicate: Callable[[list[dict[str, Any]]], bool],
    timeout_seconds: float,
    description: str,
) -> list[dict[str, Any]]:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        failure = registry.failure()
        if failure:
            raise RunFailure(f"{description}: {failure}")
        statuses = _read_statuses(paths)
        if statuses:
            errors = [s.get("error") for s in statuses if s.get("error")]
            if errors:
                raise RunFailure(f"{description}: endpoint error: {errors[0]}")
            if predicate(statuses):
                return statuses
        time.sleep(0.01)
    raise RunFailure(f"Timed out while waiting for {description}")


def _wait_until_with_process_checks(target_ns: int, registry: ProcessRegistry) -> None:
    while True:
        failure = registry.failure()
        if failure:
            raise RunFailure(failure)
        remaining = target_ns - time.perf_counter_ns()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining / 1_000_000_000.0))


def _terminate_one(process: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


def make_run_id(suite_id: str, condition_id: str, repeat_index: int) -> str:
    return safe_id(f"{suite_id}_{condition_id}_r{repeat_index}", max_length=120)


def _null_run(
    config: dict[str, Any],
    condition: dict[str, Any],
    run_id: str,
    repeat_index: int,
    start_time: str,
    end_time: str,
    error: str,
) -> dict[str, Any]:
    """失败轮次：状态 error，全部指标为 null（不写 0、不写 N/A）。"""
    run: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metrics_definition_version": METRICS_DEFINITION_VERSION,
        "middleware_id": MIDDLEWARE_ID,
        "module_name": MIDDLEWARE_ID,
        "module_version": config["suite"]["module_version"],
        "vendor": config["suite"].get("vendor", VENDOR),
        "measurement_engine": (
            f"{config['suite'].get('vendor', VENDOR)} "
            f"{config['suite']['module_version']} via Python binding"
        ),
        "adapter_version": ADAPTER_VERSION,
        "run_id": run_id,
        "condition_id": condition["condition_id"],
        "scenario_id": condition["scenario_id"],
        "scenario_name": condition["scenario_name"],
        "scenario_title": condition.get("scenario_title"),
        "repeat_index": repeat_index,
        "status": "error",
        "capability": condition.get("capability", "supported"),
        "payload_size_bytes": condition["payload_size_bytes"],
        "message_count": condition["message_count"],
        "publish_rate_hz": condition["publish_rate_hz"],
        "publisher_count": condition["publisher_count"],
        "subscriber_count": condition["subscriber_count"],
        "transport_mode": condition["transport_mode"],
        "transport_detail": transport_detail(condition["transport_mode"]),
        "qos_profile": condition["qos_profile"],
        "network_profile": condition["network_profile"],
        "effective_config": condition,
        "test_start_time": start_time,
        "test_end_time": end_time,
        "discovery_supported": True,
        "errors": [error],
        "warnings": [],
        "fault_events": [],
        "restart_count": 0,
        "artifacts": {},
        "artifacts_directory": None,
        "timing_details": {},
        "resource_details": {},
        "send_window_seconds": None,
    }
    for field in STANDARD_METRIC_FIELDS:
        run[field] = None
    for field in (
        "latency_sample_count",
        "jitter_sample_count",
        "sent_success_count",
        "sent_failure_count",
        "expected_delivery_count",
        "received_before_count",
        "valid_unique_delivery_count",
        "receive_attempt_count",
        "duplicate_count",
        "out_of_order_count",
        "corrupted_count",
        "unexpected_count",
        "clock_anomaly_count",
        "metadata_mismatch_count",
        "duplicate_send_count",
        "malformed_send_count",
        "recovered_count",
    ):
        run[field] = 0
    run["network_injected_packet_loss"] = (
        float(condition.get("network", {}).get("packet_loss_percent", 0)) / 100.0
    )
    run["per_link"] = []
    return run


_ENDPOINT_RESULT_PATTERN = r"^{role}_{endpoint_id}(?:_r\d+)?\.result\.json$"


def _collect_endpoint_results(
    run_dir: Path, role: str, endpoint_id: int
) -> dict[str, Any] | None:
    """收集某个逻辑端点的全部结果文件，并在故障重启时合并成一个。

    故障重启会让同一个逻辑端点在轮次内产生两个进程（两个原始记录文件），
    指标必须基于两份真实记录计算，所以这里按角色/编号合并计数与文件列表。
    """
    import re

    pattern = re.compile(
        _ENDPOINT_RESULT_PATTERN.format(role=role, endpoint_id=endpoint_id)
    )
    parts: list[dict[str, Any]] = []
    for path in sorted(run_dir.iterdir()):
        if pattern.match(path.name):
            try:
                parts.append(read_json(path))
            except (OSError, ValueError):
                continue
    if not parts:
        return None
    merged: dict[str, Any] = dict(parts[0])
    raw_files = [part["raw_file"] for part in parts if part.get("raw_file")]
    merged["raw_files"] = raw_files
    merged["raw_file"] = raw_files[0] if raw_files else None
    merged["pid"] = parts[-1].get("pid")
    merged["restart_count"] = len(parts) - 1
    for field in (
        "raw_record_count",
        "sent_success_count",
        "send_failure_count",
        "warmup_success_count",
        "snapshot_record_count",
        "read_error_count",
        "decode_error_count",
        "non_data_sample_count",
        "process_cpu_time_seconds",
        "process_wall_time_seconds",
    ):
        if any(field in part for part in parts):
            merged[field] = sum(int(part.get(field) or 0) for part in parts)
    merged["status"] = (
        "error"
        if any(part.get("status") == "error" for part in parts)
        else "completed"
    )
    for field in ("error", "cleanup_error", "first_callback_error"):
        merged[field] = next(
            (part.get(field) for part in parts if part.get(field)), None
        )
    merged["completed_time"] = parts[-1].get("completed_time")
    return merged


def _artifact_name(path: Path, run_dir: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(run_dir).resolve()).as_posix()
    except ValueError:
        return Path(path).name


def run_once(
    config: dict[str, Any],
    base_condition: dict[str, Any],
    suite_id: str,
    repeat_index: int,
    run_ordinal: int,
    output_root: Path,
    run_dir: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    condition = effective_condition(config, base_condition)
    condition_payload = make_payload_text(
        int(condition["payload_size_bytes"]),
        int(condition["random_seed"]),
        condition["condition_id"],
    )
    condition["payload_checksum"] = crc32_text(condition_payload)
    planned_seconds = _planned_seconds(
        condition, float(config["suite"]["default_run_timeout_seconds"])
    )
    # 控制台表单里的 timeout_seconds 覆盖套件默认的启动/发现超时（根 README §5）。
    condition_timeout = condition.get("timeout_seconds")
    startup_timeout = float(
        condition_timeout or config["suite"]["startup_timeout_seconds"]
    )
    discovery_timeout = float(
        condition_timeout or config["suite"]["discovery_timeout_seconds"]
    )
    endpoint_timeout = (
        startup_timeout
        + discovery_timeout
        + float(condition["warmup_seconds"])
        + max(planned_seconds * 3.0, 30.0)
        + float(condition["drain_seconds"])
        + 15.0
    )
    condition["endpoint_timeout_seconds"] = endpoint_timeout
    condition["startup_timeout_seconds"] = startup_timeout
    condition["discovery_timeout_seconds"] = discovery_timeout

    run_id = run_id or make_run_id(suite_id, condition["condition_id"], repeat_index)
    run_dir = Path(run_dir) if run_dir is not None else Path(output_root) / "artifacts" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "status").mkdir()
    (run_dir / "logs").mkdir()
    condition_file = run_dir / "condition.json"
    atomic_write_json(condition_file, condition)
    transport_file = render_transport_xml(
        run_dir / "fastdds_profiles.xml",
        condition["transport_mode"],
        f"fastdds_bench_{run_id}",
    )

    test_start_time = utc_now_iso()
    harness_start_ns = time.perf_counter_ns()
    registry = ProcessRegistry()
    # 被强杀时保证端点进程一起退出（Windows Job Object，见 DDS_readme_1.md §3.5.7）。
    job = create_job()
    log_streams: list[Any] = []
    monitor = ResourceMonitor(
        registry.pids,
        int(config["suite"]["resource_sample_interval_ms"]),
    )
    monitor.start()
    resources: dict[str, Any] | None = None
    fault_events: list[dict[str, Any]] = []
    fault_kind = str((condition.get("fault_plan") or {}).get("kind", "none"))
    planned_downtime = float(
        (condition.get("fault_plan") or {}).get("downtime_seconds", 0.0) or 0.0
    )

    try:
        network = condition["network"]
        if network.get("impairment_required") and not network.get(
            "external_impairment_confirmed"
        ):
            raise RunFailure(
                f"Network profile {condition['network_profile']!r} requires an external "
                "network emulator and explicit external_impairment_confirmed=true"
            )
        if fault_kind not in {"none", "restart_publisher", "restart_subscriber"}:
            raise RunFailure(
                f"fault_plan.kind={fault_kind!r} must be supplied by the shared "
                "cross-middleware fault orchestrator"
            )

        child_env = os.environ.copy()
        child_env["FASTDDS_DEFAULT_PROFILES_FILE"] = str(transport_file.resolve())
        child_env["FASTRTPS_DEFAULT_PROFILES_FILE"] = str(transport_file.resolve())
        child_env.pop("FASTDDS_BUILTIN_TRANSPORTS", None)
        domain_id = (int(config["suite"]["domain_id_base"]) + run_ordinal) % 233
        topic_name = safe_id(f"UnifiedBench_{run_id}", max_length=200)
        entry = PROJECT_ROOT / "endpoint_entry.py"

        def spawn(
            role: str, endpoint_id: int, suffix: str = "", start_sequence: int = 0
        ) -> subprocess.Popen[Any]:
            log_path = run_dir / "logs" / f"{role}_{endpoint_id}{suffix}.log"
            stream = log_path.open("w", encoding="utf-8", newline="\n")
            log_streams.append(stream)
            command = [
                sys.executable,
                str(entry),
                "--role",
                role,
                "--endpoint-id",
                str(endpoint_id),
                "--run-dir",
                str(run_dir.resolve()),
                "--condition-file",
                str(condition_file.resolve()),
                "--topic-name",
                topic_name,
                "--domain-id",
                str(domain_id),
                "--profile-name",
                PROFILE_NAME,
                "--raw-suffix",
                suffix,
                "--start-sequence",
                str(start_sequence),
            ]
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=child_env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            if job is not None:
                job.assign(process)
            return registry.register(process)

        endpoints: dict[tuple[str, int], subprocess.Popen[Any]] = {}
        for endpoint_id in range(int(condition["subscriber_count"])):
            endpoints[("subscriber", endpoint_id)] = spawn("subscriber", endpoint_id)
        for endpoint_id in range(int(condition["publisher_count"])):
            endpoints[("publisher", endpoint_id)] = spawn("publisher", endpoint_id)

        status_paths = _status_files(run_dir, condition)
        ready_statuses = _wait_status(
            status_paths,
            registry,
            lambda values: all(isinstance(v.get("ready_ns"), int) for v in values),
            startup_timeout,
            "all endpoints ready",
        )
        ready_ns = max(int(value["ready_ns"]) for value in ready_statuses)
        startup_time_ms = (ready_ns - harness_start_ns) / 1_000_000.0

        matched_statuses = _wait_status(
            status_paths,
            registry,
            lambda values: all(isinstance(v.get("matched_ns"), int) for v in values),
            discovery_timeout,
            "full endpoint discovery",
        )
        discovery_start_ns = min(
            int(value["participant_created_ns"]) for value in matched_statuses
        )
        discovery_complete_ns = max(int(value["matched_ns"]) for value in matched_statuses)
        discovery_time_ms = (discovery_complete_ns - discovery_start_ns) / 1_000_000.0

        warmup_start_ns = time.perf_counter_ns() + int(
            int(config["suite"]["barrier_delay_ms"]) * 1_000_000
        )
        formal_start_ns = warmup_start_ns + int(
            float(condition["warmup_seconds"]) * 1_000_000_000
        )
        atomic_write_json(
            run_dir / "start.json",
            {
                "warmup_start_ns": warmup_start_ns,
                "formal_start_ns": formal_start_ns,
            },
        )

        # 故障重启：真实结束并重新拉起一个端点进程，而不是在回调里伪造丢包。
        if fault_kind != "none":
            role = "publisher" if fault_kind == "restart_publisher" else "subscriber"
            plan = condition["fault_plan"]
            endpoint_id = int(plan.get("endpoint_id", 0))
            at_seconds = float(plan.get("at_seconds", 0.0))
            downtime = float(plan.get("downtime_seconds", 0.0))
            victim = endpoints.get((role, endpoint_id))
            if victim is None:
                raise RunFailure(f"fault_plan targets missing {role} #{endpoint_id}")
            fault_at_ns = formal_start_ns + int(at_seconds * 1_000_000_000)

            def fault_worker() -> None:
                event: dict[str, Any] = {
                    "kind": fault_kind,
                    "role": role,
                    "endpoint_id": endpoint_id,
                    "planned_at_seconds": at_seconds,
                    "downtime_seconds": downtime,
                    "victim_pid": victim.pid,
                }
                try:
                    while time.perf_counter_ns() < fault_at_ns:
                        time.sleep(0.02)
                    event["down_utc"] = utc_now_iso()
                    event["down_offset_seconds"] = (
                        time.perf_counter_ns() - formal_start_ns
                    ) / 1e9
                    registry.unregister(victim)
                    _terminate_one(victim)
                    time.sleep(downtime)
                    start_sequence = 0
                    if role == "publisher":
                        rate = float(condition["publish_rate_hz"])
                        if rate > 0:
                            elapsed = (
                                time.perf_counter_ns() - formal_start_ns
                            ) / 1_000_000_000.0
                            start_sequence = max(0, int(math.ceil(elapsed * rate)))
                        limit = int(condition["message_count"])
                        if limit > 0:
                            start_sequence = min(start_sequence, limit)
                    replacement = spawn(role, endpoint_id, "_r2", start_sequence)
                    endpoints[(role, endpoint_id)] = replacement
                    event["restart_utc"] = utc_now_iso()
                    event["restart_offset_seconds"] = (
                        time.perf_counter_ns() - formal_start_ns
                    ) / 1e9
                    event["restart_pid"] = replacement.pid
                    event["restart_sequence"] = start_sequence
                except Exception as exc:  # 故障执行失败必须留下证据
                    event["error"] = f"{type(exc).__name__}: {exc}"
                fault_events.append(event)

            threading.Thread(target=fault_worker, daemon=True).start()

        publisher_status_paths = [
            run_dir / "status" / f"publisher_{i}.json"
            for i in range(int(condition["publisher_count"]))
        ]
        send_timeout = (
            int(config["suite"]["barrier_delay_ms"]) / 1000.0
            + float(condition["warmup_seconds"])
            + max(planned_seconds * 3.0, 30.0)
            + planned_downtime
        )
        _wait_status(
            publisher_status_paths,
            registry,
            lambda values: all(isinstance(v.get("send_done_ns"), int) for v in values),
            send_timeout,
            "all publishers to finish formal sends",
        )
        send_complete_ns = time.perf_counter_ns()
        atomic_write_json(
            run_dir / "send_complete.json", {"send_complete_ns": send_complete_ns}
        )

        drain_end_ns = send_complete_ns + int(
            float(condition["drain_seconds"]) * 1_000_000_000
        )
        _wait_until_with_process_checks(drain_end_ns, registry)
        atomic_write_json(run_dir / "stop.json", {"stop_ns": time.perf_counter_ns()})

        registry.wait_all(15.0)
        failure = registry.failure()
        if failure:
            raise RunFailure(failure)

        resources = monitor.stop()
        resources.update(monitor.rss_window(formal_start_ns, send_complete_ns))
        resource_window_ns = int(resources["resource_monitor_end_ns"]) - int(
            resources["resource_monitor_start_ns"]
        )
        publisher_results = [
            _collect_endpoint_results(run_dir, "publisher", i)
            for i in range(int(condition["publisher_count"]))
        ]
        subscriber_results = [
            _collect_endpoint_results(run_dir, "subscriber", i)
            for i in range(int(condition["subscriber_count"]))
        ]
        missing = [
            f"{role}#{index}"
            for role, results in (
                ("publisher", publisher_results),
                ("subscriber", subscriber_results),
            )
            for index, result in enumerate(results)
            if result is None
        ]
        if missing:
            raise RunFailure(f"Missing endpoint results: {', '.join(missing)}")
        endpoint_results = [*publisher_results, *subscriber_results]
        endpoint_errors = [
            result.get("error") for result in endpoint_results if result.get("error")
        ]
        if endpoint_errors:
            raise RunFailure(str(endpoint_errors[0]))
        resources["cpu_time_seconds"] = sum(
            float(result.get("process_cpu_time_seconds", 0.0))
            for result in endpoint_results
        )
        timings: dict[str, Any] = {
            "startup_time_ms": startup_time_ms,
            "discovery_time_ms": discovery_time_ms,
            "harness_start_ns": harness_start_ns,
            "discovery_start_ns": discovery_start_ns,
            "discovery_complete_ns": discovery_complete_ns,
            "formal_start_ns": formal_start_ns,
            "send_complete_ns": send_complete_ns,
            "resource_window_ns": resource_window_ns,
        }
        metrics = analyze_run(
            condition,
            run_dir,
            publisher_results,
            subscriber_results,
            timings,
            resources,
        )
        read_errors = sum(
            int(result.get("read_error_count", 0)) for result in subscriber_results
        )
        decode_errors = sum(
            int(result.get("decode_error_count", 0)) for result in subscriber_results
        )
        warnings: list[str] = []
        if read_errors:
            warnings.append(f"Subscriber read callback errors: {read_errors}")
        if decode_errors:
            warnings.append(f"Subscriber payload decode errors: {decode_errors}")
        callback_errors = [
            result.get("first_callback_error")
            for result in subscriber_results
            if result.get("first_callback_error")
        ]
        if callback_errors:
            warnings.append(f"First subscriber callback error: {callback_errors[0]}")
        cleanup_errors = [
            result.get("cleanup_error")
            for result in endpoint_results
            if result.get("cleanup_error")
        ]
        warnings.extend(f"Endpoint cleanup warning: {value}" for value in cleanup_errors)
        fault_errors = [event.get("error") for event in fault_events if event.get("error")]
        warnings.extend(f"Fault injection warning: {value}" for value in fault_errors)

        errors: list[str] = []
        if metrics["sent_success_count"] == 0:
            errors.append("No formal message was successfully sent")
        expected_sends = int(condition["message_count"]) * int(
            condition["publisher_count"]
        )
        if fault_kind == "none" and int(condition["message_count"]) > 0:
            if metrics["sent_success_count"] != expected_sends:
                errors.append(
                    f"Successful sends {metrics['sent_success_count']} != configured "
                    f"message_count {condition['message_count']} x "
                    f"publisher_count {condition['publisher_count']}"
                )
        if metrics["corrupted_count"] > 0 or read_errors > 0:
            errors.append("Data-integrity validation reported errors")

        artifacts: dict[str, Any] = {
            "run_directory": Path(run_dir).name,
            "transport_profile": _artifact_name(transport_file, run_dir),
            "publisher_raw": [],
            "subscriber_raw": [],
            "logs": [],
        }
        for result in publisher_results:
            for raw_path in result.get("raw_files") or []:
                path = run_dir / raw_path
                if path.exists():
                    artifacts["publisher_raw"].append(
                        {
                            "path": _artifact_name(path, run_dir),
                            "sha256": sha256_file(path),
                            "record_count": result["raw_record_count"],
                        }
                    )
        for result in subscriber_results:
            for raw_path in result.get("raw_files") or []:
                path = run_dir / raw_path
                if path.exists():
                    artifacts["subscriber_raw"].append(
                        {
                            "path": _artifact_name(path, run_dir),
                            "sha256": sha256_file(path),
                            "record_count": result["raw_record_count"],
                        }
                    )
        artifacts["logs"] = [
            _artifact_name(path, run_dir)
            for path in sorted((run_dir / "logs").glob("*.log"))
        ]

        test_end_time = utc_now_iso()
        run = {
            "schema_version": SCHEMA_VERSION,
            "metrics_definition_version": METRICS_DEFINITION_VERSION,
            "middleware_id": MIDDLEWARE_ID,
            "module_name": MIDDLEWARE_ID,
            "module_version": config["suite"]["module_version"],
            "vendor": config["suite"].get("vendor", VENDOR),
            "label": config["suite"].get("label", "DDS"),
            "measurement_engine": (
                f"{config['suite'].get('vendor', VENDOR)} "
                f"{config['suite']['module_version']} via Python binding"
            ),
            "adapter_version": ADAPTER_VERSION,
            "suite_id": suite_id,
            "run_id": run_id,
            "condition_id": condition["condition_id"],
            "scenario_id": condition["scenario_id"],
            "scenario_name": condition["scenario_name"],
            "scenario_title": condition.get("scenario_title"),
            "repeat_index": repeat_index,
            "status": "error" if errors else "completed",
            "capability": condition.get("capability", "supported"),
            "payload_size_bytes": condition["payload_size_bytes"],
            "message_count": condition["message_count"],
            "publish_rate_hz": condition["publish_rate_hz"],
            "publisher_count": condition["publisher_count"],
            "subscriber_count": condition["subscriber_count"],
            "transport_mode": condition["transport_mode"],
            "transport_detail": transport_detail(condition["transport_mode"]),
            "qos_profile": condition["qos_profile"],
            "network_profile": condition["network_profile"],
            "effective_config": condition,
            "test_start_time": test_start_time,
            "test_end_time": test_end_time,
            "discovery_supported": True,
            "errors": errors,
            "warnings": warnings,
            "fault_events": fault_events,
            "restart_count": sum(
                int(result.get("restart_count") or 0) for result in endpoint_results
            ),
            "timing_details": timings,
            "resource_details": resources,
            "artifacts": artifacts,
            "artifacts_directory": f"artifacts/{run_id}",
            **metrics,
        }
        return run
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            atomic_write_json(run_dir / "stop.json", {"stop_ns": time.perf_counter_ns()})
        except Exception:
            pass
        registry.terminate_all()
        if resources is None:
            try:
                resources = monitor.stop()
            except Exception:
                resources = {}
        traceback_text = traceback.format_exc()
        (run_dir / "controller_error.txt").write_text(
            traceback_text, encoding="utf-8", newline="\n"
        )
        failed = _null_run(
            config,
            condition,
            run_id,
            repeat_index,
            test_start_time,
            utc_now_iso(),
            error,
        )
        failed["suite_id"] = suite_id
        failed["fault_events"] = fault_events
        failed["artifacts"] = {
            "run_directory": Path(run_dir).name,
            "controller_error": _artifact_name(run_dir / "controller_error.txt", run_dir),
        }
        failed["artifacts_directory"] = f"artifacts/{run_id}"
        failed["resource_details"] = resources or {}
        return failed
    finally:
        registry.terminate_all()
        if job is not None:
            job.close()
        for stream in log_streams:
            try:
                stream.close()
            except OSError:
                pass


def write_run_sample_file(
    condition: dict[str, Any],
    run_dir: Path,
    publisher_results: list[dict[str, Any]],
    subscriber_results: list[dict[str, Any]],
    run_id: str,
) -> str | None:
    """写出前端延迟曲线需要的 `subscriber-0.result.json`（真实样本，不插值）。"""
    if not subscriber_results:
        return None
    try:
        samples = subscriber_latency_samples(
            condition, run_dir, publisher_results, subscriber_results[0]
        )
    except (OSError, ValueError):
        return None
    document = sample_document(
        samples, run_id=run_id, condition_id=condition.get("condition_id")
    )
    atomic_write_json(run_dir / "subscriber-0.result.json", document)
    return "subscriber-0.result.json"


def run_suite(
    config_path: Path,
    output_dir: Path,
    condition_ids: list[str] | None = None,
    repeats_override: int | None = None,
    log: Callable[[str], None] = print,
    label: str = "CLI",
) -> dict[str, Any]:
    """顶层套件执行：写 output/result.json、output/runs/<run_id>.json、output/artifacts/<run_id>/。"""
    config, config_warnings = load_config(config_path)
    runtime_probe = probe_fastdds_runtime()
    if not runtime_probe["ok"]:
        raise RuntimeError(
            "Fast DDS Python runtime is not ready. Run DDS/setup_windows.bat first. "
            f"Probe error: {runtime_probe['error']}"
        )
    conditions = select_conditions(config, condition_ids)
    if repeats_override is not None and repeats_override < 1:
        raise ValueError("repeats_override must be >= 1")

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    suite_id = safe_id(
        f"{utc_now_iso().replace(':', '_').replace('.', '_')}_p{os.getpid()}"
    )
    environment = {**collect_environment(config), "runtime_probe": runtime_probe}
    limitations = [
        "本套件由 CLI 直接执行；控制台作业请使用 DDS/console_runner.py",
        "缺失/未测量/不适用的指标一律写 null，不写 0，也不插值或生成数据",
    ]
    planned = sum(
        (repeats_override or int(condition["repeats"])) for condition in conditions
    )
    started = utc_now_iso()
    runs: list[dict[str, Any]] = []
    limit = list(limitations)

    def publish(status: str) -> dict[str, Any]:
        document = suite_document(
            run_id=suite_id,
            status=status,
            planned_runs=planned,
            test_start_time=started,
            test_end_time=None if status == "running" else utc_now_iso(),
            environment=environment,
            configuration={
                "config_source": str(Path(config_path).resolve()),
                "config_warnings": config_warnings,
                "rate_scope": config["suite"]["rate_scope"],
                "message_count_scope": config["suite"]["message_count_scope"],
                "repeats_override": repeats_override,
                "default_repeats": config["suite"]["default_repeats"],
                "formal_repeats": config["suite"]["formal_repeats"],
                "selected_conditions": [c["condition_id"] for c in conditions],
                "invoked_by": label,
            },
            runs=runs,
            limitations=limit,
            middleware_version=config["suite"]["module_version"],
        )
        atomic_write_json(output_dir / "result.json", document)
        atomic_write_json(output_dir / "runs" / f"{suite_id}.suite.json", document)
        return document

    publish("running")
    ordinal = 0
    try:
        for condition in conditions:
            repeats = repeats_override or int(condition["repeats"])
            for repeat_index in range(1, repeats + 1):
                run_id = make_run_id(suite_id, condition["condition_id"], repeat_index)
                run_dir = output_dir / "artifacts" / run_id
                log(f"START {condition['condition_id']} repeat={repeat_index}/{repeats}")
                result = run_once(
                    config,
                    condition,
                    suite_id,
                    repeat_index,
                    ordinal,
                    output_dir,
                    run_dir=run_dir,
                    run_id=run_id,
                )
                mapped = map_engine_run(result, environment=environment)
                _write_samples_into(
                    mapped,
                    run_dir,
                    result,
                    condition,
                    run_id,
                )
                runs.append(mapped)
                atomic_write_json(output_dir / "runs" / f"{run_id}.json", mapped)
                log(
                    f"END {condition['condition_id']} status={mapped['status']} "
                    f"sent={mapped['messages_sent']} received={mapped['messages_received']}"
                )
                publish("running")
                ordinal += 1
    except KeyboardInterrupt:
        log("Cancelled by user; completed runs retained")
        return publish("cancelled")
    return publish("completed" if all(r["status"] == "completed" for r in runs) else "error")


def _write_samples_into(
    mapped: dict[str, Any],
    run_dir: Path,
    engine_run: dict[str, Any],
    condition: dict[str, Any],
    run_id: str,
) -> None:
    """从真实原始记录生成 `subscriber-0.result.json` 并回写 samples_path。"""
    publisher_results = []
    subscriber_results = []
    try:
        for index in range(int(condition["publisher_count"])):
            found = _collect_endpoint_results(run_dir, "publisher", index)
            if found:
                publisher_results.append(found)
        for index in range(int(condition["subscriber_count"])):
            found = _collect_endpoint_results(run_dir, "subscriber", index)
            if found:
                subscriber_results.append(found)
    except OSError:
        return
    if not subscriber_results:
        return
    name = write_run_sample_file(
        engine_run.get("effective_config") or condition,
        run_dir,
        publisher_results,
        subscriber_results,
        run_id,
    )
    if name:
        mapped["samples_path"] = f"{mapped.get('artifacts_directory')}/{name}"
