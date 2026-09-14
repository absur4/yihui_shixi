from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

import psutil

from . import ADAPTER_VERSION, METRICS_DEFINITION_VERSION, SCHEMA_VERSION
from .config import effective_condition, load_config, select_conditions
from .metrics import STANDARD_METRIC_FIELDS, analyze_run
from .payload import crc32_text, make_payload_text, read_payload_text
from .resource_monitor import ResourceMonitor
from .results import summarize_runs
from .transport import PROFILE_NAME, render_transport_xml
from .util import (
    PROJECT_ROOT,
    atomic_write_json,
    read_json,
    relative_posix,
    safe_id,
    sha256_file,
    utc_now_iso,
)


class RunFailure(RuntimeError):
    pass


def collect_environment(config: dict[str, Any]) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    package_versions: dict[str, str | None] = {}
    for distribution in ("PyYAML", "psutil", "jsonschema", "pywin32"):
        try:
            package_versions[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            package_versions[distribution] = None
    return {
        "os": platform.platform(),
        "os_name": os.name,
        "machine": platform.machine(),
        "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER"),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "total_memory_bytes": int(memory.total),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": sys.executable,
        "python_packages": package_versions,
        "fastdds_home": os.environ.get("FASTDDSHOME"),
        "clock": {
            "name": "time.perf_counter_ns",
            "monotonic": time.get_clock_info("perf_counter").monotonic,
            "resolution_seconds": time.get_clock_info("perf_counter").resolution,
            "same_host_required": True,
        },
        "configured_fastdds_version": config["suite"]["module_version"],
        "configured_fastdds_python_version": config["suite"][
            "fastdds_python_version"
        ],
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


def _process_failure(processes: list[subprocess.Popen[Any]]) -> str | None:
    for process in processes:
        code = process.poll()
        if code is not None and code != 0:
            return f"Endpoint pid={process.pid} exited with code {code}"
    return None


def _wait_status(
    paths: list[Path],
    processes: list[subprocess.Popen[Any]],
    predicate: Callable[[list[dict[str, Any]]], bool],
    timeout_seconds: float,
    description: str,
) -> list[dict[str, Any]]:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        failure = _process_failure(processes)
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


def _wait_until_with_process_checks(
    target_ns: int,
    processes: list[subprocess.Popen[Any]],
) -> None:
    while True:
        failure = _process_failure(processes)
        if failure:
            raise RunFailure(failure)
        remaining = target_ns - time.perf_counter_ns()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining / 1_000_000_000.0))


def _terminate_owned_processes(processes: list[subprocess.Popen[Any]]) -> None:
    live = [process for process in processes if process.poll() is None]
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


