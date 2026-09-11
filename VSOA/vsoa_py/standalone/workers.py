"""Independent publisher and subscriber processes using the unmodified VSOA package."""

import math
import os
import socket
import threading
import time
import traceback
import zlib
from pathlib import Path

import vsoa

from standalone.io_utils import read_json, wait_file, wait_until_ns, write_json
from standalone.statistics import stats


def publisher(spec):
    config = spec["config"]
    folder = Path(spec["folder"])
    identifier = spec["identifier"]
    server = vsoa.Server({"module": "vsoa", "publisher": identifier})
    payload = bytes(index % 251 for index in range(config["message_size_bytes"]))
    payload_checksum = f"crc32:{zlib.crc32(payload):08x}"
    fragment_bytes = min(config.get("fragment_size_bytes", 60000), len(payload))
    fragments = [payload[offset:offset + fragment_bytes] for offset in range(0, len(payload), fragment_bytes)]
    send_times = []
    sent_count = 0
    recovery_deliveries = 0

    @server.command("/replay")
    def replay(client, request, request_payload):
        nonlocal recovery_deliveries
        sequences = request_payload.param.get("sequences", [])
        if not isinstance(sequences, list) or len(sequences) > 64:
            client.reply(request.seqno, status=2)
            return
        for sequence in sequences:
            if isinstance(sequence, int) and 0 <= sequence < len(send_times):
                success = True
                for fragment_index, fragment in enumerate(fragments):
                    success = client.datagram("/recovery", {"param": {"publisher": identifier, "sequence": sequence,
                                              "send_ns": send_times[sequence], "recovery": True,
                                              "subscriber_target": "requesting_subscriber",
                                              "payload_size_bytes": len(payload), "checksum": payload_checksum,
                                              "fragment_index": fragment_index, "fragment_count": len(fragments),
                                              "logical_size_bytes": len(payload)}, "data": fragment}, quick=False) and success
                recovery_deliveries += int(success)
        client.reply(request.seqno)

    def publish_loop():
        nonlocal sent_count
        try:
            deadline = time.monotonic() + config["startup_timeout_seconds"]
            while True:
                try:
                    server.address()
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Publisher listener did not start")
                    time.sleep(0.005)
            write_json(folder / f"publisher-{identifier}.ready.json", {"pid": os.getpid()})
            control = wait_file(folder / "start.json", config["startup_timeout_seconds"] + 10)
            start_ns = control["start_ns"]
            end_ns = control["end_ns"]
            period_ns = 1e9 / config["publish_rate_hz"]
            slot = 0
            accepted = 0
            missed_releases = 0
            first_successful_send_ns = None
            last_successful_send_ns = None
            target_messages = config.get("message_count") or math.ceil(config["duration_seconds"] * config["publish_rate_hz"])
            while True:
                target = start_ns + int(slot * period_ns)
                if target >= end_ns:
                    break
                if sent_count >= target_messages:
                    break
                wait_until_ns(target)
                now = time.perf_counter_ns()
                if now >= end_ns:
                    break
                actual_slot = int((now - start_ns) / period_ns)
                if actual_slot > slot:
                    missed_releases += actual_slot - slot
                    slot = actual_slot
                sequence = sent_count
                sent_ns = time.perf_counter_ns()
                if config["recovery_enabled"]:
                    send_times.append(sent_ns)
                sent_count += 1
                logical_accepted = True
                for fragment_index, fragment in enumerate(fragments):
                    logical_accepted = server.publish("/bench/data", {"param": {"publisher": identifier,
                                                      "sequence": sequence, "send_ns": sent_ns, "recovery": False,
                                                      "subscriber_target": "broadcast_all", "payload_size_bytes": len(payload),
                                                      "checksum": payload_checksum,
                                                      "fragment_index": fragment_index, "fragment_count": len(fragments),
                                                      "logical_size_bytes": len(payload)}, "data": fragment},
                                                      quick=config["transport_mode"] == "udp") and logical_accepted
                accepted += int(logical_accepted)
                if logical_accepted:
                    first_successful_send_ns = first_successful_send_ns or sent_ns
                    last_successful_send_ns = sent_ns
                slot += 1
            planned = target_messages
            write_json(folder / f"publisher-{identifier}.sent.json", {
                "publisher": identifier, "messages_sent": sent_count, "publish_calls_accepted": accepted,
                "publish_calls_failed": sent_count - accepted, "planned_releases": planned,
                "unissued_releases": max(0, planned - sent_count), "scheduler_skipped_releases": missed_releases,
                "fragments_per_message": len(fragments), "fragment_size_bytes": fragment_bytes,
                "fragments_sent": sent_count * len(fragments),
                "offered_payload_mbps": sent_count * len(payload) * 8 / config["duration_seconds"] / 1e6,
                "first_successful_send_ns": first_successful_send_ns, "last_successful_send_ns": last_successful_send_ns,
                "finished_ns": time.perf_counter_ns(), "start_ns": start_ns, "end_ns": end_ns})
            wait_file(folder / "finish.json", config["recovery_timeout_seconds"] + config["drain_seconds"] + 30)
            write_json(folder / f"publisher-{identifier}.final.json", {"recovery_deliveries_sent": recovery_deliveries})
        except Exception as error:
            write_json(folder / f"publisher-{identifier}.error.json", {"error": str(error), "traceback": traceback.format_exc()})

    threading.Thread(target=publish_loop, daemon=True).start()
    server.run("127.0.0.1", spec["port"])


