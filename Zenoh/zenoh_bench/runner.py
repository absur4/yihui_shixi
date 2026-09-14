from __future__ import annotations

import json
import platform
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .config import BenchConfig
from .stats import latency_metrics, round_numbers

METRICS_VERSION = "unified-middleware-1.0"
TRANSPORT_SCHEMES = {"TCP": "tcp", "UDP": "udp"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _zenoh_version() -> str:
    try:
        return version("eclipse-zenoh")
    except PackageNotFoundError:
        return "unknown"


def _mean(values: list[float | int | None]) -> float | None:
    numeric = [float(value) for value in values if value is not None]
    return sum(numeric) / len(numeric) if numeric else None


class ZenohBench:
    """Stable integration API for loopback runs and multi-host result aggregation."""

    def __init__(self, out_dir: str | Path = "results"):
        self.out_dir = Path(out_dir)

    @staticmethod
    def worker_command(role: str, config_json: str, index: int, output: str) -> list[str]:
        """Return a portable command for a publisher/subscriber on any host."""
        return [
            sys.executable,
            "-m",
            f"{__package__}.cli",
            "worker",
            role,
            "--config-json",
            config_json,
            "--index",
            str(index),
            "--output",
            output,
        ]

    @staticmethod
    def validate_loopback_config(config: BenchConfig | dict) -> BenchConfig:
        cfg = BenchConfig.from_dict(config.to_dict() if isinstance(config, BenchConfig) else config)
        mode = cfg.transport_mode.upper()
        if not cfg.listen and not cfg.connect and mode == "QUIC":
            raise ValueError(
                "QUIC loopback requires TLS certificate and private-key Zenoh configuration; "
                "use explicit multi-host workers after configuring TLS."
            )
        if not cfg.listen and not cfg.connect and mode == "OTHER":
            raise ValueError("OTHER loopback transport requires explicit Zenoh listen/connect endpoints.")
        return cfg

    def run_loopback(
        self,
        config: BenchConfig | dict,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        cfg = self.validate_loopback_config(config)
        cfg.run_id = cfg.run_id or uuid.uuid4().hex[:12]
        root_run_id = cfg.run_id
        run_dir = self.out_dir / root_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "config.json").write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        started = time.perf_counter()
        start_utc = _utc_now()
        repeat_results = []
        for repeat_index in range(1, cfg.repeats + 1):
            if progress_callback:
                progress_callback(repeat_index, cfg.repeats)
            repeat_cfg = BenchConfig.from_dict(cfg.to_dict())
            repeat_cfg.run_id = f"{root_run_id}-r{repeat_index:02d}"
            repeat_dir = run_dir / f"repeat-{repeat_index:02d}"
            repeat_results.append(self._run_once(repeat_cfg, repeat_dir))

        result = self._aggregate_repeats(
            cfg,
            repeat_results,
            start_utc,
            _utc_now(),
            (time.perf_counter() - started) * 1000,
        )
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def _run_once(self, cfg: BenchConfig, run_dir: Path) -> dict:
        sub_cfgs, pub_cfgs = self._loopback_worker_configs(cfg)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "config.json").write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        worker_specs: list[tuple[str, int, Path, subprocess.Popen]] = []
        started = time.perf_counter()
        start_utc = _utc_now()
        try:
            for index in range(cfg.subscriber_count):
                path = run_dir / f"subscriber-{index}.json"
                process = subprocess.Popen(
                    self.worker_command("subscriber", json.dumps(sub_cfgs[index].to_dict()), index, str(path))
                )
                worker_specs.append(("subscriber", index, path, process))
            time.sleep(max(0.5, cfg.warmup_seconds))
            for index in range(cfg.publisher_count):
                path = run_dir / f"publisher-{index}.json"
                process = subprocess.Popen(
                    self.worker_command("publisher", json.dumps(pub_cfgs[index].to_dict()), index, str(path))
                )
                worker_specs.append(("publisher", index, path, process))

            recovery_wait = cfg.recovery_timeout_seconds if cfg.scenario_name == "S11" else 0.0
            timeout = cfg.warmup_seconds + cfg.duration_seconds + cfg.drain_seconds + recovery_wait + 15
            deadline = time.monotonic() + timeout
            timed_out = []
            for role, index, _path, process in worker_specs:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out.append(f"{role}[{index}]")
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
            if timed_out:
                raise RuntimeError(f"worker timeout: {', '.join(timed_out)}")
        finally:
            for _role, _index, _path, process in worker_specs:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

        failed = [
            f"{role}[{index}] exit={process.returncode}"
            for role, index, _path, process in worker_specs
            if process.returncode != 0
        ]
        if failed:
            raise RuntimeError(f"worker process failed: {', '.join(failed)}")

        fragments = []
        invalid = []
        for role, index, path, _process in worker_specs:
            try:
                fragment = json.loads(path.read_text(encoding="utf-8"))
                if fragment.get("role") != role:
                    raise ValueError(f"expected role {role!r}")
                fragments.append(fragment)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                invalid.append(f"{role}[{index}]: {exc}")
        if invalid:
            raise RuntimeError(f"worker result missing or invalid: {'; '.join(invalid)}")

        result = self.aggregate(cfg, fragments)
        result["test_start_time"] = start_utc
        result["test_end_time"] = _utc_now()
        result["metrics"]["harness_elapsed_ms"] = (time.perf_counter() - started) * 1000
        result = round_numbers(result)
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def _loopback_worker_configs(self, cfg: BenchConfig) -> tuple[list[BenchConfig], list[BenchConfig]]:
        sub_cfgs = [BenchConfig.from_dict(cfg.to_dict()) for _ in range(cfg.subscriber_count)]
        pub_cfgs = [BenchConfig.from_dict(cfg.to_dict()) for _ in range(cfg.publisher_count)]
        if not cfg.listen and not cfg.connect:
            scheme = TRANSPORT_SCHEMES[cfg.transport_mode.upper()]
            endpoints = [f"{scheme}/127.0.0.1:{_free_port(scheme)}" for _ in sub_cfgs]
            for index, sub_cfg in enumerate(sub_cfgs):
                sub_cfg.listen = [endpoints[index]]
                sub_cfg.connect = []
            for pub_cfg in pub_cfgs:
                pub_cfg.listen = []
                pub_cfg.connect = endpoints
            return sub_cfgs, pub_cfgs

        # Only one local process can own a configured listener. Other local
        # workers connect to that peer or to the explicitly configured router.
        local_connect = list(cfg.connect) or [_local_connect_endpoint(x) for x in cfg.listen]
        for index, sub_cfg in enumerate(sub_cfgs):
            sub_cfg.listen = list(cfg.listen) if index == 0 else []
            sub_cfg.connect = list(cfg.connect) if index == 0 else local_connect
        for pub_cfg in pub_cfgs:
            pub_cfg.listen = []
            pub_cfg.connect = local_connect
        return sub_cfgs, pub_cfgs

    def aggregate(self, cfg: BenchConfig | dict, fragments: list[dict]) -> dict:
        cfg = cfg if isinstance(cfg, BenchConfig) else BenchConfig.from_dict(cfg)
        pubs = [x for x in fragments if x.get("role") == "publisher"]
        subs = [x for x in fragments if x.get("role") == "subscriber"]
        sent = sum(x.get("sent_count", 0) for x in pubs)
        received = sum(x.get("valid_unique_count", 0) for x in subs)
        expected = sent * (cfg.expected_subscribers or cfg.subscriber_count)
        samples = [sample for sub in subs for sample in sub.get("samples", [])]
        delivery_matrix = []
        for publisher_id in range(cfg.publisher_count):
            for subscriber_id in range(cfg.subscriber_count):
                delivered = sum(
                    sample.get("publisher_id") == publisher_id
                    and sample.get("subscriber_id") == subscriber_id
                    and sample.get("valid")
                    for sample in samples
                )
                delivery_matrix.append(
                    {
                        "publisher_id": publisher_id,
                        "subscriber_id": subscriber_id,
                        "sent": sum(
                            pub.get("sent_count", 0)
                            for pub in pubs
                            if pub.get("publisher_id") == publisher_id
                        ),
                        "received": delivered,
                    }
                )
        receive_windows = [x["receive_window_seconds"] for x in subs if x.get("receive_window_seconds")]
        window = max(receive_windows) if receive_windows else None
        metrics = latency_metrics(samples)
        metrics.update(
            {
                "throughput_mbps": received * cfg.payload_size_bytes * 8 / window / 1e6 if window else None,
                "offered_throughput_mbps": sum(x.get("offered_throughput_mbps", 0) for x in pubs) or None,
                "cpu_percent": (
                    sum(x.get("resources", {}).get("cpu_percent", 0) for x in fragments)
                    if fragments
                    else None
                ),
                "memory_mb": (
                    sum(x.get("resources", {}).get("memory_mb", 0) for x in fragments)
                    if fragments
                    else None
                ),
                "packet_loss": (expected - received) / expected if expected else None,
                "final_packet_loss": (expected - received) / expected if expected else None,
                "startup_time_ms": max((x.get("startup_time_ms", 0) for x in fragments), default=None),
                "discovery_time_ms": max(
                    (x.get("discovery_time_ms", 0) for x in subs if x.get("discovery_time_ms") is not None),
                    default=None,
                ),
            }
        )
        publish_errors = sum(x.get("error_count", 0) for x in pubs)
        shutdown_errors = [
            {"role": fragment.get("role"), "message": message}
            for fragment in fragments
            for message in fragment.get("shutdown_errors", [])
        ]
        result = {
            "schema_version": "1.0",
            "metrics_definition_version": METRICS_VERSION,
            "module_name": cfg.module_name,
            "module_version": _zenoh_version(),
            "scenario_name": cfg.scenario_name,
            "config": cfg.to_dict(),
            "comparison_key": {
                key: cfg.to_dict()[key]
                for key in (
                    "payload_size_bytes",
                    "publish_rate_hz",
                    "publisher_count",
                    "subscriber_count",
                    "transport_mode",
                    "qos_profile",
                    "network_profile",
                )
            },
            "environment": {
                "controller_host": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "resource_scope": "publisher/subscriber processes",
            },
            "counts": {
                "sent": sent,
                "emitted": sum(x.get("emitted_count", x.get("sent_count", 0)) for x in pubs),
                "received": received,
                "expected": expected,
                "missing": max(0, expected - received),
                "valid_samples": len(samples),
                "duplicates": sum(x.get("duplicate_count", 0) for x in subs),
                "out_of_order": sum(x.get("out_of_order_count", 0) for x in subs),
                "corrupt": sum(x.get("corrupt_count", 0) for x in subs),
                "size_mismatch": sum(x.get("size_mismatch_count", 0) for x in subs),
                "recovered": sum(x.get("recovered_count", 0) for x in fragments),
                "publish_errors": publish_errors,
                "injected_loss": sum(x.get("injected_loss_count", 0) for x in pubs),
                "injected_delay_ms": sum(x.get("injected_delay_ms", 0) for x in pubs),
            },
            "metrics": metrics,
            "discovery_supported": True,
            "test_start_time": _utc_now(),
            "test_end_time": _utc_now(),
            "network_injected": cfg.to_dict()["network_profile"],
            "actual_payload_size_bytes": cfg.payload_size_bytes,
            "wire_message_size_bytes": next(
                (
                    fragment.get("wire_message_size_bytes")
                    for fragment in fragments
                    if fragment.get("wire_message_size_bytes") is not None
                ),
                None,
            ),
            "delivery_matrix": delivery_matrix,
            "errors": (
                ([{"type": "publisher_error", "count": publish_errors}] if publish_errors else [])
                + [{"type": "shutdown_warning", **error} for error in shutdown_errors]
            ),
            "nodes": [{key: value for key, value in x.items() if key != "samples"} for x in fragments],
            "samples": samples,
        }
        return round_numbers(result)

    def _aggregate_repeats(
        self,
        cfg: BenchConfig,
        repeat_results: list[dict],
        start_utc: str,
        end_utc: str,
        harness_elapsed_ms: float,
    ) -> dict:
        all_samples = [
            {"repeat": repeat_index, **sample}
            for repeat_index, result in enumerate(repeat_results, 1)
            for sample in result.get("samples", [])
        ]
        metric_names = {name for result in repeat_results for name in result.get("metrics", {})}
        metrics = {
            name: _mean([result.get("metrics", {}).get(name) for result in repeat_results])
            for name in metric_names
        }
        metrics.update(latency_metrics(all_samples))
        metrics["harness_elapsed_ms"] = harness_elapsed_ms

        count_names = {name for result in repeat_results for name in result.get("counts", {})}
        counts = {
            name: sum(result.get("counts", {}).get(name, 0) for result in repeat_results)
            for name in count_names
        }
        errors = []
        nodes = []
        summaries = []
        for index, result in enumerate(repeat_results, 1):
            errors.extend({"repeat": index, **error} for error in result.get("errors", []))
            nodes.extend({"repeat": index, **node} for node in result.get("nodes", []))
            summaries.append(
                {
                    "repeat": index,
                    "run_id": result.get("config", {}).get("run_id"),
                    "test_start_time": result.get("test_start_time"),
                    "test_end_time": result.get("test_end_time"),
                    "counts": result.get("counts", {}),
                    "metrics": result.get("metrics", {}),
                    "errors": result.get("errors", []),
                }
            )

        base = repeat_results[0]
        result = {
            "schema_version": "1.0",
            "metrics_definition_version": METRICS_VERSION,
            "module_name": cfg.module_name,
            "module_version": base.get("module_version", _zenoh_version()),
            "scenario_name": cfg.scenario_name,
            "config": cfg.to_dict(),
            "comparison_key": base.get("comparison_key", {}),
            "environment": {**base.get("environment", {}), "repeat_count": len(repeat_results)},
            "counts": counts,
            "metrics": metrics,
            "discovery_supported": all(x.get("discovery_supported", False) for x in repeat_results),
            "test_start_time": start_utc,
            "test_end_time": end_utc,
            "network_injected": base.get("network_injected", cfg.to_dict()["network_profile"]),
            "actual_payload_size_bytes": cfg.payload_size_bytes,
            "wire_message_size_bytes": base.get("wire_message_size_bytes"),
            "delivery_matrix": [
                {
                    "publisher_id": publisher_id,
                    "subscriber_id": subscriber_id,
                    "sent": sum(
                        next(
                            (
                                link.get("sent", 0)
                                for link in repeat.get("delivery_matrix", [])
                                if link.get("publisher_id") == publisher_id
                                and link.get("subscriber_id") == subscriber_id
                            ),
                            0,
                        )
                        for repeat in repeat_results
                    ),
                    "received": sum(
                        next(
                            (
                                link.get("received", 0)
                                for link in repeat.get("delivery_matrix", [])
                                if link.get("publisher_id") == publisher_id
                                and link.get("subscriber_id") == subscriber_id
                            ),
                            0,
                        )
                        for repeat in repeat_results
                    ),
                }
                for publisher_id in range(cfg.publisher_count)
                for subscriber_id in range(cfg.subscriber_count)
            ],
            "errors": errors,
            "nodes": nodes,
            "samples": all_samples,
            "repeats_requested": cfg.repeats,
            "repeats_completed": len(repeat_results),
            "repeat_results": summaries,
        }
        return round_numbers(result)


def _local_connect_endpoint(endpoint: str) -> str:
    return endpoint.replace("/0.0.0.0:", "/127.0.0.1:").replace("/[::]:", "/[::1]:")


def _free_port(scheme: str) -> int:
    socket_type = socket.SOCK_DGRAM if scheme == "udp" else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, socket_type) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
