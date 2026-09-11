"""CPU usage: separate client/server CPU-time deltas under RPC load."""

from bench.common import entry
from bench.resources import measure


def run(options):
    return measure(options, "cpu")


if __name__ == "__main__":
    raise SystemExit(entry("cpu", "performance", __doc__, run))
