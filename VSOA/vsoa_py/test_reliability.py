"""Reliability: integrity, sequence correctness, timeout signal and new-session recovery."""

import time

from bench.common import Client, connected, echo_samples, entry, metric, sample_summary


def run(options):
    with connected(options) as (server, client, startup):
        rows = echo_samples(client, options)
        began = time.perf_counter_ns()
        try:
            client.rpc("/silent", timeout=options.timeout)
            timeout_detected = False
        except TimeoutError:
            timeout_detected = True
        timeout_ms = (time.perf_counter_ns() - began) / 1e6
        header, reply, started, finished = client.rpc(data=b"after-timeout")
        after_timeout = bytes(reply.data) == b"after-timeout"
        client.close()
        with Client(server.port, options.timeout) as reconnected:
            header, reply, started, finished = reconnected.rpc(data=b"reconnected")
            reconnect_ok = bytes(reply.data) == b"reconnected"
        results = sample_summary(rows)
        results.update(timeout_detected=timeout_detected,
                       timeout_observed=metric(timeout_ms, "ms"),
                       usable_after_timeout=after_timeout, manual_new_session_passed=reconnect_ok,
                       samples=rows)
    passed = results["failed"] == 0 and timeout_detected and after_timeout and reconnect_ok
    return "pass" if passed else "fail", results, [
        "Ordered sequential RPC integrity is tested on a healthy TCP loopback path",
        "Timeout measured by bounded harness; not an exact timer-accuracy guarantee",
        "Recovery creates a new client explicitly; automatic reconnect and TCP retransmission are not tested",
        "No exactly-once, persistence or server-failover guarantee inferred"]


if __name__ == "__main__":
    raise SystemExit(entry("reliability", "feature", __doc__, run))
