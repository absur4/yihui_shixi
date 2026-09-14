from __future__ import annotations

import json
import os
import platform
import random
import socket
import threading
import time
from pathlib import Path

import psutil
import zenoh

from .config import BenchConfig
from .protocol import build_message, parse_message


def _zconfig(cfg: BenchConfig):
    conf = zenoh.Config()
    conf.insert_json5("mode", json.dumps(cfg.mode))
    if cfg.connect:
        conf.insert_json5("connect/endpoints", json.dumps(cfg.connect))
    if cfg.listen:
        conf.insert_json5("listen/endpoints", json.dumps(cfg.listen))
    return conf


class ResourceMeter:
    def __init__(self):
        self.process = psutil.Process()
        self.stop = threading.Event()
        self.cpu_start = 0.0
        self.wall_start = 0.0
        self.rss_peak = 0

    def __enter__(self):
        times = self.process.cpu_times()
        self.cpu_start = times.user + times.system
        self.wall_start = time.perf_counter()
        self.rss_peak = self.process.memory_info().rss

        def sample():
            while not self.stop.wait(.1):
                try:
                    self.rss_peak = max(self.rss_peak, self.process.memory_info().rss)
                except psutil.Error:
                    pass

        self.thread = threading.Thread(target=sample, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set(); self.thread.join(timeout=1)
        times = self.process.cpu_times()
        elapsed = max(time.perf_counter() - self.wall_start, 1e-9)
        return {"cpu_percent": ((times.user + times.system) - self.cpu_start) / elapsed * 100,
                "memory_mb": self.rss_peak / 1_000_000}


def _write_result(path: str, result: dict):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _sleep_seconds(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _message_time_ns(cfg: BenchConfig) -> int:
    """Use monotonic local time or a controller-normalized multi-host clock."""
    if cfg.scheduled_start_epoch_ns is None:
        return time.perf_counter_ns()
    return time.time_ns() - cfg.clock_offset_ns


def _wait_for_scheduled_start(cfg: BenchConfig) -> None:
    if cfg.scheduled_start_epoch_ns is None:
        _sleep_seconds(cfg.warmup_seconds)
        return
    while True:
        remaining = (cfg.scheduled_start_epoch_ns - _message_time_ns(cfg)) / 1e9
        if remaining <= 0:
            return
        _sleep_seconds(min(remaining, 0.1))


def run_publisher(cfg: BenchConfig, publisher_id: int, output: str) -> None:
    rng = random.Random(cfg.random_seed + publisher_id)
    key = f"{cfg.key_prefix}/{cfg.run_id}/data/{publisher_id}"
    session_start = time.perf_counter()
    session = zenoh.open(_zconfig(cfg))
    startup_ms = (time.perf_counter() - session_start) * 1000
    reliable = "best-effort" not in cfg.qos_profile.lower()
    publisher = session.declare_publisher(
        key,
        reliability=zenoh.Reliability.RELIABLE if reliable else zenoh.Reliability.BEST_EFFORT,
        congestion_control=zenoh.CongestionControl.BLOCK if "block" in cfg.qos_profile.lower() else zenoh.CongestionControl.DROP,
    )
    print(json.dumps({"event": "ready", "role": "publisher", "publisher_id": publisher_id}), flush=True)
    _wait_for_scheduled_start(cfg)
    meter = ResourceMeter().__enter__()
    started_utc = time.time_ns()
    start = time.perf_counter_ns()
    attempted = emitted = errors = injected_loss = 0
    injected_delay_ms = 0.0
    deadline_ns = start + int(cfg.duration_seconds * 1e9) if cfg.duration_seconds else None
    next_ns = start
    while True:
        if cfg.message_count and attempted >= cfg.message_count:
            break
        if deadline_ns and time.perf_counter_ns() >= deadline_ns:
            break
        now = _message_time_ns(cfg)
        message = build_message(publisher_id, attempted, now, cfg.payload_size_bytes, cfg.random_seed)
        attempted += 1
        profile = cfg.network_profile
        delay_ms = profile.delay_ms + (rng.uniform(-profile.jitter_ms, profile.jitter_ms) if profile.jitter_ms else 0.0)
        delay_ms = max(0.0, delay_ms)
        if delay_ms:
            _sleep_seconds(delay_ms / 1000)
            injected_delay_ms += delay_ms
        if profile.loss_percent and rng.random() < profile.loss_percent / 100:
            injected_loss += 1
        else:
            try:
                publisher.put(message)
                emitted += 1
            except Exception:
                errors += 1
        if profile.bandwidth_mbps:
            _sleep_seconds(len(message) * 8 / (profile.bandwidth_mbps * 1_000_000))
        if cfg.publish_rate_hz:
            next_ns += int(1e9 / cfg.publish_rate_hz)
            wait = next_ns - time.perf_counter_ns()
            if wait > 1_000_000:
                _sleep_seconds(wait / 1e9)
            while time.perf_counter_ns() < next_ns:
                pass
    send_end = time.perf_counter_ns()
    _sleep_seconds(cfg.drain_seconds)
    resources = meter.close()
    shutdown_errors = []
    try:
        publisher.undeclare()
    except Exception as exc:
        shutdown_errors.append(f"publisher undeclare: {type(exc).__name__}: {exc}")
    try:
        session.close()
    except Exception as exc:
        shutdown_errors.append(f"session close: {type(exc).__name__}: {exc}")
    elapsed = max((send_end - start) / 1e9, 1e-9)
    _write_result(output, {"role": "publisher", "publisher_id": publisher_id, "host": socket.gethostname(),
        "pid": os.getpid(), "platform": platform.platform(), "sent_count": attempted,
        "emitted_count": emitted, "error_count": errors,
        "injected_loss_count": injected_loss, "injected_delay_ms": injected_delay_ms,
        "send_window_seconds": elapsed,
        "offered_throughput_mbps": attempted * cfg.payload_size_bytes * 8 / elapsed / 1e6,
        "emitted_throughput_mbps": emitted * cfg.payload_size_bytes * 8 / elapsed / 1e6,
        "startup_time_ms": startup_ms, "test_start_epoch_ns": started_utc, "resources": resources,
        "actual_payload_size_bytes": cfg.payload_size_bytes,
        "wire_message_size_bytes": len(build_message(0, 0, 0, cfg.payload_size_bytes, cfg.random_seed)),
        "shutdown_errors": shutdown_errors, "config": cfg.to_dict()})


def run_subscriber(cfg: BenchConfig, subscriber_id: int, output: str) -> None:
    samples, seen = [], set()
    counters = {"received_count": 0, "valid_unique_count": 0, "duplicate_count": 0,
                "out_of_order_count": 0, "corrupt_count": 0, "size_mismatch_count": 0}
    last_sequence: dict[int, int] = {}
    lock = threading.Lock()
    first_recv = last_recv = None
    discovery_start = time.perf_counter_ns()
    discovery_time_ms = None

    def on_sample(sample):
        nonlocal first_recv, last_recv, discovery_time_ms
        recv_ns = _message_time_ns(cfg)
        recv_monotonic_ns = time.perf_counter_ns()
        raw = bytes(sample.payload)
        parsed = parse_message(raw)
        with lock:
            counters["received_count"] += 1
            if discovery_time_ms is None:
                discovery_time_ms = (time.perf_counter_ns() - discovery_start) / 1e6
            if not parsed.get("valid"):
                counters["corrupt_count"] += 1; return
            if parsed.get("payload_size") != cfg.payload_size_bytes:
                counters["size_mismatch_count"] += 1; return
            identity = (parsed["publisher_id"], parsed["sequence"])
            if identity in seen:
                counters["duplicate_count"] += 1; return
            seen.add(identity); counters["valid_unique_count"] += 1
            previous = last_sequence.get(parsed["publisher_id"])
            if previous is not None and parsed["sequence"] < previous:
                counters["out_of_order_count"] += 1
            last_sequence[parsed["publisher_id"]] = max(previous if previous is not None else -1, parsed["sequence"])
            first_recv = recv_monotonic_ns if first_recv is None else first_recv
            last_recv = recv_monotonic_ns
            samples.append({"publisher_id": parsed["publisher_id"], "subscriber_id": subscriber_id,
                "sequence": parsed["sequence"], "latency_ms": (recv_ns - parsed["send_ns"]) / 1e6, "valid": True})

    session_start = time.perf_counter(); session = zenoh.open(_zconfig(cfg))
    startup_ms = (time.perf_counter() - session_start) * 1000
    subscriber = session.declare_subscriber(f"{cfg.key_prefix}/{cfg.run_id}/data/*", on_sample)
    print(json.dumps({"event": "ready", "role": "subscriber", "subscriber_id": subscriber_id}), flush=True)
    if cfg.scheduled_start_epoch_ns is not None:
        remaining_ns = max(
            0, cfg.scheduled_start_epoch_ns - _message_time_ns(cfg)
        )
        discovery_start = time.perf_counter_ns() + remaining_ns
    _wait_for_scheduled_start(cfg)
    meter = ResourceMeter().__enter__()
    recovery_wait = cfg.recovery_timeout_seconds if cfg.scenario_name == "S11" else 0.0
    duration = cfg.duration_seconds + cfg.drain_seconds + recovery_wait
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        _sleep_seconds(.1)
    resources = meter.close()
    shutdown_errors = []
    try:
        subscriber.undeclare()
    except Exception as exc:
        shutdown_errors.append(f"subscriber undeclare: {type(exc).__name__}: {exc}")
    try:
        session.close()
    except Exception as exc:
        shutdown_errors.append(f"session close: {type(exc).__name__}: {exc}")
    window = ((last_recv - first_recv) / 1e9) if first_recv is not None and last_recv != first_recv else None
    _write_result(output, {"role": "subscriber", "subscriber_id": subscriber_id, "host": socket.gethostname(),
        "pid": os.getpid(), **counters, "receive_window_seconds": window, "startup_time_ms": startup_ms,
        "discovery_time_ms": discovery_time_ms, "resources": resources, "samples": samples,
        "actual_payload_size_bytes": cfg.payload_size_bytes,
        "wire_message_size_bytes": len(build_message(0, 0, 0, cfg.payload_size_bytes, cfg.random_seed)),
        "shutdown_errors": shutdown_errors, "config": cfg.to_dict()})


def worker_main(role: str, config_path: str, index: int, output: str):
    cfg = BenchConfig.from_dict(json.loads(Path(config_path).read_text(encoding="utf-8")))
    (run_publisher if role == "publisher" else run_subscriber)(cfg, index, output)