def subscriber(spec):
    config = spec["config"]
    folder = Path(spec["folder"])
    identifier = spec["identifier"]
    expected_payload = bytes(index % 251 for index in range(config["message_size_bytes"]))
    expected_checksum = f"crc32:{zlib.crc32(expected_payload):08x}"
    received = {}
    assemblies = {}
    lock = threading.Lock()
    clients = []
    threads = []
    invalid = 0
    duplicates = 0
    out_of_order = 0
    corrupted = 0
    unparseable = 0
    highest_sequence = {}
    expected_transport = config["transport_mode"] == "udp"

    def receive(client, url, payload, quick):
        nonlocal invalid, duplicates, out_of_order, corrupted, unparseable
        arrival_ns = time.perf_counter_ns()
        try:
            params = payload.param
            publisher_id, sequence, sent_ns = params["publisher"], params["sequence"], params["send_ns"]
            recovery = params["recovery"]
            fragment_index = params.get("fragment_index", 0)
            fragment_count = params.get("fragment_count", 1)
            logical_size = params.get("logical_size_bytes", config["message_size_bytes"])
            declared_size = params.get("payload_size_bytes")
            checksum = params.get("checksum")
            fragment = bytes(payload.data or b"")
            fragment_bytes = min(config.get("fragment_size_bytes", 60000), config["message_size_bytes"])
            expected_fragment = expected_payload[fragment_index * fragment_bytes:(fragment_index + 1) * fragment_bytes]
            valid = (0 <= publisher_id < len(spec["endpoints"]) and isinstance(sequence, int) and sequence >= 0
                     and isinstance(fragment_index, int) and isinstance(fragment_count, int)
                     and 0 <= fragment_index < fragment_count and logical_size == config["message_size_bytes"]
                     and declared_size == config["message_size_bytes"] and checksum == expected_checksum
                     and fragment == expected_fragment and arrival_ns >= sent_ns
                     and quick == (False if recovery else expected_transport))
        except (KeyError, TypeError, ValueError):
            valid = False
            unparseable += 1
        with lock:
            if not valid:
                invalid += 1
                corrupted += 1
                return
            key = (publisher_id, sequence)
            fragment_key = (publisher_id, sequence, recovery)
            if key in received:
                duplicates += 1
            else:
                assembly = assemblies.setdefault(fragment_key, {"parts": set(), "count": fragment_count,
                                                  "sent_ns": sent_ns, "arrival_ns": arrival_ns})
                if fragment_index in assembly["parts"]:
                    duplicates += 1
                    return
                assembly["parts"].add(fragment_index)
                assembly["arrival_ns"] = max(assembly["arrival_ns"], arrival_ns)
                if len(assembly["parts"]) == assembly["count"]:
                    if not recovery and sequence < highest_sequence.get(publisher_id, -1):
                        out_of_order += 1
                    if not recovery:
                        highest_sequence[publisher_id] = max(sequence, highest_sequence.get(publisher_id, -1))
                    received[key] = (sent_ns, assembly["arrival_ns"], recovery)
                    del assemblies[fragment_key]

    try:
        for endpoint in spec["endpoints"]:
            client = vsoa.Client()
            clients.append(client)
            client.onmessage = receive
            client.ondata = receive
            result = client.connect(f"vsoa://127.0.0.1:{endpoint}", timeout=config["startup_timeout_seconds"])
            if result != vsoa.Client.CONNECT_OK:
                raise ConnectionError(f"Subscriber {identifier} connect returned {result}")
            thread = threading.Thread(target=client.run, daemon=True)
            threads.append(thread)
            thread.start()
            acknowledgement = threading.Event()
            success_values = []

            def onsubscribe(native, success):
                success_values.append(success)
                acknowledgement.set()

            if not client.subscribe("/bench/", onsubscribe, timeout=2) or not acknowledgement.wait(3) or not success_values[0]:
                raise TimeoutError("Subscription acknowledgement failed")
        write_json(folder / f"subscriber-{identifier}.ready.json", {"pid": os.getpid()})
        control = wait_file(folder / "start.json", config["startup_timeout_seconds"] + 10)
        cutoff_ns = control["end_ns"] + int(config["drain_seconds"] * 1e9)
        wait_until_ns(cutoff_ns)
        counts = wait_file(folder / "counts.json", 10)["publishers"]
        with lock:
            initial = {key: value for key, value in received.items() if value[1] <= cutoff_ns and not value[2]}
        initial_keys = set(initial)
        missing = {publisher_id: [sequence for sequence in range(count)
                                  if (publisher_id, sequence) not in initial_keys]
                   for publisher_id, count in enumerate(counts)}
        initial_differences = []
        for publisher_id in range(len(counts)):
            ordered = sorted((sequence, value) for (source, sequence), value in initial.items() if source == publisher_id)
            for previous, current in zip(ordered, ordered[1:]):
                if current[0] == previous[0] + 1:
                    first_delay = previous[1][1] - previous[1][0]
                    second_delay = current[1][1] - current[1][0]
                    initial_differences.append(abs(second_delay - first_delay) / 1e6)
        recovery_started = time.perf_counter_ns()
        retry_requests = 0
        recovery_errors = []
        if config["recovery_enabled"]:
            deadline = time.monotonic() + config["recovery_timeout_seconds"]
            for publisher_id, sequences in missing.items():
                for offset in range(0, len(sequences), 64):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        recovery_errors.append("Recovery phase timeout")
                        break
                    acknowledged = threading.Event()
                    response_status = []

                    def replayed(native, header, payload, statuses=response_status, event=acknowledged):
                        statuses.append(header.status if header is not None else None)
                        event.set()

                    requested = sequences[offset:offset + 64]
                    accepted = clients[publisher_id].call("/replay", payload={"param": {"sequences": requested}},
                                                         callback=replayed, timeout=min(remaining, 5))
                    retry_requests += len(requested)
                    if not accepted or not acknowledged.wait(min(remaining, 5) + 0.1) or response_status[0] != 0:
                        recovery_errors.append(f"Replay RPC failed: publisher {publisher_id}")
        with lock:
            final = dict(received)
        expected_count = sum(counts)
        valid_final = {key: value for key, value in final.items() if key[1] < counts[key[0]]}
        late = sum(key not in initial_keys and not value[2] for key, value in valid_final.items())
        recovered = sum(key not in initial_keys and value[2] for key, value in valid_final.items())
        links = []
        for publisher_id, count in enumerate(counts):
            link_initial = {key: value for key, value in initial.items() if key[0] == publisher_id}
            link_final = {key: value for key, value in valid_final.items() if key[0] == publisher_id}
            link_latencies = [(value[1] - value[0]) / 1e6 for value in link_initial.values()]
            ordered_link = sorted(link_initial.items())
            link_jitter = [abs((current[1][1] - current[1][0]) - (previous[1][1] - previous[1][0])) / 1e6
                           for previous, current in zip(ordered_link, ordered_link[1:])
                           if current[0][1] == previous[0][1] + 1]
            links.append({"publisher": publisher_id, "subscriber": identifier, "messages_sent": count,
                          "messages_received": len(link_initial), "messages_received_after_recovery": len(link_final),
                          "throughput_mbps": len(link_initial) * config["message_size_bytes"] * 8 / config["duration_seconds"] / 1e6,
                          "packet_loss": (count - len(link_initial)) / count if count else None,
                          "final_packet_loss": (count - len(link_final)) / count if count else None,
                          "latency_ms": stats(link_latencies), "jitter_ms": stats(link_jitter)})
        write_json(folder / f"subscriber-{identifier}.result.json", {
            "subscriber": identifier, "expected_deliveries": expected_count, "initial_received": len(initial),
            "final_received": len(valid_final), "invalid_messages": invalid, "duplicate_messages": duplicates,
            "corrupted_count": corrupted, "unparseable_count": unparseable,
            "out_of_range_messages": len(final) - len(valid_final), "out_of_order_count": out_of_order,
            "incomplete_logical_messages": len(assemblies), "links": links,
            "latencies_ms": [(value[1] - value[0]) / 1e6 for value in initial.values()],
            "jitter_samples_ms": initial_differences,
            "measurement_window_received": sum(value[1] <= control["end_ns"] for value in initial.values()),
            "late_native_deliveries": late, "application_recovered": recovered,
            "application_retry_requests": retry_requests, "recovery_errors": recovery_errors,
            "recovery_time_ms": (time.perf_counter_ns() - recovery_started) / 1e6 if config["recovery_enabled"] else None,
            "recovery_latencies_ms": [(value[1] - value[0]) / 1e6 for value in valid_final.values() if value[2]],
            "first_counted_receive_ns": min((value[1] for value in initial.values()), default=None),
            "last_counted_receive_ns": max((value[1] for value in initial.values()), default=None),
            "initial_cutoff_ns": cutoff_ns})
        wait_file(folder / "finish.json", 30)
    finally:
        for client in clients:
            client.close()
        for thread in threads:
            thread.join(timeout=2)


def position(spec):
    entries = spec["entries"]

    def onquery(query, reply):
        port = entries.get(query["name"])
        reply({"addr": "127.0.0.1", "port": port, "domain": socket.AF_INET} if port else None)

    vsoa.Position(onquery).run("127.0.0.1", spec["port"])


def run_worker(spec_path):
    spec = read_json(spec_path)
    try:
        if spec["kind"] == "publisher":
            publisher(spec)
        elif spec["kind"] == "subscriber":
            if spec["config"].get("test_kind") == "stability":
                from standalone.stability import subscriber as stable_subscriber
                stable_subscriber(spec)
            else:
                subscriber(spec)
        elif spec["kind"] == "position":
            position(spec)
        elif spec["kind"] == "proxy":
            from standalone.udp_proxy import serve_proxy
            serve_proxy(spec)
        elif spec["kind"].startswith("fault_"):
            from standalone.fault_workers import dispatch
            dispatch(spec)
        else:
            raise ValueError("Unknown internal worker")
    except Exception as error:
        write_json(Path(spec["folder"]) / f"{spec['kind']}-{spec.get('identifier', 0)}.error.json",
                   {"error": str(error), "traceback": traceback.format_exc()})
        raise
