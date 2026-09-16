"""Bounded-memory delivery accounting and latency sampling for long stability runs."""

import math
import struct
import os
import random
import threading
import time
from pathlib import Path

import vsoa

from standalone.io_utils import wait_file, wait_until_ns, write_json
from standalone.statistics import stats


class Moments:
    def __init__(self, capacity, seed):
        self.count, self.mean, self.m2 = 0, 0.0, 0.0
        self.minimum, self.maximum = None, None
        self.samples, self.capacity = [], capacity
        self.random = random.Random(seed)

    def add(self, value):
        self.count += 1
        difference = value - self.mean
        self.mean += difference / self.count
        self.m2 += difference * (value - self.mean)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        if len(self.samples) < self.capacity:
            self.samples.append(value)
        else:
            index = self.random.randrange(self.count)
            if index < self.capacity:
                self.samples[index] = value

    def summary(self):
        result = stats(self.samples)
        result.update(count=self.count, min=self.minimum, max=self.maximum,
                      mean=self.mean if self.count else None,
                      variance=self.m2 / self.count if self.count else None,
                      std=math.sqrt(max(0, self.m2 / self.count)) if self.count else None,
                      sample_count=len(self.samples), percentile_method="seeded_uniform_reservoir",
                      exact_moments=True)
        return result


