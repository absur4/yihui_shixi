from __future__ import annotations

import json
import os
import platform
import socket
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from statistics import mean
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from .zenoh_bench.config import BenchConfig, PAYLOAD_SIZES
from .zenoh_bench.runner import METRICS_VERSION, ZenohBench
from .zenoh_bench.scenarios import SCENARIOS

try:
    from interfaces import MiddlewareAdapter, ScenarioResult, ScenarioSpec  # type: ignore
except ImportError:
    class MiddlewareAdapter:
        """Fallback protocol used when the host console is not installed."""

    @dataclass(slots=True)
    class ScenarioSpec:
        scenario_name: str
        title: str = ""
        condition_id: str = ""
        case: str = "standard"
        configuration: dict[str, Any] | None = None

        def to_dict(self) -> dict[str, Any]:
            return asdict(self)

    ScenarioResult = dict[str, Any]


INPUT_FIELDS = (
    "scenario_name", "case", "condition_id", "title",
    "payload_size_bytes", "publish_rate_hz", "publisher_count",
    "subscriber_count", "message_count", "duration_seconds", "repeats",
    "random_seed", "warmup_seconds", "drain_seconds", "timeout_seconds",
    "network_delay_ms", "network_jitter_ms", "network_loss_rate",
    "network_profile", "transport_mode", "qos_profile",
)

