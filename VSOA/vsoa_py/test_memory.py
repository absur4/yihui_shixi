"""Memory usage: separate client/server baseline, sampled RSS peak and delta."""

from bench.common import entry
from bench.resources import measure


def run(options):
    return measure(options, "memory")


if __name__ == "__main__":
    raise SystemExit(entry("memory", "performance", __doc__, run))
