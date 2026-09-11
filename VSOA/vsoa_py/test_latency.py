"""End-to-end latency: measured local one-way delivery and separately labeled RPC RTT."""

from bench.common import connected, distribution, echo_samples, entry, sample_summary


def run(options):
    with connected(options) as (server, client, startup):
        rows = echo_samples(client, options)
    result = sample_summary(rows)
    one_way = [(row["server_receive_ns"] - row["send_ns"]) / 1e6 for row in rows if row["ok"]]
    result.update(client_to_server_handler=distribution(one_way), samples=rows,
                  clock="same-host system-wide perf_counter_ns",
                  endpoints="before client.call -> server echo handler entry")
    valid_clock = all(value >= 0 for value in one_way)
    return "pass" if result["failed"] == 0 and valid_clock else "fail", result, [
        "One-way value is measured, never calculated as RTT/2",
        "Includes serialization, stack, scheduling and handler dispatch; excludes connection and warmup",
        "Same-host clocks only; cross-host one-way measurements require a measured synchronization error",
        "Closed-loop traffic may hide queueing under an independent offered load"]


if __name__ == "__main__":
    raise SystemExit(entry("latency", "performance", __doc__, run))