def subscriber(spec):
    config = spec["config"]
    folder = Path(spec["folder"])
    identifier = spec["identifier"]
    capacity = config.get("sample_capacity", 10000)
    latency = Moments(capacity, config["seed"] + identifier)
    jitter = Moments(capacity, config["seed"] + identifier + 99)
    planned = math.ceil(config["duration_seconds"] * config["publish_rate_hz"]) + 1
    seen = [bytearray(planned) for endpoint in spec["endpoints"]]
    previous = {}
    counts = {"duplicates": 0, "invalid": 0, "within_window": 0, "out_of_order": 0,
              "first_receive_ns": None, "last_receive_ns": None}
    control = {}
    lock = threading.Lock()
    clients, threads = [], []
    expected_payload = bytes(index % 251 for index in range(config["message_size_bytes"]))
    # 发布端（workers.publisher）会把超过 fragment_size_bytes 的 payload 切成多片发送：
    # 一条逻辑消息 = N 个数据报，每片都带 fragment_index / fragment_count / logical_size_bytes。
    # 1 KiB 的组看不出问题（只有 1 片），64 KiB 就有 2 片，1 MiB 有 18 片 —— 少了重组，
    # 每片都会被拿去和"整包期望值"比较，整轮必然全判 invalid（曾实测 30000 条 × 2 片 = 60000）。
    fragment_bytes = min(config.get("fragment_size_bytes", 60000), config["message_size_bytes"])
    assemblies = {}
    clock_offset_ns = int(spec.get("clock_offset_ns", 0))
    clock_uncertainty_ns = int(spec.get("clock_uncertainty_ns", 0))

    def common_now_ns():
        return time.perf_counter_ns() + clock_offset_ns
    sample_names = {field: f"subscriber-{identifier}.{field}.f64" for field in ("latencies_ms", "jitter_samples_ms")}
    sample_files = {field: open(folder / name, "wb") for field, name in sample_names.items()}

    def receive(native, url, payload, quick):
        arrived = common_now_ns()
        with lock:
            if not control or arrived > control["end_ns"] + int(config["drain_seconds"] * 1e9):
                return
            params = payload.param
            try:
                source, sequence, sent_ns = params["publisher"], params["sequence"], params["send_ns"]
                fragment_index = params.get("fragment_index", 0)
                fragment_count = params.get("fragment_count", 1)
                logical_size = params.get("logical_size_bytes", config["message_size_bytes"])
                declared_size = params.get("payload_size_bytes", config["message_size_bytes"])
                fragment = bytes(payload.data or b"")
                expected_fragment = expected_payload[
                    fragment_index * fragment_bytes:(fragment_index + 1) * fragment_bytes]
                valid = (0 <= source < len(seen) and isinstance(sequence, int) and 0 <= sequence < planned
                         and isinstance(fragment_index, int) and isinstance(fragment_count, int)
                         and 0 <= fragment_index < fragment_count
                         and logical_size == config["message_size_bytes"]
                         and declared_size == config["message_size_bytes"]
                         and fragment == expected_fragment
                         and arrived + clock_uncertainty_ns >= sent_ns and not quick)
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                counts["invalid"] += 1
                return
            key = (source, sequence)
            assembly = assemblies.get(key)
            if assembly is None:
                assembly = assemblies[key] = {"parts": set(), "count": fragment_count, "arrival_ns": arrived}
            if fragment_index in assembly["parts"]:
                counts["duplicates"] += 1
                return
            assembly["parts"].add(fragment_index)
            assembly["arrival_ns"] = max(assembly["arrival_ns"], arrived)
            if len(assembly["parts"]) != assembly["count"]:
                return                       # 分片还没收齐，等齐了才记一条
            del assemblies[key]
            if seen[source][sequence]:
                counts["duplicates"] += 1
                return
            seen[source][sequence] = 1
            delay = max(0, assembly["arrival_ns"] - sent_ns) / 1e6
            latency.add(delay)
            sample_files["latencies_ms"].write(struct.pack("d", delay))
            if source in previous:
                previous_sequence, previous_delay = previous[source]
                if sequence == previous_sequence + 1:
                    jitter.add(abs(delay - previous_delay))
                    sample_files["jitter_samples_ms"].write(struct.pack("d", abs(delay - previous_delay)))
                elif sequence < previous_sequence:
                    counts["out_of_order"] += 1
            previous[source] = (sequence, delay)
            counts["within_window"] += int(assembly["arrival_ns"] <= control["end_ns"])
            counts["first_receive_ns"] = counts["first_receive_ns"] or assembly["arrival_ns"]
            counts["last_receive_ns"] = assembly["arrival_ns"]

    try:
        for endpoint in spec["endpoints"]:
            host, port = ((endpoint.get("host"), endpoint.get("port"))
                          if isinstance(endpoint, dict) else ("127.0.0.1", endpoint))
            client = vsoa.Client()
            clients.append(client)
            client.onmessage = receive
            if client.connect(f"vsoa://{host}:{port}", timeout=5) != vsoa.Client.CONNECT_OK:
                raise ConnectionError("Stability subscriber could not connect")
            thread = threading.Thread(target=client.run, daemon=True)
            threads.append(thread)
            thread.start()
            acknowledged = threading.Event()
            status = []

            def onsubscribe(native, success, event=acknowledged, values=status):
                values.append(success)
                event.set()

            if not client.subscribe("/bench/", onsubscribe, timeout=2) or not acknowledged.wait(3) or not status[0]:
                raise TimeoutError("Stability subscription failed")
        write_json(folder / f"subscriber-{identifier}.ready.json", {"pid": os.getpid()})
        initial_control = wait_file(folder / "start.json", config["startup_timeout_seconds"] + 10)
        with lock:
            control.update(initial_control)
        cutoff = control["end_ns"] + int(config["drain_seconds"] * 1e9)
        wait_until_ns(cutoff - clock_offset_ns)
        published = wait_file(folder / "counts.json", 10)["publishers"]
        with lock:
            result = {"subscriber": identifier, "expected_deliveries": sum(published),
                      "initial_received": latency.count, "final_received": latency.count,
                      "invalid_messages": counts["invalid"], "duplicate_messages": counts["duplicates"],
                      "corrupted_count": counts["invalid"], "unparseable_count": 0,
                      "out_of_range_messages": 0, "out_of_order_messages": counts["out_of_order"],
                      "out_of_order_count": counts["out_of_order"], "links": [],
                      "latencies_ms": latency.samples, "jitter_samples_ms": jitter.samples,
                      "latency_moments": latency.summary(), "jitter_moments": jitter.summary(),
                      "measurement_window_received": counts["within_window"], "late_native_deliveries": 0,
                      "application_recovered": 0, "application_retry_requests": 0, "recovery_errors": [],
                      "recovery_time_ms": None, "recovery_latencies_ms": [], "initial_cutoff_ns": cutoff,
                      "first_counted_receive_ns": counts["first_receive_ns"],
                      "last_counted_receive_ns": counts["last_receive_ns"],
                      # 结束时还没收齐分片的逻辑消息数：用于区分"分片丢失"与"内容错误"
                      "incomplete_logical_messages": len(assemblies),
                      "sample_capacity": capacity, "receipt_bitmap_bytes": planned * len(seen)}
        for field, handle in sample_files.items():
            handle.flush()
            result[field + "_file"] = sample_names[field]
        result["full_sample_encoding"] = "native little-endian float64; exact parent quantiles"
        write_json(folder / f"subscriber-{identifier}.result.json", result)
        wait_file(folder / "finish.json", 30)
    finally:
        for client in clients:
            client.close()
        for thread in threads:
            thread.join(timeout=2)
        for handle in sample_files.values():
            handle.close()
