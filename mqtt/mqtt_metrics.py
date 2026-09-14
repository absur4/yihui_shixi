"""Unified metric definitions 1.0. No MQTT dependencies."""
import math
import statistics


def percentile(values, p):
    if not values:
        return None
    x = sorted(values)
    h = (len(x) - 1) * p
    lo, hi = math.floor(h), math.ceil(h)
    return x[lo] + (x[hi] - x[lo]) * (h - lo)


def describe(values):
    x = [v for v in values if v is not None]
    return dict(count=len(x), mean=statistics.mean(x) if x else None,
                median=statistics.median(x) if x else None,
                variance=statistics.pvariance(x) if x else None,
                std=statistics.pstdev(x) if x else None,
                min=min(x) if x else None, max=max(x) if x else None,
                p95=percentile(x, .95), p99=percentile(x, .99))


def latency_metrics(links):
    """links contains lists of (sequence_id, latency_ms), one per pub->sub."""
    values = [lat for link in links for _, lat in link]
    diffs = [abs(b[1] - a[1]) for link in links
             for a, b in zip(sorted(link), sorted(link)[1:])]
    d = describe(values)
    return dict(latency_ms=d['mean'], latency_p95_ms=d['p95'],
                latency_p99_ms=d['p99'], latency_std_ms=d['std'],
                jitter_ms=statistics.mean(diffs) if diffs else None,
                latency_sample_count=len(values), jitter_sample_count=len(diffs))


def throughput(count, payload, window):
    return count * payload * 8 / window / 1_000_000 if window and window > 0 else None


def loss(expected, received):
    return (expected - received) / expected if expected else None


def resource_window_metrics(samples, resource_start_ns, window_start_ns, window_end_ns):
    """Weight CPU samples by their actual interval lengths, including boundary overlap."""
    weighted, covered = 0.0, 0
    previous = resource_start_ns
    for sample in samples:
        current = sample['timestamp_ns']
        overlap = max(0, min(current, window_end_ns)-max(previous, window_start_ns))
        weighted += sample['cpu_percent']*overlap
        covered += overlap
        previous = current
    return dict(cpu_percent=weighted/covered if covered else None,
                memory_mb=max((s['memory_mb'] for s in samples
                               if window_start_ns <= s['timestamp_ns'] < window_end_ns),default=None),
                resource_sample_coverage_seconds=covered/1e9,
                resource_boundary_method='CPU weighted by sampled interval overlap; uniform allocation within a boundary interval')
