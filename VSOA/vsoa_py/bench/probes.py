"""Reusable functional probes; every result represents actual traffic."""

import threading


def datagram_probe(client, quick, timeout):
    arrived = threading.Event()
    observed = {}

    def ondata(native, url, payload, received_quick):
        observed.update(url=url, quick=received_quick,
                        valid=bytes(payload.data or b"") == b"datagram-probe")
        arrived.set()

    client.native.ondata = ondata
    sent = client.native.datagram("/datagram", {"data": b"datagram-probe"}, quick=quick)
    received = arrived.wait(timeout) if sent else False
    return {"passed": bool(sent and received and observed.get("valid")
                           and observed.get("url") == "/datagram" and observed.get("quick") == quick),
            "transport": "UDP" if quick else "TCP", "send_accepted": sent,
            "received": received, "observed": observed}


def publish_probe(client, quick, timeout):
    subscribed = threading.Event()
    arrived = threading.Event()
    acknowledgement = []
    observed = {}

    def onmessage(native, url, payload, received_quick):
        observed.update(url=url, quick=received_quick,
                        valid=bytes(payload.data or b"") == b"publish-probe")
        arrived.set()

    def onsubscribe(native, success):
        acknowledgement.append(success)
        subscribed.set()

    client.native.onmessage = onmessage
    accepted = client.native.subscribe("/bench/", onsubscribe, timeout=timeout)
    if not accepted or not subscribed.wait(timeout) or not acknowledgement[0]:
        return {"passed": False, "error": "subscription was not acknowledged"}
    client.rpc("/publish", {"quick": quick}, b"publish-probe")
    received = arrived.wait(timeout)
    return {"passed": bool(received and observed.get("valid")
                           and observed.get("url") == "/bench/topic" and observed.get("quick") == quick),
            "subscription": "/bench/", "published_topic": "/bench/topic",
            "transport": "UDP" if quick else "TCP", "observed": observed}


def stream_probe(client, timeout):
    connected = threading.Event()
    arrived = threading.Event()
    received = bytearray()
    expected = bytes(range(256)) * 16

    def onlink(channel, ready):
        if ready:
            connected.set()

    def ondata(channel, data):
        received.extend(data)
        if len(received) >= len(expected):
            arrived.set()

    header, payload, started, finished = client.rpc("/stream")
    stream = client.native.create_stream(header.tunid, onlink, ondata, timeout=timeout)
    try:
        if not connected.wait(timeout):
            return {"passed": False, "error": "stream did not connect"}
        sent = stream.send(expected)
        complete = arrived.wait(timeout)
        return {"passed": complete and bytes(received) == expected and sent == len(expected),
                "transport": "TCP stream", "sent_bytes": sent, "received_bytes": len(received)}
    finally:
        stream.close()
