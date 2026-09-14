from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import psutil


class ResourceMonitor:
    """Sample only middleware-required processes, never the controller itself."""

    def __init__(
        self,
        pid_provider: Callable[[], Iterable[int]],
        interval_ms: int = 50,
    ) -> None:
        self._pid_provider = pid_provider
        self._interval = max(0.01, interval_ms / 1000.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.start_ns: int | None = None
        self.end_ns: int | None = None
        self.peak_rss_bytes: int | None = None
        self.first_rss_bytes: int | None = None
        self.last_rss_bytes: int | None = None
        self.sample_count = 0
        self._cpu_first: dict[int, float] = {}
        self._cpu_last: dict[int, float] = {}
        self._rss_samples: list[tuple[int, int]] = []

    def start(self) -> None:
        self.start_ns = time.perf_counter_ns()
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self._interval * 4))
        self._sample()
        self.end_ns = time.perf_counter_ns()
        observed_cpu = sum(
            max(0.0, self._cpu_last[pid] - self._cpu_first.get(pid, self._cpu_last[pid]))
            for pid in self._cpu_last
        )
        return {
            "peak_rss_bytes": self.peak_rss_bytes,
            "first_rss_bytes": self.first_rss_bytes,
            "last_rss_bytes": self.last_rss_bytes,
            "resource_sample_count": self.sample_count,
            "observed_cpu_time_seconds": observed_cpu,
            "resource_monitor_start_ns": self.start_ns,
            "resource_monitor_end_ns": self.end_ns,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self._interval)

    def rss_window(self, start_ns: int, end_ns: int) -> dict[str, int | None]:
        samples = [
            (timestamp_ns, rss_bytes)
            for timestamp_ns, rss_bytes in self._rss_samples
            if start_ns <= timestamp_ns <= end_ns
        ]
        if not samples:
            return {
                "formal_first_rss_bytes": None,
                "formal_last_rss_bytes": None,
                "formal_rss_sample_count": 0,
            }
        return {
            "formal_first_rss_bytes": samples[0][1],
            "formal_last_rss_bytes": samples[-1][1],
            "formal_rss_sample_count": len(samples),
        }

    def _sample(self) -> None:
        pids: set[int] = set()
        for root_pid in list(self._pid_provider()):
            try:
                root = psutil.Process(root_pid)
                pids.add(root.pid)
                pids.update(child.pid for child in root.children(recursive=True))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        total_rss = 0
        sampled = False
        for pid in pids:
            try:
                process = psutil.Process(pid)
                total_rss += process.memory_info().rss
                cpu = process.cpu_times()
                cpu_total = float(cpu.user + cpu.system)
                self._cpu_first.setdefault(pid, cpu_total)
                self._cpu_last[pid] = cpu_total
                sampled = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if sampled:
            sample_ns = time.perf_counter_ns()
            self.sample_count += 1
            if self.first_rss_bytes is None:
                self.first_rss_bytes = total_rss
            self.last_rss_bytes = total_rss
            self._rss_samples.append((sample_ns, total_rss))
            if self.peak_rss_bytes is None or total_rss > self.peak_rss_bytes:
                self.peak_rss_bytes = total_rss
