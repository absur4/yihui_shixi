"""Discovery mechanism: real Position name resolution and missing-service behavior."""

from bench.common import Worker, entry, position_lookup


def run(options):
    with Worker() as server:
        server.wait_ready()
        results = position_lookup(server, options)
    return "pass" if results["unknown_service_returns_none"] else "fail", results, [
        "Static registry callback, not multicast discovery or automatic registration",
        "Position service is a separate local process; discovery does not imply RPC readiness"]


if __name__ == "__main__":
    raise SystemExit(entry("discovery", "feature", __doc__, run))
