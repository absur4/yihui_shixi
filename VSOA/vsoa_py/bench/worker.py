"""Isolated VSOA server and position-server processes."""

import argparse
import json
import socket
import threading
import time
from pathlib import Path

import vsoa


def serve(port, ready):
    server = vsoa.Server({"name": "vsoa-benchmark"})
    received = {}

    @server.command("/echo")
    def echo(client, request, payload):
        arrival = time.perf_counter_ns()
        params = dict(payload.param or {})
        params["server_receive_ns"] = arrival
        client.reply(request.seqno, {"param": params, "data": payload.data})

    @server.command("/publish")
    def publish(client, request, payload):
        sent = server.publish("/bench/topic", payload, quick=payload.param.get("quick", False))
        client.reply(request.seqno, {"param": {"sent": sent}})

    @server.command("/qos")
    def qos(client, request, payload):
        try:
            client.priority = payload.param["priority"]
            result = {"applied": True, "priority": client.priority}
        except (OSError, ValueError) as error:
            result = {"applied": False, "error": str(error)}
        client.reply(request.seqno, {"param": result})

    @server.command("/silent")
    def silent(client, request, payload):
        return None

    @server.command("/stats")
    def stats(client, request, payload):
        client.reply(request.seqno, {"param": {"counts": received}})

    @server.command("/reset")
    def reset(client, request, payload):
        received.clear()
        client.reply(request.seqno)

    @server.command("/stream")
    def stream(client, request, payload):
        def ondata(channel, data):
            channel.send(data)

        channel = server.create_stream(lambda channel, connected: None, ondata)
        client.reply(request.seqno, tunid=channel.tunid)

    def ondata(client, url, payload, quick):
        if url == "/loss":
            sequence = str(payload.param["sequence"])
            received[sequence] = received.get(sequence, 0) + 1
        else:
            client.datagram(url, payload, quick=quick)

    def announce():
        while True:
            try:
                address = server.address()
            except Exception:
                time.sleep(0.005)
            else:
                Path(ready).write_text(json.dumps({"address": address}), encoding="utf-8")
                return

    server.ondata = ondata
    threading.Thread(target=announce, daemon=True).start()
    server.run("127.0.0.1", port)


def position(port, target):
    def onquery(query, reply):
        if query["name"] == "benchmark-service":
            reply({"addr": "127.0.0.1", "port": target, "domain": socket.AF_INET})
        else:
            reply(None)

    vsoa.Position(onquery).run("127.0.0.1", port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["server", "position"])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--target", type=int)
    parser.add_argument("--ready")
    options = parser.parse_args()
    if options.kind == "server":
        serve(options.port, options.ready)
    else:
        position(options.port, options.target)
