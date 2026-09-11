"""Throughput: bounded-window asynchronous RPC, validated completion goodput."""

import threading
import time

from bench.common import connected, entry, metric


def run(options):
    payload = bytes(index % 251 for index in range(options.payload_bytes))
    with connected(options) as (server, client, startup):
        for sequence in range(options.warmup):
            client.rpc(data=payload)
        condition = threading.Condition()
        pending = set()
        counts = {"attempted": 0, "send_accepted": 0, "completed_valid": 0,
                  "failed": 0, "duplicate_callbacks": 0}

        def make_callback(sequence):
            def callback(native, header, response):
                with condition:
                    if sequence not in pending:
                        counts["duplicate_callbacks"] += 1
                        return
                    pending.remove(sequence)
                    valid = (header is not None and header.status == 0 and response is not None
                             and response.param.get("sequence") == sequence
                             and bytes(response.data or b"") == payload)
                    counts["completed_valid" if valid else "failed"] += 1
                    condition.notify_all()
            return callback

        started = time.perf_counter()
        deadline = started + options.duration
        while time.perf_counter() < deadline:
            with condition:
                while len(pending) >= options.window:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    condition.wait(min(remaining, 0.05))
                if time.perf_counter() >= deadline:
                    break
                sequence = counts["attempted"]
                counts["attempted"] += 1
                pending.add(sequence)
            accepted = client.native.call("/echo", payload={"param": {"sequence": sequence}, "data": payload},
                                          callback=make_callback(sequence), timeout=options.timeout)
            with condition:
                if accepted:
                    counts["send_accepted"] += 1
                elif sequence in pending:
                    pending.remove(sequence)
                    counts["failed"] += 1
        send_finished = time.perf_counter()
        drain_deadline = send_finished + options.timeout + 0.5
        with condition:
            while pending and time.perf_counter() < drain_deadline:
                condition.wait(min(0.05, max(0, drain_deadline - time.perf_counter())))
            timed_out = len(pending)
            counts["failed"] += timed_out
            pending.clear()
        finished = time.perf_counter()
        elapsed = finished - started
        good_bytes = counts["completed_valid"] * options.payload_bytes
        results = {**counts, "unresolved_at_drain_timeout": timed_out,
                   "send_window": metric(send_finished - started, "s"),
                   "measurement_including_drain": metric(elapsed, "s"),
                   "rpc_rate": metric(counts["completed_valid"] / elapsed, "RPC/s"),
                   "one_direction_payload_goodput": metric(good_bytes / elapsed, "byte/s"),
                   "one_direction_payload_goodput_mib": metric(good_bytes / elapsed / 2**20, "MiB/s"),
                   "bidirectional_payload_goodput": metric(good_bytes * 2 / elapsed, "byte/s"),
                   "concurrency_window": options.window}
    return "pass" if counts["completed_valid"] and not counts["failed"] and not counts["duplicate_callbacks"] else "fail", results, [
        "Goodput counts only validated echo completions, not enqueue/send attempts",
        "Payload bytes exclude VSOA, JSON, TCP and Ethernet overhead; not wire bandwidth",
        "Single TCP connection, bounded outstanding RPCs, duration includes final drain"]


if __name__ == "__main__":
    raise SystemExit(entry("throughput", "performance", __doc__, run))
