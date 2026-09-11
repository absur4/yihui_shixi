"""Jitter: RTT population standard deviation and consecutive-packet delay variation."""

from bench.common import connected, distribution, echo_samples, entry, metric, sample_summary


def run(options):
    with connected(options) as (server, client, startup):
        rows = echo_samples(client, options, paced=True)
    pairs = [(previous, current) for previous, current in zip(rows, rows[1:])
             if previous["ok"] and current["ok"]]
    variation = [abs(current["rtt_ms"] - previous["rtt_ms"]) for previous, current in pairs]
    spacing_error = [abs((current["receive_ns"] - previous["receive_ns"])
                         - (current["send_ns"] - previous["send_ns"])) / 1e6
                     for previous, current in pairs]
    result = sample_summary(rows)
    result.update(rtt_population_stddev=metric(result["rtt"]["stddev"], "ms"),
                  consecutive_absolute_rtt_variation=distribution(variation),
                  receive_minus_send_interval_absolute_error=distribution(spacing_error), samples=rows)
    return "pass" if result["failed"] == 0 and pairs else "fail", result, [
        "Jitter definitions are explicit; no RTP/RFC3550 jitter equivalence is assumed",
        "Pairs crossing failed samples are excluded; Python scheduling contributes to variation"]


if __name__ == "__main__":
    raise SystemExit(entry("jitter", "performance", __doc__, run))
