"""Cross-platform: local smoke test plus an explicitly unverified platform matrix."""

import platform

from bench.common import connected, entry
from bench.probes import datagram_probe


def run(options):
    with connected(options) as (server, client, startup):
        header, reply, started, finished = client.rpc(data=b"platform-probe")
        rpc_ok = bytes(reply.data) == b"platform-probe"
        quick = datagram_probe(client, True, options.timeout)
    current = platform.system()
    matrix = {name: {"status": "pass" if rpc_ok and quick["passed"] else "fail"}
              if name == current else {"status": "not_tested"}
              for name in {"Windows", "Linux", "Darwin", current}}
    return "partial" if rpc_ok and quick["passed"] else "fail", {
        "local_os": current, "rpc_passed": rpc_ok, "quick": quick, "platform_matrix": matrix,
        "cross_os_interoperability": "not_tested"}, [
        "Run this file on each target OS and retain its JSON result",
        "An OS-independent package classifier is not a verified interoperability guarantee"]


if __name__ == "__main__":
    raise SystemExit(entry("cross_platform", "feature", __doc__, run))
