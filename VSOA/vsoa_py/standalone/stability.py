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
    sample_names = {field: f"subscriber-{identifier}.{field}.f64" for field in ("latencies_ms", "jitter_samples_ms")}
    sample_files = {field: open(folder / name, "wb") for field, name in sample_names.items()}

    def receive(native, url, payload, quick):
        arrived = time.perf_counter_ns()
        with lock:
            if not control or arrived > control["end_ns"] + int(config["drain_seconds"] * 1e9):
                return
            params = payload.param
            try:
                source, sequence, sent_ns = params["publisher"], params["sequence"], params["send_ns"]
                valid = (0 <= source < len(seen) and 0 <= sequence < planned and arrived >= sent_ns
                         and bytes(payload.data or b"") == expected_payload and not quick)
            except (KeyError, TypeError):
                valid = False
            if not valid:
                counts["invalid"] += 1
                return
            if seen[source][sequence]:
                counts["duplicates"] += 1
                return
            seen[source][sequence] = 1
            delay = (arrived - sent_ns) / 1e6
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
            counts["within_window"] += int(arrived <= control["end_ns"])
            counts["first_receive_ns"] = counts["first_receive_ns"] or arrived
            counts["last_receive_ns"] = arrived

    try:
        for endpoint in spec["endpoints"]:
            client = vsoa.Client()
            clients.append(client)
            client.onmessage = receive
            if client.connect(f"vsoa://127.0.0.1:{endpoint}", timeout=5) != vsoa.Client.CONNECT_OK:
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
        wait_until_ns(cutoff)
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
