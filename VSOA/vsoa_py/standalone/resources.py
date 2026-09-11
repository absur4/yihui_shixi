"""Separated middleware and benchmark-infrastructure CPU/RSS measurement."""

import threading
import time

import psutil

from standalone.io_utils import wait_until_ns
from standalone.statistics import stats


class ResourceMeter:
    def __init__(self, process_groups, start_ns, end_ns, interval):
        self.groups = {group: {name: psutil.Process(pid) for name, pid in pids.items()}
                       for group, pids in process_groups.items()}
        self.start_ns, self.end_ns, self.interval = start_ns, end_ns, interval
        self.rows, self.error, self.result = [], None, None
        self.thread = threading.Thread(target=self.measure, daemon=True)
        self.thread.start()

    def snapshot(self):
        output = {}
        for group, processes in self.groups.items():
            output[group] = {}
            for name, process in processes.items():
                cpu = process.cpu_times()
                output[group][name] = {"cpu_seconds": cpu.user + cpu.system, "rss_bytes": process.memory_info().rss}
        return output

    @staticmethod
    def group_totals(snapshot, group):
        values = snapshot[group].values()
        return {"cpu_seconds": sum(value["cpu_seconds"] for value in values),
                "rss_bytes": sum(value["rss_bytes"] for value in values)}

    def measure(self):
        try:
            wait_until_ns(self.start_ns)
            started = previous_ns = time.perf_counter_ns()
            baseline = previous = self.snapshot()
            while previous_ns < self.end_ns:
                wait_until_ns(min(previous_ns + int(self.interval * 1e9), self.end_ns))
                current, current_ns = self.snapshot(), time.perf_counter_ns()
                elapsed = (current_ns - previous_ns) / 1e9
                row = {"timestamp_ns": current_ns, "groups": {}, "per_process": current}
                for group in self.groups:
                    now = self.group_totals(current, group)
                    before = self.group_totals(previous, group)
                    row["groups"][group] = {"cpu_percent": (now["cpu_seconds"] - before["cpu_seconds"]) / elapsed * 100,
                                             "memory_mb": now["rss_bytes"] / 1e6}
                row["cpu_percent"] = sum(value["cpu_percent"] for value in row["groups"].values())
                row["memory_mb"] = sum(value["memory_mb"] for value in row["groups"].values())
                self.rows.append(row)
                previous, previous_ns = current, current_ns
            elapsed = (previous_ns - started) / 1e9

            def summarize(group_names):
                cpu_seconds = sum(self.group_totals(previous, group)["cpu_seconds"]
                                  - self.group_totals(baseline, group)["cpu_seconds"] for group in group_names)
                cpu_samples = [sum(row["groups"][group]["cpu_percent"] for group in group_names) for row in self.rows]
                rss_samples = [sum(row["groups"][group]["memory_mb"] for group in group_names) for row in self.rows]
                return {"cpu_percent": cpu_seconds / elapsed * 100, "memory_mb": max(rss_samples),
                        "cpu_time_seconds": cpu_seconds, "observed_duration_seconds": elapsed,
                        "cpu_statistics": stats(cpu_samples), "memory_statistics": stats(rss_samples),
                        "process_ids": {name: process.pid for group in group_names for name, process in self.groups[group].items()}}

            middleware = summarize(["middleware"])
            infrastructure = summarize(["test_infrastructure"])
            total = summarize(["middleware", "test_infrastructure"])
            self.result = {"middleware": middleware, "test_infrastructure": infrastructure, "total": total,
                           "cpu_percent": middleware["cpu_percent"], "memory_mb": middleware["memory_mb"],
                           "cpu_statistics": middleware["cpu_statistics"], "memory_statistics": middleware["memory_statistics"],
                           "observed_duration_seconds": elapsed, "samples": self.rows,
                           "process_ids": {group: value["process_ids"] for group, value in
                                           (("middleware", middleware), ("test_infrastructure", infrastructure))}}
        except Exception as error:
            self.error = error

    def finish(self, timeout):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("Resource meter did not finish")
        if self.error:
            raise self.error
        return self.result
