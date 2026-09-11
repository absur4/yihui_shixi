"""Packet loss: real UDP proxy drops, native observation and explicit application retry."""

import time

from bench.common import Client, Worker, entry, metric
from bench.loss_proxy import LossProxy


def run(options):
    if options.samples > 10000:
        raise ValueError("Loss test supports at most 10000 IDs per statistics response")
    with Worker() as server:
        server.wait_ready()
        with LossProxy(server.port, options.loss_rate, options.seed) as proxy:
            with Client(proxy.port, options.timeout) as client:
                expected = set(range(options.samples))
                data = bytes(options.payload_bytes)
                local_send_failures = 0

                def send(sequences):
                    nonlocal local_send_failures
                    for sequence in sorted(sequences):
                        accepted = client.native.datagram("/loss", {"param": {"sequence": sequence}, "data": data},
                                                          quick=True)
                        if not accepted:
                            local_send_failures += 1
                        time.sleep(min(options.interval_ms / 1000, 0.01))

                def counts():
                    header, response, started, finished = client.rpc("/stats")
                    return {int(sequence): count for sequence, count in response.param["counts"].items()}

                def settle():
                    deadline = time.monotonic() + options.timeout
                    previous = None
                    stable_since = time.monotonic()
                    while True:
                        current = counts()
                        if current != previous:
                            previous = current
                            stable_since = time.monotonic()
                        if time.monotonic() - stable_since >= min(0.1, options.timeout / 2):
                            return current
                        if time.monotonic() >= deadline:
                            return current
                        time.sleep(0.01)

                client.rpc("/reset")
                started = time.perf_counter_ns()
                send(expected)
                initial_counts = settle()
                initial_missing = expected - set(initial_counts)
                initial_proxy = proxy.snapshot()
                time.sleep(options.timeout)
                native_counts = counts()
                native_recovered = initial_missing & set(native_counts)
                retry_started = time.perf_counter_ns()
                history = []
                final_counts = native_counts
                for attempt in range(options.retries):
                    missing = expected - set(final_counts)
                    if not missing:
                        break
                    send(missing)
                    final_counts = settle()
                    history.append({"round": attempt + 1, "retransmitted": len(missing),
                                    "remaining_missing": len(expected - set(final_counts))})
                finished = time.perf_counter_ns()
                final_missing = expected - set(final_counts)
                proxy_counts = proxy.snapshot()
                recovered = initial_missing - final_missing
                results = {"injection": {"method": "seeded Bernoulli drop of real UDP datagrams at local relay",
                                         "direction": "client-to-server only", "seed": options.seed,
                                         "configured_drop_rate": metric(options.loss_rate, "ratio"),
                                         "observed_proxy_drop_rate": metric(proxy_counts["dropped"] / proxy_counts["seen"]
                                                                            if proxy_counts["seen"] else None, "ratio"),
                                         "initial_proxy": initial_proxy, "all_attempts_proxy": proxy_counts},
                           "sent_unique": len(expected), "local_send_failures": local_send_failures,
                           "initial_received_unique": len(initial_counts),
                           "initial_missing_ids": sorted(initial_missing),
                           "initial_loss_rate": metric(len(initial_missing) / len(expected), "ratio"),
                           "native_observation": {"wait": metric(options.timeout, "s"),
                                                  "late_or_native_recovered_ids": sorted(native_recovered)},
                           "application_retry": {"rounds": history, "final_received_unique": len(final_counts),
                                                 "remaining_missing_ids": sorted(final_missing),
                                                 "remaining_loss_rate": metric(len(final_missing) / len(expected), "ratio"),
                                                 "recovered_initial_missing": len(recovered),
                                                 "recovery_rate": metric(len(recovered) / len(initial_missing)
                                                                         if initial_missing else None, "ratio"),
                                                 "recovery_phase_duration": metric((finished - retry_started) / 1e6, "ms"),
                                                 "duplicate_deliveries": sum(count - 1 for count in final_counts.values())},
                           "total_duration": metric((finished - started) / 1e6, "ms"),
                           "tcp_kernel_packet_loss_recovery": "not_tested"}
    valid = (not proxy_counts["errors"] and local_send_failures == 0
             and proxy_counts["seen"] == proxy_counts["dropped"] + proxy_counts["forwarded"]
             and set(final_counts) <= expected)
    return "pass" if valid else "fail", results, [
        "Pass means the experiment completed consistently, not zero loss or successful recovery",
        "UDP control/statistics use reliable TCP; the relay intentionally does not forward server UDP replies",
        "Native observation cannot distinguish delayed delivery from a retry; UDP quick has no app retry here",
        "Explicit missing-ID retransmission and receiver deduplication are harness logic, not VSOA features",
        "TCP byte dropping would corrupt a stream rather than simulate TCP packet loss; kernel netem is not used",
        "No privileged network, firewall or system-wide traffic settings are changed"]


if __name__ == "__main__":
    raise SystemExit(entry("packet_loss_recovery", "performance", __doc__, run))
