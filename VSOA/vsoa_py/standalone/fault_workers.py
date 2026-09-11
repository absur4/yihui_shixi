"""Fault fixtures with explicitly application-owned source and receipt journals."""

import os
import threading
import time
from pathlib import Path

import vsoa

from standalone.io_utils import read_json, write_json


def journal_rows(path):
    import json
    if not Path(path).exists():
        return []
    content = Path(path).read_text(encoding="utf-8")
    rows = []
    for line in content.split("\n")[:-1]:
        if line:
            rows.append(json.loads(line))
    return rows


def append(handle, record):
    import json
    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    handle.flush()


def publisher(spec):
    folder, state = Path(spec["folder"]), Path(spec["state_directory"])
    config = spec["config"]
    records = {row["sequence"]: row for row in journal_rows(state / "source.jsonl")}
    server = vsoa.Server({"fault_fixture": True, "generation": spec["identifier"]})
    data = bytes(index % 251 for index in range(config["message_size_bytes"]))
    lock = threading.Lock()

    @server.command("/disconnect")
    def disconnect(client, request, payload):
        client.reply(request.seqno)
        for subscriber in server.clients():
            if subscriber.id != client.id:
                subscriber.close()

    @server.command("/replay")
    def replay(client, request, payload):
        sequences = payload.param["sequences"]
        if len(sequences) > 64:
            client.reply(request.seqno, status=2)
            return
        for sequence in sequences:
            with lock:
                row = records.get(sequence)
            if row:
                client.datagram("/recovery", {"param": {**row, "recovery": True}, "data": data})
        client.reply(request.seqno)

    def publish_loop():
        while True:
            try:
                server.address()
                break
            except Exception:
                time.sleep(0.005)
        write_json(folder / f"fault_publisher-{spec['identifier']}.ready.json", {"pid": os.getpid()})
        sequence = max(records, default=-1) + 1
        with open(state / "source.jsonl", "a", encoding="utf-8") as handle:
            while not (state / "freeze.json").exists():
                row = {"sequence": sequence, "send_ns": time.perf_counter_ns(), "generation": spec["identifier"]}
                append(handle, row)
                with lock:
                    records[sequence] = row
                server.publish("/fault/data", {"param": {**row, "recovery": False}, "data": data})
                sequence += 1
                time.sleep(1 / config["publish_rate_hz"])
        write_json(state / "source_frozen.json", {"count": len(records), "at_ns": time.perf_counter_ns()})

    threading.Thread(target=publish_loop, daemon=True).start()
    server.run("127.0.0.1", spec["port"])


def subscriber(spec):
    folder, state = Path(spec["folder"]), Path(spec["state_directory"])
    config = spec["config"]
    expected_data = bytes(index % 251 for index in range(config["message_size_bytes"]))
    existing = journal_rows(state / "receipts.jsonl")
    seen = {row["sequence"] for row in existing if row.get("valid")}
    lock = threading.RLock()
    status = {"connected": False, "subscribed": False, "pid": os.getpid(), "received": len(seen)}
    client = vsoa.Client()
    with open(state / "events.jsonl", "a", encoding="utf-8") as events, open(state / "receipts.jsonl", "a", encoding="utf-8") as receipts:
        def event(name, **details):
            with lock:
                append(events, {"event": name, "at_ns": time.perf_counter_ns(), **details})

        def onsubscribe(native, success):
            with lock:
                status["subscribed"] = success
            event("subscribed", success=success)

        def onconnect(native, connected, info):
            with lock:
                status.update(connected=connected, subscribed=False)
            event("connected" if connected else "disconnected")
            if connected:
                native.subscribe("/fault/", onsubscribe, timeout=2)

        def receive(native, url, payload, quick):
            arrived = time.perf_counter_ns()
            row = payload.param
            valid = (isinstance(row, dict) and isinstance(row.get("sequence"), int)
                     and arrived >= row.get("send_ns", arrived + 1) and bytes(payload.data or b"") == expected_data)
            with lock:
                duplicate = valid and row["sequence"] in seen
                append(receipts, {**(row if isinstance(row, dict) else {}), "receive_ns": arrived,
                                  "valid": valid, "duplicate": duplicate})
                if valid:
                    seen.add(row["sequence"])
                status["received"] = len(seen)

        client.onconnect, client.onmessage, client.ondata = onconnect, receive, receive
        client.robot(f"vsoa://127.0.0.1:{spec['port']}", keepalive=0.1, conn_timeout=0.5, reconn_delay=0.1)
        write_json(folder / f"fault_subscriber-{spec['identifier']}.ready.json", {"pid": os.getpid()})
        replayed = False
        try:
            while True:
                with lock:
                    write_json(state / "subscriber_state.json", {**status, "at_ns": time.perf_counter_ns()})
                request_path = state / "replay_request.json"
                if not replayed and request_path.exists():
                    replayed = True
                    sequences = read_json(request_path)["sequences"]
                    errors = []
                    deadline = time.monotonic() + config["fault_recovery_timeout_seconds"]
                    for offset in range(0, len(sequences), 64):
                        acknowledged = threading.Event()
                        responses = []

                        def callback(native, header, payload, event=acknowledged, values=responses):
                            values.append(header.status if header else None)
                            event.set()

                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            errors.append("Replay deadline exceeded")
                            break
                        accepted = client.call("/replay", payload={"param": {"sequences": sequences[offset:offset + 64]}},
                                               callback=callback, timeout=min(remaining, 2))
                        if not accepted or not acknowledged.wait(min(remaining, 2) + 0.2) or responses[0] != 0:
                            errors.append("Replay RPC failed")
                    write_json(state / "replay_done.json", {"errors": errors, "at_ns": time.perf_counter_ns()})
                time.sleep(0.02)
        finally:
            client.close()


def dispatch(spec):
    if spec["kind"] == "fault_publisher":
        publisher(spec)
    elif spec["kind"] == "fault_subscriber":
        subscriber(spec)
    else:
        raise ValueError("Unknown fault worker")
