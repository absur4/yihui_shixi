from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .rawio import (
    ALL_VALID_FLAGS,
    SendRecord,
    iter_receive_records,
    iter_send_records,
)

STANDARD_METRIC_FIELDS = (
    "latency_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "latency_std_ms",
    "jitter_ms",
    "throughput_mbps",
    "offered_throughput_mbps",
    "cpu_percent",
    "memory_mb",
    "packet_loss",
    "final_packet_loss",
    "startup_time_ms",
    "discovery_time_ms",
    "recovery_time_ms",
    "latency_drift_ms",
    "memory_growth_mb",
)


def linear_percentile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between 0 and 1")
    h = (len(ordered) - 1) * probability
    lower = math.floor(h)
    upper = math.ceil(h)
    if lower == upper:
        return ordered[lower]
    fraction = h - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def latency_statistics(
    samples_by_link: dict[tuple[int, int], list[tuple[int, float]]]
) -> dict[str, float | int | None]:
    latencies: list[float] = []
    jitter_samples: list[float] = []
    for samples in samples_by_link.values():
        latencies.extend(latency for _, latency in samples)
        ordered = sorted(samples, key=lambda item: item[0])
        jitter_samples.extend(
            abs(ordered[i + 1][1] - ordered[i][1])
            for i in range(len(ordered) - 1)
        )
    if not latencies:
        return {
            "latency_ms": None,
            "latency_p95_ms": None,
            "latency_p99_ms": None,
            "latency_std_ms": None,
            "jitter_ms": None,
            "latency_sample_count": 0,
            "jitter_sample_count": 0,
        }
    return {
        "latency_ms": statistics.fmean(latencies),
        "latency_p95_ms": linear_percentile(latencies, 0.95),
        "latency_p99_ms": linear_percentile(latencies, 0.99),
        "latency_std_ms": statistics.pstdev(latencies),
        "jitter_ms": statistics.fmean(jitter_samples) if jitter_samples else None,
        "latency_sample_count": len(latencies),
        "jitter_sample_count": len(jitter_samples),
    }


def _rate(numerator_bits: int, first_ns: int | None, last_ns: int | None) -> float | None:
    if first_ns is None or last_ns is None or last_ns <= first_ns:
        return None
    seconds = (last_ns - first_ns) / 1_000_000_000.0
    return numerator_bits / seconds / 1_000_000.0


def _loss(expected: int, received: int) -> float | None:
    if expected <= 0:
        return None
    return max(0.0, min(1.0, (expected - received) / expected))


