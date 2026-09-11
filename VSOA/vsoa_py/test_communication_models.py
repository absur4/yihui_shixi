"""Communication models: RPC, publish/subscribe, datagrams and duplex streams."""

from bench.common import connected, entry
from bench.probes import datagram_probe, publish_probe, stream_probe


def run(options):
    with connected(options) as (server, client, startup):
        header, reply, started, finished = client.rpc(data=b"rpc-probe")
        results = {"rpc": {"passed": bytes(reply.data) == b"rpc-probe"},
                   "publish_subscribe": publish_probe(client, False, options.timeout),
                   "tcp_datagram": datagram_probe(client, False, options.timeout),
                   "quick_datagram": datagram_probe(client, True, options.timeout),
                   "duplex_stream": stream_probe(client, options.timeout)}
    return "pass" if all(value["passed"] for value in results.values()) else "fail", results, [
        "Single publisher and subscriber; no fanout scaling tested"]


if __name__ == "__main__":
    raise SystemExit(entry("communication_models", "feature", __doc__, run))
