"""Transports: actual TCP, quick UDP and stream traffic; TLS evidence only."""

from bench.common import connected, entry
from bench.probes import datagram_probe, publish_probe, stream_probe


def run(options):
    with connected(options) as (server, client, startup):
        results = {"tcp_datagram": datagram_probe(client, False, options.timeout),
                   "udp_quick_datagram": datagram_probe(client, True, options.timeout),
                   "udp_quick_publish": publish_probe(client, True, options.timeout),
                   "tcp_stream": stream_probe(client, options.timeout)}
    passed = all(result["passed"] for result in results.values())
    results["tls"] = {"status": "documented_not_tested", "evidence": "vsoa/sslwork.py; Server.run sslopt",
                      "reason": "No test CA, certificates or authenticated TLS fixture configured"}
    results["ipv6"] = {"status": "not_tested"}
    return "partial" if passed else "fail", results, [
        "IPv4 loopback only; not a NAT, WAN or TLS validation",
        "Installed server source disables quick UDP when TLS is enabled"]


if __name__ == "__main__":
    raise SystemExit(entry("transports", "feature", __doc__, run))
