"""Startup/discovery timings with separate process, handshake and lookup boundaries."""

import time

from bench.common import Client, Worker, distribution, entry, metric, position_lookup


def run(options):
    with Worker() as server:
        ready_ms = server.wait_ready()
        handshakes = []
        first_rpc = []
        for sequence in range(options.samples):
            with Client(server.port, options.timeout) as client:
                handshakes.append(client.connect_ms)
                header, reply, started, finished = client.rpc(data=b"startup")
                if bytes(reply.data) != b"startup":
                    raise RuntimeError("First RPC payload mismatch")
                first_rpc.append((finished - started) / 1e6)
                if sequence == 0:
                    first_usable_ms = (finished - server.started_ns) / 1e6
        discovery = position_lookup(server, options)
    return "pass", {"server_spawn_to_listener_ready": metric(ready_ms, "ms"),
                    "server_spawn_to_first_successful_rpc": metric(first_usable_ms, "ms"),
                    "fresh_client_handshake": distribution(handshakes),
                    "first_rpc_after_handshake": distribution(first_rpc), "discovery": discovery}, [
        "Server process startup is one observation including interpreter/import cost and 5ms readiness polling",
        "Fresh connection samples reuse one running server; not repeated cold server launches",
        "Position spawn-to-resolution includes lookup polling and library query timeout; warm lookup is separate"]


if __name__ == "__main__":
    raise SystemExit(entry("startup_discovery", "performance", __doc__, run))