def _raw_path(run_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else run_dir / path


def raw_paths(run_dir: Path, result: dict[str, Any]) -> list[Path]:
    """返回该端点的全部原始记录文件。

    故障重启场景下，同一个逻辑端点在轮次内会产生两个进程，每个进程写自己的
    原始记录，因此结果里用 `raw_files` 列出多份文件（向后兼容 `raw_file`）。
    """
    names = result.get("raw_files") or [result.get("raw_file")]
    return [_raw_path(run_dir, name) for name in names if name]


def analyze_run(
    condition: dict[str, Any],
    run_dir: Path,
    publisher_results: list[dict[str, Any]],
    subscriber_results: list[dict[str, Any]],
    timings: dict[str, int | float | None],
    resources: dict[str, float | int | None],
) -> dict[str, Any]:
    """Compute all standard metrics from immutable per-message observation files."""
    run_dir = Path(run_dir)
    payload_size = int(condition["payload_size_bytes"])
    expected_payload_checksum = int(condition["payload_checksum"])
    send_complete_ns = timings.get("send_complete_ns")

    sent_by_publisher: dict[int, dict[int, SendRecord]] = defaultdict(dict)
    duplicate_send_count = 0
    malformed_send_count = 0
    send_timestamps: list[int] = []
    for result in publisher_results:
        for path in raw_paths(run_dir, result):
            for record in iter_send_records(path):
                publisher_records = sent_by_publisher[record.publisher_id]
                if record.sequence_number in publisher_records:
                    duplicate_send_count += 1
                    continue
                if (
                    record.payload_length != payload_size
                    or record.checksum != expected_payload_checksum
                ):
                    malformed_send_count += 1
                    continue
                publisher_records[record.sequence_number] = record
                send_timestamps.append(record.send_timestamp_ns)

    sent_success_count = sum(len(records) for records in sent_by_publisher.values())
    subscriber_count = int(condition["subscriber_count"])
    expected_delivery_count = sent_success_count * subscriber_count
    receive_attempt_count = 0
    valid_unique_delivery_count = 0
    received_before_count = 0
    duplicate_count = 0
    out_of_order_count = 0
    corrupted_count = 0
    unexpected_count = 0
    clock_anomaly_count = 0
    metadata_mismatch_count = 0

    samples_by_link: dict[tuple[int, int], list[tuple[int, float]]] = defaultdict(list)
    measured_latency_timeline: list[tuple[int, float]] = []
    link_seen: dict[tuple[int, int], set[int]] = defaultdict(set)
    link_max_arrival_sequence: dict[tuple[int, int], int] = {}
    link_last_receive_before_ns: dict[tuple[int, int], int] = {}
    link_before_count: dict[tuple[int, int], int] = defaultdict(int)
    recovered_receive_times: list[int] = []

    for sub_result in subscriber_results:
        subscriber_id = int(sub_result["endpoint_id"])
        snapshot_count = int(sub_result.get("snapshot_record_count", 0))
        arrival_index = 0
        for raw_file in raw_paths(run_dir, sub_result):
            for record in iter_receive_records(raw_file):
                receive_attempt_count += 1
                link = (record.publisher_id, subscriber_id)
                flags_valid = (record.flags & ALL_VALID_FLAGS) == ALL_VALID_FLAGS
                sender_record = sent_by_publisher.get(record.publisher_id, {}).get(
                    record.sequence_number
                )
                if sender_record is None:
                    unexpected_count += 1
                    arrival_index += 1
                    continue
                metadata_matches = (
                    record.send_timestamp_ns == sender_record.send_timestamp_ns
                    and record.declared_payload_length == sender_record.payload_length
                    and record.declared_checksum == sender_record.checksum
                    and record.actual_payload_length == payload_size
                    and record.actual_checksum == expected_payload_checksum
                )
                if not flags_valid or not metadata_matches:
                    corrupted_count += 1
                    if not metadata_matches:
                        metadata_mismatch_count += 1
                    arrival_index += 1
                    continue
                if record.sequence_number in link_seen[link]:
                    duplicate_count += 1
                    arrival_index += 1
                    continue

                previous_max = link_max_arrival_sequence.get(link)
                if previous_max is not None and record.sequence_number < previous_max:
                    out_of_order_count += 1
                link_max_arrival_sequence[link] = max(
                    record.sequence_number,
                    previous_max if previous_max is not None else record.sequence_number,
                )
                link_seen[link].add(record.sequence_number)
                valid_unique_delivery_count += 1
                is_before_boundary = (
                    record.receive_timestamp_ns <= send_complete_ns
                    if isinstance(send_complete_ns, int)
                    else arrival_index < snapshot_count
                )
                arrival_index += 1
                if is_before_boundary:
                    received_before_count += 1
                    link_before_count[link] += 1
                    link_last_receive_before_ns[link] = max(
                        link_last_receive_before_ns.get(link, 0),
                        record.receive_timestamp_ns,
                    )
                else:
                    recovered_receive_times.append(record.receive_timestamp_ns)

                latency_ns = record.receive_timestamp_ns - record.send_timestamp_ns
                if latency_ns < 0:
                    clock_anomaly_count += 1
                    continue
                # The requirements explicitly exclude recovery/drain arrivals from
                # the latency and throughput sample sets. They still contribute to
                # final delivery/loss and recovery statistics above.
                if is_before_boundary:
                    latency_ms = latency_ns / 1_000_000.0
                    samples_by_link[link].append((record.sequence_number, latency_ms))
                    measured_latency_timeline.append(
                        (record.send_timestamp_ns, latency_ms)
                    )

    latency = latency_statistics(samples_by_link)
    first_send_ns = min(send_timestamps) if send_timestamps else None
    last_send_ns = max(send_timestamps) if send_timestamps else None
    last_receive_before_ns = (
        max(link_last_receive_before_ns.values())
        if link_last_receive_before_ns
        else None
    )

    per_link: list[dict[str, Any]] = []
    for publisher_id in range(int(condition["publisher_count"])):
        expected_for_link = len(sent_by_publisher.get(publisher_id, {}))
        pub_send_times = [
            record.send_timestamp_ns
            for record in sent_by_publisher.get(publisher_id, {}).values()
        ]
        first_pub_send = min(pub_send_times) if pub_send_times else None
        for subscriber_id in range(subscriber_count):
            link = (publisher_id, subscriber_id)
            link_samples = {link: samples_by_link.get(link, [])}
            link_stats = latency_statistics(link_samples)
            received = len(link_seen.get(link, set()))
            per_link.append(
                {
                    "publisher_id": publisher_id,
                    "subscriber_id": subscriber_id,
                    "expected_delivery_count": expected_for_link,
                    "received_before_count": link_before_count.get(link, 0),
                    "valid_unique_delivery_count": received,
                    "packet_loss": _loss(
                        expected_for_link, link_before_count.get(link, 0)
                    ),
                    "final_packet_loss": _loss(expected_for_link, received),
                    "throughput_mbps": _rate(
                        link_before_count.get(link, 0) * payload_size * 8,
                        first_pub_send,
                        link_last_receive_before_ns.get(link),
                    ),
                    **link_stats,
                }
            )

    if recovered_receive_times and isinstance(send_complete_ns, int):
        recovery_time_ms: float | None = max(
            0.0, (max(recovered_receive_times) - send_complete_ns) / 1_000_000.0
        )
    elif expected_delivery_count > 0 and received_before_count == expected_delivery_count:
        recovery_time_ms = 0.0
    else:
        recovery_time_ms = None

    latency_drift_ms: float | None = None
    if measured_latency_timeline:
        ordered_timeline = sorted(measured_latency_timeline)
        window_size = max(1, len(ordered_timeline) // 10)
        first_window = [value for _, value in ordered_timeline[:window_size]]
        last_window = [value for _, value in ordered_timeline[-window_size:]]
        latency_drift_ms = statistics.fmean(last_window) - statistics.fmean(
            first_window
        )

    resource_window_ns = timings.get("resource_window_ns")
    cpu_time_seconds = resources.get("cpu_time_seconds")
    if (
        isinstance(resource_window_ns, int)
        and resource_window_ns > 0
        and isinstance(cpu_time_seconds, (int, float))
    ):
        cpu_percent: float | None = (
            float(cpu_time_seconds) / (resource_window_ns / 1_000_000_000.0) * 100.0
        )
    else:
        cpu_percent = None

    network = condition.get("network", {})
    return {
        **latency,
        "throughput_mbps": _rate(
            received_before_count * payload_size * 8,
            first_send_ns,
            last_receive_before_ns,
        ),
        "offered_throughput_mbps": _rate(
            sent_success_count * payload_size * 8,
            first_send_ns,
            last_send_ns,
        ),
        "cpu_percent": cpu_percent,
        "memory_mb": resources.get("peak_rss_bytes") / 1_000_000.0
        if isinstance(resources.get("peak_rss_bytes"), int)
        else None,
        "packet_loss": _loss(expected_delivery_count, received_before_count),
        "final_packet_loss": _loss(
            expected_delivery_count, valid_unique_delivery_count
        ),
        "startup_time_ms": timings.get("startup_time_ms"),
        "discovery_time_ms": timings.get("discovery_time_ms"),
        "recovery_time_ms": recovery_time_ms,
        "latency_drift_ms": latency_drift_ms,
        "memory_growth_mb": (
            (
                int(
                    resources.get("formal_last_rss_bytes")
                    if isinstance(resources.get("formal_last_rss_bytes"), int)
                    else resources["last_rss_bytes"]
                )
                - int(
                    resources.get("formal_first_rss_bytes")
                    if isinstance(resources.get("formal_first_rss_bytes"), int)
                    else resources["first_rss_bytes"]
                )
            )
            / 1_000_000.0
            if isinstance(resources.get("first_rss_bytes"), int)
            and isinstance(resources.get("last_rss_bytes"), int)
            else None
        ),
        "network_injected_packet_loss": float(
            network.get("packet_loss_percent", 0)
        )
        / 100.0,
        "sent_success_count": sent_success_count,
        "sent_failure_count": sum(
            int(result.get("send_failure_count", 0)) for result in publisher_results
        ),
        "expected_delivery_count": expected_delivery_count,
        "received_before_count": received_before_count,
        "valid_unique_delivery_count": valid_unique_delivery_count,
        "receive_attempt_count": receive_attempt_count,
        "duplicate_count": duplicate_count,
        "out_of_order_count": out_of_order_count,
        "corrupted_count": corrupted_count,
        "unexpected_count": unexpected_count,
        "clock_anomaly_count": clock_anomaly_count,
        "metadata_mismatch_count": metadata_mismatch_count,
        "duplicate_send_count": duplicate_send_count,
        "malformed_send_count": malformed_send_count,
        "recovered_count": max(0, valid_unique_delivery_count - received_before_count),
        "send_window_seconds": (
            (last_send_ns - first_send_ns) / 1_000_000_000.0
            if first_send_ns is not None
            and last_send_ns is not None
            and last_send_ns >= first_send_ns
            else None
        ),
        "delivery_window_seconds": (
            (last_receive_before_ns - first_send_ns) / 1_000_000_000.0
            if first_send_ns is not None
            and last_receive_before_ns is not None
            and last_receive_before_ns >= first_send_ns
            else None
        ),
        "per_link": per_link,
    }


def subscriber_latency_samples(
    condition: dict[str, Any],
    run_dir: Path,
    publisher_results: list[dict[str, Any]],
    subscriber_result: dict[str, Any],
) -> list[float]:
    """按到达顺序返回某个订阅者被校验过的真实延迟样本（ms）。

    前端延迟曲线依赖这个列表，所以这里只做真实的读取与校验：不插值、不排序、
    不随机生成。校验规则与 :func:`analyze_run` 完全一致（DDS 声明有效、payload
    可解码、长度与 CRC32 正确、且能在发送端原始记录里按
    (publisher_id, sequence_number) 找到同一条消息）。
    """
    run_dir = Path(run_dir)
    payload_size = int(condition["payload_size_bytes"])
    expected_checksum = int(condition["payload_checksum"])

    sent: dict[int, dict[int, SendRecord]] = defaultdict(dict)
    for result in publisher_results:
        for path in raw_paths(run_dir, result):
            for record in iter_send_records(path):
                if (
                    record.payload_length != payload_size
                    or record.checksum != expected_checksum
                ):
                    continue
                sent[record.publisher_id].setdefault(record.sequence_number, record)

    seen: set[tuple[int, int]] = set()
    samples: list[float] = []
    for path in raw_paths(run_dir, subscriber_result):
        for record in iter_receive_records(path):
            if (record.flags & ALL_VALID_FLAGS) != ALL_VALID_FLAGS:
                continue
            sender = sent.get(record.publisher_id, {}).get(record.sequence_number)
            if sender is None:
                continue
            if (
                record.send_timestamp_ns != sender.send_timestamp_ns
                or record.declared_payload_length != sender.payload_length
                or record.declared_checksum != sender.checksum
                or record.actual_payload_length != payload_size
                or record.actual_checksum != expected_checksum
            ):
                continue
            key = (record.publisher_id, record.sequence_number)
            if key in seen:
                continue
            seen.add(key)
            latency_ns = record.receive_timestamp_ns - record.send_timestamp_ns
            if latency_ns < 0:
                continue
            samples.append(latency_ns / 1_000_000.0)
    return samples
