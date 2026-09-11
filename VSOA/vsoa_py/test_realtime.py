"""Real-time behavior: soft deadline compliance including client scheduling delay."""

from bench.common import connected, distribution, echo_samples, entry, metric, sample_summary


def run(options):
    with connected(options) as (server, client, startup):
        rows = echo_samples(client, options, paced=True)
    valid = [row for row in rows if row["ok"]]
    response = [(row["receive_ns"] - row["scheduled_ns"]) / 1e6 for row in valid]
    misses = sum(value > options.deadline_ms for value in response) + len(rows) - len(valid)
    result = sample_summary(rows)
    result.update(deadline=metric(options.deadline_ms, "ms"), deadline_misses=misses,
                  deadline_miss_rate=metric(misses / len(rows), "ratio"),
                  scheduled_release_to_completion=distribution(response),
                  dispatch_lateness=distribution([(row["send_ns"] - row["scheduled_ns"]) / 1e6
                                                  for row in valid]),
                  hard_realtime_guarantee="not_established", samples=rows)
    return "pass" if misses == 0 else "fail", result, [
        "Soft real-time observation on general-purpose OS and Python, not a worst-case bound",
        "Periodic scheduled release times expose scheduling lateness; closed-loop requests cannot overlap",
        "Deadline is a user-selected acceptance threshold, not a VSOA specification"]


if __name__ == "__main__":
    raise SystemExit(entry("realtime", "feature", __doc__, run))
