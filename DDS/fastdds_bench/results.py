"""结果文档形状（套件级 result.json 与单轮 runs/<run_id>.json）。

本模块只使用标准库，因此"环境未就绪"的降级路径（contract/编排层）也能复用
同一套文档结构，保证 `status="not_tested"` 与真实测量的字段名完全一致。
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from . import (
    DISCOVERY_READY_CONDITION,
    METRICS_DEFINITION_VERSION,
    MIDDLEWARE_ID,
    RECOVERY_SEMANTICS,
    RESOURCE_SCOPE,
    SCHEMA_VERSION,
    VENDOR,
)
from .metrics import STANDARD_METRIC_FIELDS
from .util import utc_now_iso

VALID_STATUS = "completed"

# 用于横向比较的分组字段（UNIFIED_CONTRACT §6）：口径一致才允许同组平均。
GROUP_FIELDS = (
    "middleware_id",
    "scenario_id",
    "scenario_name",
    "condition_id",
    "payload_size_bytes",
    "publish_rate_hz",
    "publisher_count",
    "subscriber_count",
    "transport_mode",
    "qos_profile",
    "network_profile",
)

# 根 README §6 要求的单轮字段。缺测必须是 null，禁止写 0 或 "N/A"。
UNIFIED_RUN_FIELDS = (
    "run_id",
    "middleware_id",
    "middleware_version",
    "scenario_name",
    "scenario_title",
    "repeat",
    "status",
    "payload_size_bytes",
    "publish_rate_hz",
    "publisher_count",
    "subscriber_count",
    "messages_sent",
    "messages_received",
    "unique_deliveries",
    "expected_deliveries",
    "achieved_publish_rate_hz",
    "latency_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "latency_std_ms",
    "throughput_mbps",
    "jitter_ms",
    "packet_loss",
    "final_packet_loss",
    "duplicate_count",
    "out_of_order_count",
    "corrupted_count",
    "startup_time_ms",
    "discovery_time_ms",
    "recovery_time_ms",
    "cpu_percent",
    "memory_mb",
    "latency_sample_count",
    "configuration",
    "statistics",
    "link_metrics",
    "environment",
    "limitations",
)

# 与传输/QoS/资源口径相关的固定限制说明（不伪造数据声明见 README）。
STATIC_LIMITATIONS = (
    "transport_mode 是映射值：udp = 显式 UDPv4 descriptor（禁用 builtin transports 与 Data Sharing），"
    "shm = 显式 SHM descriptor；不同 transport_mode 的结果不得同组平均",
    "DDS 的 RELIABLE 是协议级重传，不等价于其他中间件的应用层恢复",
    f"资源口径：{RESOURCE_SCOPE}；DDS 没有独立的 Broker/Router/发现服务进程",
    f"发现完成定义：{DISCOVERY_READY_CONDITION}",
    "单向延迟使用 time.perf_counter_ns()，要求发布者与订阅者同机；不可跨主机直接比较",
    "缺失/未测量/不适用的指标一律写 null，不写 0，也不插值或生成数据",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _numeric_summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "std_population": statistics.pstdev(values),
    }


def rate_calibration(condition: dict[str, Any], achieved_rate_hz: float | None) -> dict[str, Any]:
    """S03/S04 的速率口径说明：只报告实测达成速率，不声称全局最大稳定速率。"""
    configured = _number(condition.get("publish_rate_hz")) or 0.0
    unpaced = configured <= 0
    return {
        "configured_rate_hz": configured,
        "mode": "unpaced" if unpaced else "paced",
        "selected_rate_hz": configured if not unpaced else achieved_rate_hz,
        "achieved_rate_hz": achieved_rate_hz,
        "rate_scope": "per_publisher",
        "calibration_scan": [],
        "claims_global_maximum": False,
        "note": (
            "该条件不限速（publish_rate_hz=0），selected_rate_hz 是实测达成速率，"
            "不是经过候选扫描标定的全局最大稳定速率；如需标定需要额外的候选扫描轮次"
            if unpaced
            else "该条件按配置速率限速，selected_rate_hz 即配置值"
        ),
    }


def map_engine_run(
    engine_run: dict[str, Any],
    *,
    environment: dict[str, Any] | None = None,
    samples_path: str | None = None,
) -> dict[str, Any]:
    """把引擎单轮结果映射成根 README §6 的统一单轮形状。"""
    condition = dict(engine_run.get("effective_config") or {})
    sent = int(engine_run.get("sent_success_count") or 0)
    received = int(engine_run.get("received_before_count") or 0)
    unique = int(engine_run.get("valid_unique_delivery_count") or 0)
    expected = int(engine_run.get("expected_delivery_count") or 0)
    publisher_count = max(1, int(condition.get("publisher_count") or 1))
    window = _number(engine_run.get("send_window_seconds"))
    achieved = sent / window / publisher_count if window and window > 0 and sent else None

    limitations = list(STATIC_LIMITATIONS)
    warnings = list(engine_run.get("warnings") or [])
    errors = list(engine_run.get("errors") or [])
    limitations.extend(warnings)
    if samples_path:
        limitations.append("延迟曲线样本来自 output/artifacts/<run_id>/subscriber-0.result.json 的真实接收记录")

    run: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metrics_definition_version": METRICS_DEFINITION_VERSION,
        "adapter_version": engine_run.get("adapter_version"),
        "run_id": engine_run.get("run_id"),
        "suite_id": engine_run.get("suite_id"),
        "middleware_id": MIDDLEWARE_ID,
        "middleware_label": "DDS",
        "middleware_version": engine_run.get("module_version"),
        "vendor": VENDOR,
        "measurement_engine": engine_run.get("measurement_engine"),
        "scenario_id": engine_run.get("scenario_id"),
        "scenario_name": engine_run.get("scenario_name"),
        "scenario_title": engine_run.get("scenario_title"),
        "condition_id": engine_run.get("condition_id"),
        "case": engine_run.get("condition_id"),
        "repeat": engine_run.get("repeat_index"),
        "status": VALID_STATUS if engine_run.get("status") == "completed" else "error",
        "payload_size_bytes": condition.get("payload_size_bytes"),
        "publish_rate_hz": condition.get("publish_rate_hz"),
        "publisher_count": condition.get("publisher_count"),
        "subscriber_count": condition.get("subscriber_count"),
        "messages_sent": sent,
        "messages_received": received,
        "unique_deliveries": unique,
        "expected_deliveries": expected,
        "achieved_publish_rate_hz": achieved,
        "latency_ms": _number(engine_run.get("latency_ms")),
        "latency_p95_ms": _number(engine_run.get("latency_p95_ms")),
        "latency_p99_ms": _number(engine_run.get("latency_p99_ms")),
        "latency_std_ms": _number(engine_run.get("latency_std_ms")),
        "throughput_mbps": _number(engine_run.get("throughput_mbps")),
        "jitter_ms": _number(engine_run.get("jitter_ms")),
        "packet_loss": _number(engine_run.get("packet_loss")),
        "final_packet_loss": _number(engine_run.get("final_packet_loss")),
        "duplicate_count": engine_run.get("duplicate_count"),
        "out_of_order_count": engine_run.get("out_of_order_count"),
        "corrupted_count": engine_run.get("corrupted_count"),
        "startup_time_ms": _number(engine_run.get("startup_time_ms")),
        "discovery_time_ms": _number(engine_run.get("discovery_time_ms")),
        "recovery_time_ms": _number(engine_run.get("recovery_time_ms")),
        "cpu_percent": _number(engine_run.get("cpu_percent")),
        "memory_mb": _number(engine_run.get("memory_mb")),
        "latency_sample_count": int(engine_run.get("latency_sample_count") or 0),
        "test_start_time": engine_run.get("test_start_time"),
        "test_end_time": engine_run.get("test_end_time"),
        "configuration": condition,
        "statistics": {
            "latency_ms": _numeric_summary(
                [v for v in [_number(engine_run.get("latency_ms"))] if v is not None]
            ),
            "jitter_ms": _numeric_summary(
                [v for v in [_number(engine_run.get("jitter_ms"))] if v is not None]
            ),
            "latency_sample_count": int(engine_run.get("latency_sample_count") or 0),
            "jitter_sample_count": int(engine_run.get("jitter_sample_count") or 0),
            "latency_drift_ms": _number(engine_run.get("latency_drift_ms")),
            "memory_growth_mb": _number(engine_run.get("memory_growth_mb")),
            "offered_throughput_mbps": _number(engine_run.get("offered_throughput_mbps")),
            "send_window_seconds": _number(engine_run.get("send_window_seconds")),
            "delivery_window_seconds": _number(engine_run.get("delivery_window_seconds")),
            "sent_success_count": int(engine_run.get("sent_success_count") or 0),
            "sent_failure_count": int(engine_run.get("sent_failure_count") or 0),
            "expected_delivery_count": expected,
            "received_before_count": received,
            "valid_unique_delivery_count": unique,
            "receive_attempt_count": int(engine_run.get("receive_attempt_count") or 0),
            "recovered_count": int(engine_run.get("recovered_count") or 0),
            "malformed_send_count": int(engine_run.get("malformed_send_count") or 0),
            "unexpected_count": int(engine_run.get("unexpected_count") or 0),
            "clock_anomaly_count": int(engine_run.get("clock_anomaly_count") or 0),
            "metadata_mismatch_count": int(engine_run.get("metadata_mismatch_count") or 0),
            "duplicate_send_count": int(engine_run.get("duplicate_send_count") or 0),
            "per_link": engine_run.get("per_link") or [],
        },
        "link_metrics": engine_run.get("per_link") or [],
        "environment": environment or {},
        "limitations": limitations,
        "errors": errors,
        # DDS 特有的语义声明（DDS_readme_1.md §5.5 / §6.5）
        "transport_mode": condition.get("transport_mode"),
        "transport_detail": engine_run.get("transport_detail"),
        "qos_profile": condition.get("qos_profile"),
        "network_profile": condition.get("network_profile"),
        "network_injected_packet_loss": _number(engine_run.get("network_injected_packet_loss")),
        "rate_scope": "per_publisher",
        "resource_scope": RESOURCE_SCOPE,
        "discovery_ready_condition": DISCOVERY_READY_CONDITION,
        "recovery_semantics": RECOVERY_SEMANTICS,
        "rate_calibration": rate_calibration(condition, achieved),
        "capability": engine_run.get("capability", "supported"),
        "fault_events": engine_run.get("fault_events") or [],
        "restart_count": int(engine_run.get("restart_count") or 0),
        "samples_path": samples_path,
        "artifacts_directory": engine_run.get("artifacts_directory"),
        "artifacts": engine_run.get("artifacts") or {},
        "timing_details": engine_run.get("timing_details") or {},
        "resource_details": engine_run.get("resource_details") or {},
        "condition_id_repeat_key": f"{engine_run.get('condition_id')}#{engine_run.get('repeat_index')}",
        "planned_repeats": int(condition.get("repeats") or 1),
        "case_repeats": int(condition.get("case_repeats") or condition.get("repeats") or 1),
        "random_seed": condition.get("random_seed"),
        "warmup_seconds": condition.get("warmup_seconds"),
        "drain_seconds": condition.get("drain_seconds"),
        "duration_seconds": condition.get("duration_seconds"),
        "message_count": condition.get("message_count"),
        "timeout_seconds": condition.get("timeout_seconds"),
    }
    for field in UNIFIED_RUN_FIELDS:
        run.setdefault(field, None)
    return run


def not_tested_run(
    case: dict[str, Any],
    *,
    repeat: int,
    run_id: str,
    suite_id: str,
    status: str,
    reason: str,
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """环境未就绪或能力不足时的单轮结果：全部指标为 null，并写明原因。"""
    limitations = [reason, *STATIC_LIMITATIONS]
    run: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metrics_definition_version": METRICS_DEFINITION_VERSION,
        "adapter_version": case.get("adapter_version"),
        "run_id": run_id,
        "suite_id": suite_id,
        "middleware_id": MIDDLEWARE_ID,
        "middleware_label": "DDS",
        "middleware_version": case.get("middleware_version"),
        "vendor": VENDOR,
        "measurement_engine": None,
        "scenario_id": case.get("scenario_id") or case.get("scenario_name"),
        "scenario_name": case.get("case") or case.get("scenario_name"),
        "scenario_title": case.get("scenario_title") or case.get("title"),
        "condition_id": case.get("condition_id"),
        "case": case.get("condition_id"),
        "repeat": repeat,
        "status": status,
        "payload_size_bytes": case.get("payload_size_bytes"),
        "publish_rate_hz": case.get("publish_rate_hz"),
        "publisher_count": case.get("publisher_count"),
        "subscriber_count": case.get("subscriber_count"),
        "messages_sent": None,
        "messages_received": None,
        "unique_deliveries": None,
        "expected_deliveries": None,
        "achieved_publish_rate_hz": None,
        "latency_ms": None,
        "latency_p95_ms": None,
        "latency_p99_ms": None,
        "latency_std_ms": None,
        "throughput_mbps": None,
        "jitter_ms": None,
        "packet_loss": None,
        "final_packet_loss": None,
        "duplicate_count": None,
        "out_of_order_count": None,
        "corrupted_count": None,
        "startup_time_ms": None,
        "discovery_time_ms": None,
        "recovery_time_ms": None,
        "cpu_percent": None,
        "memory_mb": None,
        "latency_sample_count": None,
        "test_start_time": utc_now_iso(),
        "test_end_time": utc_now_iso(),
        "configuration": dict(case),
        "statistics": {},
        "link_metrics": [],
        "environment": environment or {},
        "limitations": limitations,
        "errors": [reason],
        "transport_mode": case.get("transport_mode"),
        "transport_detail": case.get("transport_detail"),
        "qos_profile": case.get("qos_profile"),
        "network_profile": case.get("network_profile"),
        "network_injected_packet_loss": case.get("network_loss_rate"),
        "rate_scope": "per_publisher",
        "resource_scope": RESOURCE_SCOPE,
        "discovery_ready_condition": DISCOVERY_READY_CONDITION,
        "recovery_semantics": RECOVERY_SEMANTICS,
        "rate_calibration": rate_calibration(case, None),
        "capability": case.get("capability", "supported"),
        "fault_events": [],
        "restart_count": 0,
        "samples_path": None,
        "artifacts_directory": None,
        "artifacts": {},
        "timing_details": {},
        "resource_details": {},
        "condition_id_repeat_key": f"{case.get('condition_id')}#{repeat}",
        "planned_repeats": int(case.get("case_repeats") or 1),
        "case_repeats": int(case.get("case_repeats") or 1),
        "random_seed": case.get("random_seed"),
        "warmup_seconds": case.get("warmup_seconds"),
        "drain_seconds": case.get("drain_seconds"),
        "duration_seconds": case.get("duration_seconds"),
        "message_count": case.get("message_count"),
        "timeout_seconds": case.get("timeout_seconds"),
    }
    for field in UNIFIED_RUN_FIELDS:
        run.setdefault(field, None)
    return run


def sample_document(
    latencies_ms: list[float],
    *,
    run_id: str,
    condition_id: str | None = None,
    subscriber_id: int = 0,
) -> dict[str, Any]:
    """`output/artifacts/<run_id>/subscriber-0.result.json` 的内容。"""
    return {
        "run_id": run_id,
        "condition_id": condition_id,
        "subscriber_id": subscriber_id,
        "unit": "ms",
        "clock": "time.perf_counter_ns (monotonic, same host)",
        "ordering": "arrival order (raw receive record order), de-duplicated per (publisher_id, sequence_number)",
        "latencies_ms": [round(float(value), 6) for value in latencies_ms],
        "sample_count": len(latencies_ms),
        "source": "real subscriber receive records; no interpolation and no generated data",
    }


def summarize_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按条件汇总有效重复次数与指标均值（控制台报告页读取）。"""
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[tuple(run.get(field) for field in GROUP_FIELDS)].append(run)

    summaries: list[dict[str, Any]] = []
    for key, group in grouped.items():
        group_key = dict(zip(GROUP_FIELDS, key, strict=True))
        valid = [run for run in group if run.get("status") == VALID_STATUS]
        metrics: dict[str, Any] = {}
        row: dict[str, Any] = {
            "middleware_id": group_key.get("middleware_id") or MIDDLEWARE_ID,
            "scenario_id": group_key.get("scenario_id"),
            "scenario_name": group_key.get("scenario_id") or group_key.get("scenario_name"),
            "scenario_title": next(
                (run.get("scenario_title") for run in group if run.get("scenario_title")),
                None,
            ),
            "condition_id": group_key.get("condition_id"),
            "condition_ids": sorted(
                {str(run.get("condition_id")) for run in group if run.get("condition_id")}
            ),
            "group_key": group_key,
            "attempted_repeats": len(group),
            "repeat_count": len(valid),
            "valid_repeats": len(valid),
            "formal_minimum_met": len(valid) >= 5,
            "status": next(
                (
                    run.get("status")
                    for run in reversed(group)
                    if run.get("status") != VALID_STATUS
                ),
                VALID_STATUS,
            ),
            "capability": next(
                (run.get("capability") for run in group if run.get("capability")),
                "supported",
            ),
        }
        for field in STANDARD_METRIC_FIELDS:
            values = [
                float(run[field])
                for run in valid
                if isinstance(run.get(field), (int, float))
                and not isinstance(run.get(field), bool)
            ]
            metrics[field] = _numeric_summary(values)
            row[field] = _mean(values)
        row["metrics"] = metrics
        summaries.append(row)

    summaries.sort(
        key=lambda item: (
            str(item.get("scenario_id") or ""),
            int(item.get("group_key", {}).get("payload_size_bytes") or 0),
            float(item.get("group_key", {}).get("publish_rate_hz") or 0),
            str(item.get("condition_id") or ""),
        )
    )
    return summaries


