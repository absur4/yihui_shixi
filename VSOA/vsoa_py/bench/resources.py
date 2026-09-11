"""Resource measurements under an explicit sequential echo workload."""

import threading
import time

import psutil

from bench.common import connected, distribution, metric


def measure(options, kind):
    with connected(options) as (server, client, startup):
        payload = bytes(options.payload_bytes)
        for sequence in range(options.warmup):
            client.rpc(data=payload)
        server_process = psutil.Process(server.process.pid)
        descendants = server_process.children(recursive=True)
        processes = {"client": psutil.Process(), "server": descendants[-1] if descendants else server_process}
        baseline = {name: process.memory_info().rss for name, process in processes.items()}
        samples = []
        stop = threading.Event()

        def sample():
            samples.append({"timestamp_ns": time.perf_counter_ns(),
                            **{name: process.memory_info().rss for name, process in processes.items()}})

        def monitor():
            while not stop.wait(options.sample_interval):
                sample()

        sample()
        monitor_thread = threading.Thread(target=monitor, daemon=True)
        cpu_start = {name: process.cpu_times() for name, process in processes.items()}
        started = time.perf_counter()
        monitor_thread.start()
        completed = 0
        failures = 0
        try:
            while time.perf_counter() - started < options.duration:
                try:
                    header, response, sent, received = client.rpc(data=payload)
                    if bytes(response.data or b"") == payload:
                        completed += 1
                    else:
                        failures += 1
                except (TimeoutError, RuntimeError):
                    failures += 1
        finally:
            stop.set()
            monitor_thread.join(timeout=2)
        elapsed = time.perf_counter() - started
        cpu_end = {name: process.cpu_times() for name, process in processes.items()}
        sample()
        result = {"workload": "closed-loop sequential TCP echo RPC",
                  "duration": metric(elapsed, "s"), "completed_valid": completed, "failed": failures,
                  "rpc_rate": metric(completed / elapsed, "RPC/s"),
                  "process_ids": {name: process.pid for name, process in processes.items()}}
        if kind == "cpu":
            per_process = {}
            total = 0
            for name in processes:
                seconds = ((cpu_end[name].user + cpu_end[name].system)
                           - (cpu_start[name].user + cpu_start[name].system))
                total += seconds
                per_process[name] = {"cpu_time": metric(seconds, "s"),
                                     "one_core_percent": metric(seconds / elapsed * 100, "%"),
                                     "host_capacity_percent": metric(seconds / elapsed * 100
                                                                     / (psutil.cpu_count() or 1), "%")}
            result.update(processes=per_process, combined_one_core_percent=metric(total / elapsed * 100, "%"))
        else:
            per_process = {}
            for name in processes:
                values = [row[name] for row in samples]
                per_process[name] = {"rss_baseline": metric(baseline[name], "byte"),
                                     "rss_end": metric(values[-1], "byte"),
                                     "rss_delta": metric(values[-1] - baseline[name], "byte"),
                                     "rss_sampled": distribution(values, "byte")}
            result.update(processes=per_process, samples=samples,
                          sample_interval=metric(options.sample_interval, "s"),
                          combined_sampled_peak_rss=metric(max(row["client"] + row["server"]
                                                              for row in samples), "byte"))
    return "pass" if completed and failures == 0 else "fail", result, [
        "Client includes generator, callbacks and resource monitor; server includes Python runtime",
        "Retained RSS samples are harness overhead; use a moderate sample interval for long runs",
        "CPU 100% means one logical core; coarse OS CPU accounting may report zero for very short runs",
        "RSS is resident memory, not private heap; sampled peak may miss shorter spikes",
        "Summed RSS may double-count shared pages; no leak verdict from a short run"]