def _null_run(
    config: dict[str, Any],
    condition: dict[str, Any],
    run_id: str,
    repeat_index: int,
    start_time: str,
    end_time: str,
    error: str,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metrics_definition_version": METRICS_DEFINITION_VERSION,
        "module_name": "fastdds",
        "module_version": config["suite"]["module_version"],
        "measurement_engine": f"eProsima Fast DDS {config['suite']['module_version']} via Python binding",
        "adapter_version": ADAPTER_VERSION,
        "run_id": run_id,
        "condition_id": condition["condition_id"],
        "scenario_id": condition["scenario_id"],
        "scenario_name": condition["scenario_name"],
        "repeat_index": repeat_index,
        "status": "failed",
        "payload_size_bytes": condition["payload_size_bytes"],
        "message_count": condition["message_count"],
        "publish_rate_hz": condition["publish_rate_hz"],
        "publisher_count": condition["publisher_count"],
        "subscriber_count": condition["subscriber_count"],
        "transport_mode": condition["transport_mode"],
        "qos_profile": condition["qos_profile"],
        "network_profile": condition["network_profile"],
        "effective_config": condition,
        "test_start_time": start_time,
        "test_end_time": end_time,
        "discovery_supported": True,
        "errors": [error],
        "warnings": [],
        "artifacts": {},
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


def run_once(
    config: dict[str, Any],
    base_condition: dict[str, Any],
    suite_id: str,
    repeat_index: int,
    run_ordinal: int,
    output_root: Path,
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
    endpoint_timeout = (
        float(config["suite"]["startup_timeout_seconds"])
        + float(config["suite"]["discovery_timeout_seconds"])
        + float(condition["warmup_seconds"])
        + max(planned_seconds * 3.0, 30.0)
        + float(condition["drain_seconds"])
        + 15.0
    )
    condition["endpoint_timeout_seconds"] = endpoint_timeout
    condition["discovery_timeout_seconds"] = config["suite"][
        "discovery_timeout_seconds"
    ]

    timestamp = utc_now_iso().replace(":", "_").replace(".", "_").replace("Z", "Z")
    run_id = safe_id(
        f"{timestamp}_{suite_id}_{condition['condition_id']}_r{repeat_index}"
    )
    run_dir = output_root / "raw" / suite_id / run_id
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
    processes: list[subprocess.Popen[Any]] = []
    log_streams: list[Any] = []
    monitor = ResourceMonitor(
        lambda: [process.pid for process in processes],
        int(config["suite"]["resource_sample_interval_ms"]),
    )
    monitor.start()
    resources: dict[str, Any] | None = None

    try:
        network = condition["network"]
        if network.get("impairment_required") and not network.get(
            "external_impairment_confirmed"
        ):
            raise RunFailure(
                f"Network profile {condition['network_profile']!r} requires an external "
                "network emulator and explicit external_impairment_confirmed=true"
            )
        if condition.get("fault_plan", {}).get("kind", "none") != "none":
            raise RunFailure(
                "This baseline package records S11 fields but process fault injection must "
                "be supplied by the shared cross-middleware fault orchestrator"
            )

        child_env = os.environ.copy()
        child_env["FASTDDS_DEFAULT_PROFILES_FILE"] = str(transport_file.resolve())
        child_env["FASTRTPS_DEFAULT_PROFILES_FILE"] = str(transport_file.resolve())
        child_env.pop("FASTDDS_BUILTIN_TRANSPORTS", None)
        domain_id = (
            int(config["suite"]["domain_id_base"]) + run_ordinal
        ) % 233
        topic_name = safe_id(f"UnifiedBench_{run_id}", max_length=200)
        entry = PROJECT_ROOT / "endpoint_entry.py"

        def spawn(role: str, endpoint_id: int) -> None:
            log_path = run_dir / "logs" / f"{role}_{endpoint_id}.log"
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
            ]
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            processes.append(
                subprocess.Popen(
                    command,
                    cwd=PROJECT_ROOT,
                    env=child_env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            )

        for endpoint_id in range(int(condition["subscriber_count"])):
            spawn("subscriber", endpoint_id)
        for endpoint_id in range(int(condition["publisher_count"])):
            spawn("publisher", endpoint_id)

        status_paths = _status_files(run_dir, condition)
        ready_statuses = _wait_status(
            status_paths,
            processes,
            lambda values: all(isinstance(v.get("ready_ns"), int) for v in values),
            float(config["suite"]["startup_timeout_seconds"]),
            "all endpoints ready",
        )
        ready_ns = max(int(value["ready_ns"]) for value in ready_statuses)
        startup_time_ms = (ready_ns - harness_start_ns) / 1_000_000.0

        matched_statuses = _wait_status(
            status_paths,
            processes,
            lambda values: all(isinstance(v.get("matched_ns"), int) for v in values),
            float(config["suite"]["discovery_timeout_seconds"]),
            "full endpoint discovery",
        )
        discovery_start_ns = min(
            int(value["participant_created_ns"]) for value in matched_statuses
        )
        discovery_complete_ns = max(int(value["matched_ns"]) for value in matched_statuses)
        discovery_time_ms = (
            discovery_complete_ns - discovery_start_ns
        ) / 1_000_000.0

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

        publisher_status_paths = [
            run_dir / "status" / f"publisher_{i}.json"
            for i in range(int(condition["publisher_count"]))
        ]
        send_timeout = (
            int(config["suite"]["barrier_delay_ms"]) / 1000.0
            + float(condition["warmup_seconds"])
            + max(planned_seconds * 3.0, 30.0)
        )
        _wait_status(
            publisher_status_paths,
            processes,
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
        _wait_until_with_process_checks(drain_end_ns, processes)
        atomic_write_json(run_dir / "stop.json", {"stop_ns": time.perf_counter_ns()})

        exit_deadline = time.perf_counter() + 15.0
        for process in processes:
            remaining = max(0.01, exit_deadline - time.perf_counter())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise RunFailure(f"Endpoint pid={process.pid} did not stop") from exc
        failure = _process_failure(processes)
        if failure:
            raise RunFailure(failure)

        resources = monitor.stop()
        resources.update(monitor.rss_window(formal_start_ns, send_complete_ns))
        resource_window_ns = int(resources["resource_monitor_end_ns"]) - int(
            resources["resource_monitor_start_ns"]
        )
        publisher_results = [
            read_json(run_dir / f"publisher_{i}.result.json")
            for i in range(int(condition["publisher_count"]))
        ]
        subscriber_results = [
            read_json(run_dir / f"subscriber_{i}.result.json")
            for i in range(int(condition["subscriber_count"]))
        ]
        endpoint_results = publisher_results + subscriber_results
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
        errors: list[str] = []
        if metrics["sent_success_count"] == 0:
            errors.append("No formal message was successfully sent")
        if int(condition["message_count"]) > 0 and metrics["sent_success_count"] != int(
            condition["message_count"]
        ):
            errors.append(
                f"Successful sends {metrics['sent_success_count']} != configured "
                f"message_count {condition['message_count']}"
            )
        if metrics["corrupted_count"] > 0 or read_errors > 0:
            errors.append("Data-integrity validation reported errors")

        artifacts: dict[str, Any] = {
            "run_directory": relative_posix(run_dir),
            "transport_profile": relative_posix(transport_file),
            "publisher_raw": [],
            "subscriber_raw": [],
            "logs": [],
        }
        for result in publisher_results:
            raw_path = run_dir / result["raw_file"]
            artifacts["publisher_raw"].append(
                {
                    "path": relative_posix(raw_path),
                    "sha256": sha256_file(raw_path),
                    "record_count": result["raw_record_count"],
                }
            )
        for result in subscriber_results:
            raw_path = run_dir / result["raw_file"]
            artifacts["subscriber_raw"].append(
                {
                    "path": relative_posix(raw_path),
                    "sha256": sha256_file(raw_path),
                    "record_count": result["raw_record_count"],
                }
            )
        artifacts["logs"] = [
            relative_posix(path)
            for path in sorted((run_dir / "logs").glob("*.log"))
        ]

        test_end_time = utc_now_iso()
        run = {
            "schema_version": SCHEMA_VERSION,
            "metrics_definition_version": METRICS_DEFINITION_VERSION,
            "module_name": "fastdds",
            "module_version": config["suite"]["module_version"],
            "measurement_engine": f"eProsima Fast DDS {config['suite']['module_version']} via Python binding",
            "adapter_version": ADAPTER_VERSION,
            "run_id": run_id,
            "condition_id": condition["condition_id"],
            "scenario_id": condition["scenario_id"],
            "scenario_name": condition["scenario_name"],
            "repeat_index": repeat_index,
            "status": "passed" if not errors else "failed",
            "payload_size_bytes": condition["payload_size_bytes"],
            "message_count": condition["message_count"],
            "publish_rate_hz": condition["publish_rate_hz"],
            "publisher_count": condition["publisher_count"],
            "subscriber_count": condition["subscriber_count"],
            "transport_mode": condition["transport_mode"],
            "qos_profile": condition["qos_profile"],
            "network_profile": condition["network_profile"],
            "effective_config": condition,
            "test_start_time": test_start_time,
            "test_end_time": test_end_time,
            "discovery_supported": True,
            "errors": errors,
            "warnings": warnings,
            "timing_details": timings,
            "resource_details": resources,
            "artifacts": artifacts,
            **metrics,
        }
        return run
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            atomic_write_json(run_dir / "stop.json", {"stop_ns": time.perf_counter_ns()})
        except Exception:
            pass
        _terminate_owned_processes(processes)
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
        failed["artifacts"] = {
            "run_directory": relative_posix(run_dir),
            "controller_error": relative_posix(run_dir / "controller_error.txt"),
        }
        return failed
    finally:
        _terminate_owned_processes(processes)
        for stream in log_streams:
            try:
                stream.close()
            except OSError:
                pass


def run_suite(
    config_path: Path,
    output_path: Path,
    condition_ids: list[str] | None = None,
    repeats_override: int | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    config, config_warnings = load_config(config_path)
    runtime_probe = probe_fastdds_runtime()
    if not runtime_probe["ok"]:
        raise RuntimeError(
            "Fast DDS Python runtime is not ready. Run setup_windows.bat first. "
            f"Probe error: {runtime_probe['error']}"
        )
    conditions = select_conditions(config, condition_ids)
    if repeats_override is not None and repeats_override < 1:
        raise ValueError("repeats_override must be >= 1")

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Refusing to overwrite existing result: {output_path}. "
            "Choose another --output path or pass --overwrite explicitly."
        )
    suite_start_time = utc_now_iso()
    suite_id = safe_id(
        f"{suite_start_time.replace(':', '_').replace('.', '_')}_p{os.getpid()}"
    )
    suite: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite_id,
        "metrics_definition_version": METRICS_DEFINITION_VERSION,
        "module_name": "fastdds",
        "module_version": config["suite"]["module_version"],
        "adapter_version": ADAPTER_VERSION,
        "suite_start_time": suite_start_time,
        "suite_end_time": None,
        "environment": {
            **collect_environment(config),
            "runtime_probe": runtime_probe,
        },
        "config_source": relative_posix(Path(config_path).resolve()),
        "config_sha256": sha256_file(Path(config_path).resolve()),
        "config_warnings": config_warnings,
        "runs": [],
        "summary": [],
    }
    atomic_write_json(output_path, suite)

    ordinal = 0
    for condition in conditions:
        repeats = repeats_override or int(condition["repeats"])
        for repeat_index in range(1, repeats + 1):
            print(
                f"[{ordinal + 1}] {condition['condition_id']} repeat {repeat_index}/{repeats}",
                flush=True,
            )
            run = run_once(
                config,
                condition,
                suite_id,
                repeat_index,
                ordinal,
                output_path.parent,
            )
            suite["runs"].append(run)
            suite["summary"] = summarize_runs(suite["runs"])
            suite["suite_end_time"] = utc_now_iso()
            atomic_write_json(output_path, suite)
            ordinal += 1

    suite["summary"] = summarize_runs(suite["runs"])
    suite["suite_end_time"] = utc_now_iso()
    atomic_write_json(output_path, suite)
    return suite