def suite_document(
    *,
    run_id: str,
    status: str,
    planned_runs: int,
    test_start_time: str,
    test_end_time: str | None,
    environment: dict[str, Any],
    configuration: dict[str, Any],
    runs: list[dict[str, Any]],
    limitations: list[str],
    middleware_version: str | None = None,
) -> dict[str, Any]:
    """`output/result.json` 的套件级文档。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "metrics_definition_version": METRICS_DEFINITION_VERSION,
        "statistical_level": "suite_level",
        "run_id": run_id,
        "suite_id": run_id,
        "middleware_id": MIDDLEWARE_ID,
        "middleware_label": "DDS",
        "middleware_version": middleware_version,
        "vendor": VENDOR,
        "status": status,
        "planned_runs": planned_runs,
        "test_start_time": test_start_time,
        "test_end_time": test_end_time,
        "environment": environment,
        "configuration": configuration,
        "scenario_summaries": summarize_runs(runs),
        "runs": runs,
        "limitations": limitations,
        "units": {
            "latency_ms": "ms",
            "jitter_ms": "ms",
            "throughput_mbps": "Mbit/s (application payload only)",
            "packet_loss": "0-1 fraction",
            "cpu_percent": "percent of one logical core",
            "memory_mb": "decimal MB (RSS)",
            "startup_time_ms": "ms",
            "discovery_time_ms": "ms",
            "recovery_time_ms": "ms",
        },
    }