_NETWORK_CONDITIONS = (
    ("baseline", 0.0, 0.0, 0.0),
    ("light", 5.0, 5.0, 0.01),
    ("moderate", 20.0, 20.0, 0.05),
    ("severe", 50.0, 50.0, 0.10),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _middleware_version() -> str:
    try:
        return version("eclipse-zenoh")
    except PackageNotFoundError:
        return "unknown"


def _number(value: Any, default: float | int | None = None) -> float | int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric configuration value")
    return value


def _scenario_dict(scenario: ScenarioSpec | dict[str, Any]) -> dict[str, Any]:
    if isinstance(scenario, dict):
        return dict(scenario)
    if hasattr(scenario, "to_dict"):
        return dict(scenario.to_dict())
    if hasattr(scenario, "__dict__"):
        return dict(vars(scenario))
    return {name: getattr(scenario, name) for name in INPUT_FIELDS if hasattr(scenario, name)}


def _flat_configuration(scenario: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """Normalize console fields while retaining every actual value in results."""
    raw = dict(scenario.get("configuration") or {})
    raw.update({key: value for key, value in scenario.items() if key in INPUT_FIELDS})
    raw.update(parameters or {})
    network = raw.get("network_profile")
    if isinstance(network, dict):
        raw.setdefault("network_delay_ms", network.get("delay_ms", 0.0))
        raw.setdefault("network_jitter_ms", network.get("jitter_ms", 0.0))
        raw.setdefault("network_loss_rate", float(network.get("loss_percent", 0.0)) / 100.0)
        raw.setdefault("network_profile", network.get("name", "baseline"))
    elif network is None:
        raw["network_profile"] = "baseline"
    raw.setdefault("scenario_name", scenario.get("scenario_name", "S01"))
    raw.setdefault("case", scenario.get("case", "standard"))
    raw.setdefault("condition_id", scenario.get("condition_id", raw["scenario_name"]))
    raw.setdefault("title", scenario.get("title", SCENARIOS.get(raw["scenario_name"], {}).get("title", "")))
    raw.setdefault("payload_size_bytes", SCENARIOS.get(raw["scenario_name"], {}).get("payload_size_bytes", 1024))
    raw.setdefault("publish_rate_hz", SCENARIOS.get(raw["scenario_name"], {}).get("publish_rate_hz", 100.0))
    raw.setdefault("publisher_count", SCENARIOS.get(raw["scenario_name"], {}).get("publisher_count", 1))
    raw.setdefault("subscriber_count", SCENARIOS.get(raw["scenario_name"], {}).get("subscriber_count", 1))
    raw.setdefault("message_count", SCENARIOS.get(raw["scenario_name"], {}).get("message_count", 1000))
    raw.setdefault("duration_seconds", SCENARIOS.get(raw["scenario_name"], {}).get("duration_seconds", 10.0))
    if raw.get("message_count") and raw.get("publish_rate_hz"):
        raw["duration_seconds"] = max(
            float(raw["duration_seconds"] or 0),
            float(raw["message_count"]) / float(raw["publish_rate_hz"]) + 2.0,
        )
    raw.setdefault("repeats", 5)
    raw.setdefault("random_seed", 20260909)
    raw.setdefault("warmup_seconds", 1.0)
    raw.setdefault("drain_seconds", 0.5)
    raw.setdefault("timeout_seconds", 60.0)
    raw.setdefault("network_delay_ms", 0.0)
    raw.setdefault("network_jitter_ms", 0.0)
    raw.setdefault("network_loss_rate", 0.0)
    raw.setdefault("transport_mode", "tcp")
    raw.setdefault("qos_profile", "reliable-block")
    return {key: raw.get(key) for key in INPUT_FIELDS}


def _engine_config(configuration: dict[str, Any], run_id: str) -> BenchConfig:
    loss_rate = float(configuration["network_loss_rate"] or 0.0)
    if not 0 <= loss_rate <= 1:
        raise ValueError("network_loss_rate must be between 0 and 1")
    data = {
        "module_name": "Zenoh",
        "scenario_name": configuration["scenario_name"],
        "payload_size_bytes": int(configuration["payload_size_bytes"]),
        "publish_rate_hz": float(configuration["publish_rate_hz"]),
        "publisher_count": int(configuration["publisher_count"]),
        "subscriber_count": int(configuration["subscriber_count"]),
        "message_count": int(configuration["message_count"]),
        "duration_seconds": float(configuration["duration_seconds"]),
        "repeats": max(5, int(configuration["repeats"])),
        "random_seed": int(configuration["random_seed"]),
        "warmup_seconds": float(configuration["warmup_seconds"]),
        "drain_seconds": float(configuration["drain_seconds"]),
        "transport_mode": str(configuration["transport_mode"]).upper(),
        "qos_profile": str(configuration["qos_profile"]),
        "run_id": run_id,
        "network_profile": {
            "name": str(configuration["network_profile"]),
            "delay_ms": float(configuration["network_delay_ms"] or 0),
            "jitter_ms": float(configuration["network_jitter_ms"] or 0),
            "loss_percent": loss_rate * 100,
        },
    }
    return BenchConfig.from_dict(data)


def _metric(result: dict[str, Any], name: str) -> Any:
    return result.get("metrics", {}).get(name)


def _single_result(
    configuration: dict[str, Any],
    raw: dict[str, Any],
    repeat: int,
    run_id: str,
    status_override: str | None = None,
) -> dict[str, Any]:
    counts = raw.get("counts", {})
    metrics = raw.get("metrics", {})
    status = status_override or "completed"
    scenario_name = configuration["scenario_name"]
    limitations: list[str] = []
    if status_override is None and scenario_name == "S09":
        status = "unsupported"
        limitations.append("The existing backend delays/drops before publish; it does not inject real network loss on Windows.")
    if status_override is None and scenario_name == "S11":
        status = "unsupported"
        limitations.append("Peer/Router/process fault stop and restart are not automated by this adapter.")
    if status_override is None and scenario_name == "S10" and configuration.get("case") != "cold_start":
        status = "not_tested"
        limitations.append("This discovery variant is not automated; no cold-start data is substituted.")
    expected = counts.get("expected")
    sent = counts.get("sent")
    received = counts.get("received")
    samples = raw.get("samples", [])
    achieved = None
    offered = metrics.get("offered_throughput_mbps")
    if offered is not None and configuration["payload_size_bytes"]:
        achieved = float(offered) * 1_000_000 / (float(configuration["payload_size_bytes"]) * 8)
    network = {
        "name": configuration["network_profile"],
        "delay_ms": configuration["network_delay_ms"],
        "jitter_ms": configuration["network_jitter_ms"],
        "loss_rate": configuration["network_loss_rate"],
    }
    test_start = raw.get("test_start_time", _utc_now())
    test_end = raw.get("test_end_time", _utc_now())
    planned = (
        int(configuration["message_count"]) * int(configuration["publisher_count"])
        if configuration.get("message_count") else None
    )
    result = {
        "schema_version": "1.0",
        "metric_definition_version": METRICS_VERSION,
        "metrics_definition_version": METRICS_VERSION,
        "module_name": "Zenoh",
        "run_id": run_id,
        "middleware_id": "zenoh",
        "middleware_version": raw.get("module_version", _middleware_version()),
        "scenario_name": scenario_name,
        "scenario_title": SCENARIOS.get(scenario_name, {}).get("title", configuration.get("title", "")),
        "repeat": repeat,
        "status": status,
        "payload_size_bytes": configuration["payload_size_bytes"],
        "publish_rate_hz": configuration["publish_rate_hz"],
        "publisher_count": configuration["publisher_count"],
        "subscriber_count": configuration["subscriber_count"],
        "messages_sent": sent,
        "messages_received": received,
        "unique_deliveries": received,
        "expected_deliveries": expected,
        "messages_planned": planned,
        "messages_not_sent": max(0, planned - sent) if planned is not None and sent is not None else None,
        "achieved_publish_rate_hz": achieved,
        "latency_ms": _metric(raw, "latency_ms") if status == "completed" else None,
        "latency_p95_ms": _metric(raw, "latency_p95_ms") if status == "completed" else None,
        "latency_p99_ms": _metric(raw, "latency_p99_ms") if status == "completed" else None,
        "latency_std_ms": _metric(raw, "latency_std_ms") if status == "completed" else None,
        "throughput_mbps": _metric(raw, "throughput_mbps") if status == "completed" else None,
        "offered_throughput_mbps": metrics.get("offered_throughput_mbps"),
        "jitter_ms": _metric(raw, "jitter_ms") if status == "completed" else None,
        "packet_loss": _metric(raw, "packet_loss"),
        "final_packet_loss": _metric(raw, "final_packet_loss"),
        "duplicate_count": counts.get("duplicates", 0),
        "out_of_order_count": counts.get("out_of_order", 0),
        "corrupted_count": counts.get("corrupt", 0),
        "startup_time_ms": _metric(raw, "startup_time_ms"),
        "discovery_time_ms": _metric(raw, "discovery_time_ms"),
        "recovery_time_ms": None,
        "application_recovered": None if scenario_name == "S11" else 0,
        "late_native_deliveries": None,
        "injected_network_loss_rate": configuration["network_loss_rate"],
        "cpu_percent": _metric(raw, "cpu_percent"),
        "memory_mb": _metric(raw, "memory_mb"),
        "latency_sample_count": len([sample for sample in samples if sample.get("valid") and sample.get("latency_ms") is not None]),
        "transport_mode": configuration["transport_mode"],
        "qos_profile": configuration["qos_profile"],
        "network_profile": configuration["network_profile"],
        "configuration": dict(configuration),
        "statistics": {
            "counts": counts,
            "metrics": metrics,
            "measurement_level": "sample_level",
            "actual_payload_size_bytes": raw.get("actual_payload_size_bytes"),
            "wire_message_size_bytes": raw.get("wire_message_size_bytes"),
            "offered_throughput_mbps": metrics.get("offered_throughput_mbps"),
            "network_injected": network,
            "measurement_level": "sample_level",
        },
        "actual_payload_size_bytes": raw.get("actual_payload_size_bytes"),
        "wire_message_size_bytes": raw.get("wire_message_size_bytes"),
        "metadata_size_bytes": (
            raw.get("wire_message_size_bytes") - raw.get("actual_payload_size_bytes")
            if raw.get("wire_message_size_bytes") is not None
            and raw.get("actual_payload_size_bytes") is not None else None
        ),
        "link_metrics": raw.get("delivery_matrix", []),
        "environment": {
            **raw.get("environment", {}),
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "resource_scope": "Zenoh publisher/subscriber processes; controller excluded",
            "discovery_ready_condition": "first valid sample received for the subscribed Key Expression",
        },
        "limitations": limitations,
        "discovery_supported": bool(raw.get("discovery_supported", True)),
        "samples": [
            {"sequence": sample.get("sequence"), "latency": sample.get("latency_ms"),
             "publisher_id": sample.get("publisher_id"), "subscriber_id": sample.get("subscriber_id")}
            for sample in samples if sample.get("valid")
        ],
        "measurement_window": {
            "warmup_start": test_start,
            "measurement_start": None,
            "send_end": None,
            "drain_end": test_end,
            "scope": "test harness window; engine does not expose per-phase UTC timestamps",
        },
        "errors": list(raw.get("errors", [])),
        "test_start_time": test_start,
        "test_end_time": test_end,
    }
    return result


def _catalog_case(scenario_name: str, condition_id: str, case: str, **values: Any) -> dict[str, Any]:
    defaults = _flat_configuration({"scenario_name": scenario_name, "condition_id": condition_id, "case": case}, values)
    defaults.update({"scenario_name": scenario_name, "condition_id": condition_id, "case": case,
                     "title": SCENARIOS[scenario_name]["title"]})
    return defaults


def _catalog() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    cases.append(_catalog_case("S01", "S01-100Hz", "standard", payload_size_bytes=1024, publish_rate_hz=100, message_count=10000))
    cases.append(_catalog_case("S01", "S01-1000Hz", "standard", payload_size_bytes=1024, publish_rate_hz=1000, message_count=10000))
    for size in PAYLOAD_SIZES:
        cases.append(_catalog_case("S02", f"S02-{size}", "payload", payload_size_bytes=size, publish_rate_hz=1000, message_count=1000))
    for size in (65536, 262144, 1048576):
        cases.append(_catalog_case("S03", f"S03-{size}", "large_payload", payload_size_bytes=size, publish_rate_hz=0, message_count=0, duration_seconds=10))
    for rate in (100, 1000, 5000, 0):
        cases.append(_catalog_case("S04", f"S04-{rate or 'max'}", "rate", payload_size_bytes=1024, publish_rate_hz=rate, message_count=10000))
    cases.extend([
        _catalog_case("S05", "S05-1P4S", "standard", publisher_count=1, subscriber_count=4, payload_size_bytes=1024, publish_rate_hz=1000, message_count=10000),
        _catalog_case("S06", "S06-4P1S", "standard", publisher_count=4, subscriber_count=1, payload_size_bytes=1024, publish_rate_hz=1000, message_count=10000),
        _catalog_case("S07", "S07-4P4S", "standard", publisher_count=4, subscriber_count=4, payload_size_bytes=1024, publish_rate_hz=1000, message_count=10000),
        _catalog_case("S08", "S08-300s", "long_duration", payload_size_bytes=1024, publish_rate_hz=1000, message_count=0, duration_seconds=300),
    ])
    for name, delay, jitter, loss in _NETWORK_CONDITIONS:
        cases.append(_catalog_case("S09", f"S09-{name}", "network", network_profile=name, network_delay_ms=delay, network_jitter_ms=jitter, network_loss_rate=loss))
    cases.extend([
        _catalog_case("S10", "S10-cold-start", "cold_start", duration_seconds=3),
        _catalog_case("S10", "S10-hot-start", "hot_start", duration_seconds=3),
        _catalog_case("S10", "S10-discovery-failure", "discovery_failure", duration_seconds=3),
        _catalog_case("S10", "S10-discovery-timeout", "discovery_timeout", duration_seconds=3),
        _catalog_case("S11", "S11-process-restart", "process_restart", duration_seconds=30),
        _catalog_case("S12", "S12-checksum-order", "correctness", message_count=10000),
    ])
    return cases


class Adapter(MiddlewareAdapter):
    name = "zenoh"

    def metadata(self) -> dict[str, Any]:
        try:
            import zenoh  # noqa: F401
            available = True
            note = "Real eclipse-zenoh publisher/subscriber measurements are available."
        except ImportError as exc:
            available = False
            note = f"eclipse-zenoh is unavailable: {exc}"
        return {
            "id": self.name,
            "name": self.name,
            "label": "Zenoh",
            "version": _middleware_version(),
            "available": available,
            "transport_options": ["tcp", "udp", "quic", "other"],
            "qos_options": [
                {"value": "reliable-block", "label": "Reliable / block"},
                {"value": "reliable-drop", "label": "Reliable / drop"},
                {"value": "best-effort-drop", "label": "Best effort / drop"},
            ],
            "notes": [note, "S09 requires a real network injector; S11 requires automated peer/router fault control."],
        }

    def catalog(self) -> list[dict[str, Any]]:
        return _catalog()

    def run(self, scenario: ScenarioSpec | dict[str, Any], parameters: dict[str, Any] | None = None) -> ScenarioResult:
        scenario_data = _scenario_dict(scenario)
        configuration = _flat_configuration(scenario_data, parameters or {})
        run_id = str((parameters or {}).get("run_id") or uuid.uuid4().hex[:12])
        if configuration["scenario_name"] in {"S09", "S11"}:
            return _single_result(configuration, {"counts": {}, "metrics": {}, "samples": [], "environment": {}},
                                  int((parameters or {}).get("repeat", 1)), run_id)
        cfg = _engine_config(configuration, run_id)
        artifact_dir = Path((parameters or {}).get("artifact_dir") or (Path.cwd() / ".zenoh-adapter-artifacts" / run_id))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        raw = ZenohBench(artifact_dir)._run_once(cfg, artifact_dir / "engine-run")
        return _single_result(configuration, raw, int((parameters or {}).get("repeat", 1)), run_id)


def create_adapter() -> Adapter:
    return Adapter()


__all__ = ["Adapter", "ScenarioSpec", "ScenarioResult", "create_adapter"]
