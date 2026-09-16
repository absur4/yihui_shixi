"""One MQTT endpoint per process; only local monotonic timestamps enter raw files."""
from __future__ import annotations

import argparse
from collections import deque
import csv
import math
import os
from pathlib import Path
import random
import signal
import struct
import threading
import time
import zlib

from .common import read_json, token, write_json
from .wire import decode, encode

PUBLISHER_FIELDS = ("sequence", "send_ns", "return_ns", "accepted", "rc", "phase", "payload_bytes", "api_ms", "api_called")
SUBSCRIBER_FIELDS = ("publisher_id", "sequence", "send_ns", "receive_ns", "phase", "validation", "payload_bytes", "wire_bytes")
TIMING_FIELDS = struct.Struct("!QQ")


def planned_count(duration_s, rate_hz):
    return math.ceil(duration_s * rate_hz - 1e-9)


def due_slot(now_ns, start_ns, rate_hz, next_slot):
    """Discard overdue schedule slots rather than generating catch-up bursts."""
    return max(next_slot, (max(0, now_ns - start_ns) * rate_hz) // 1_000_000_000)


def _safe_error(error):
    secret = os.environ.get("MQTT_CD_TOKEN", "")
    message = str(error)
    return message.replace(secret, "<redacted>") if secret else message


def endpoint(spec_path, role, index):
    import paho.mqtt.client as mqtt

    spec_path = Path(spec_path)
    folder = spec_path.parent
    spec = read_json(spec_path)
    case = spec["case"]
    summary = dict(role=role, index=index, status="starting", errors=[], protocol_completed=0,
                   warmup_protocol_completed=0, pending_peak=0, max_pending_bytes=0,
                   queue_rejected=0, api_rejected=0, skipped_sends=0, discarded_sends=0,
                   accepted=0, received=0, invalid=0, pending_at_deadline=0,
                   planned=planned_count(case["duration_s"], case["rate_hz"]),
                   process_start_ns=time.perf_counter_ns(), stage="preparing", failure_origin=None)
    connected, ready = threading.Event(), threading.Event()
    stopping = threading.Event()
    pending = deque()
    pending_bytes = 0
    delivery_deadline_ns = None
    formal_start_ns = None
    topic = f"mqtt-cd/{spec['run_id']}/data"
    output_path = folder / f"{role}-{index}.csv"
    summary_path = folder / f"worker-{role}-{index}.summary.json"
    output = output_path.open("w", newline="", encoding="utf-8", buffering=1024 * 1024)
    writer = csv.writer(output)
    writer.writerow(PUBLISHER_FIELDS if role == "publisher" else SUBSCRIBER_FIELDS)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"cd-{spec['run_id']}-{role[0]}{index}",
                         protocol=mqtt.MQTTv311, clean_session=True)
    client.username_pw_set("benchmark", token())
    client.max_inflight_messages_set(case["queue_messages"])
    client.max_queued_messages_set(case["queue_messages"])
    client.reconnect_delay_set(1, 2)
    client.connect_timeout = case["connect_timeout_s"]
    timer = None

    def cancelled():
        return stopping.is_set() or (folder / "cancel").exists()

    def current_stage():
        if formal_start_ns is None:
            return "ready" if ready.is_set() else "preparing"
        now = time.perf_counter_ns()
        if now < formal_start_ns - round((case["warmup_s"] + case["settle_s"]) * 1e9):
            return "armed"
        if now < formal_start_ns - round(case["settle_s"] * 1e9):
            return "warmup"
        if now < formal_start_ns:
            return "settle"
        if now < formal_start_ns + round(case["duration_s"] * 1e9):
            return "formal"
        return "drain" if now < delivery_deadline_ns else "diagnostic"

    def error(message, origin=None):
        if summary["failure_origin"] is None:
            summary["stage"] = current_stage()
            summary["failure_origin"] = ("controller" if (folder / "cancel").exists() else
                (origin or ("dut" if summary["stage"] in ("formal", "drain") else "configuration")))
            summary["failed_at_ns"] = time.perf_counter_ns()
        if len(summary["errors"]) < 100:
            summary["errors"].append(_safe_error(message))

    def on_connect(c, userdata, flags, reason, properties):
        if reason.is_failure:
            error(f"CONNACK rejected: {reason}")
            stopping.set()
            return
        connected.set()
        if role == "subscriber":
            rc, _ = c.subscribe(topic, qos=case["qos"])
            if rc != mqtt.MQTT_ERR_SUCCESS:
                error(f"SUBSCRIBE failed: {rc}")
                stopping.set()
        else:
            ready.set()

    def on_subscribe(c, userdata, mid, reasons, properties):
        if not reasons or any(reason.is_failure for reason in reasons):
            error("SUBACK rejected")
            stopping.set()
        else:
            ready.set()

    def on_disconnect(c, userdata, flags, reason, properties):
        connected.clear()
        if not stopping.is_set() and (delivery_deadline_ns is None or time.perf_counter_ns() < delivery_deadline_ns):
            error(f"MQTT connection interrupted: {reason}")

    def on_message(c, userdata, message):
        received_ns = time.perf_counter_ns()
        try:
            record = decode(message.payload, spec["run_id"], case["payload_bytes"], case["publishers"])
            record["receive_ns"] = received_ns
            writer.writerow([record[field] for field in SUBSCRIBER_FIELDS])
            summary["received"] += 1
            summary["invalid"] += int(record["validation"] != "ok")
        except Exception as exc:
            error(f"Receive callback: {_safe_error(exc)}", "measurement")
            stopping.set()

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    def reap_pending():
        nonlocal pending_bytes
        while pending:
            info, size, phase = pending[0]
            try:
                done = info.is_published()
            except (ValueError, RuntimeError):
                done = False
            if not done:
                break
            pending.popleft()
            pending_bytes -= size
            summary["protocol_completed" if phase == 1 else "warmup_protocol_completed"] += 1

    def wait_until(deadline):
        while not cancelled():
            left = (deadline - time.perf_counter_ns()) / 1e9
            if left <= 0:
                return True
            if left > .001:
                stopping.wait(min(.025, max(.0001, left - .0005)))
            # The final fraction of a millisecond is busy-waited for reproducible pacing.
        return False

    def send_window(begin_ns, duration_s, phase, payload, crc, rate_hz):
        nonlocal pending_bytes
        count = planned_count(duration_s, rate_hz)
        end_ns = begin_ns + round(duration_s * 1e9)
        template = bytearray(encode(spec["run_id"], index, 0, 0, phase, payload, crc))
        seq = 0
        while seq < count and not cancelled():
            now = time.perf_counter_ns()
            if now >= end_ns:
                if phase == 1:
                    summary["skipped_sends"] += count - seq
                break
            slot = due_slot(now, begin_ns, rate_hz, seq)
            if slot > seq and phase == 1:
                summary["skipped_sends"] += min(slot, count) - seq
            seq = slot
            if seq >= count:
                break
            due = begin_ns + seq * 1_000_000_000 // rate_hz
            if not wait_until(due):
                break
            if time.perf_counter_ns() >= end_ns:
                if phase == 1:
                    summary["skipped_sends"] += count - seq
                break
            reap_pending()
            sent = time.perf_counter_ns()
            if sent >= end_ns:
                if phase == 1:
                    summary["skipped_sends"] += count - seq
                break
            if len(pending) >= case["queue_messages"] or pending_bytes + len(payload) > case["queue_bytes"]:
                if phase == 1:
                    summary["queue_rejected"] += 1
                    summary["discarded_sends"] += 1
                writer.writerow((seq, sent, sent, 0, int(mqtt.MQTT_ERR_QUEUE_SIZE), phase, len(payload), 0.0, 0))
            else:
                packet = template.copy()
                sent = time.perf_counter_ns()
                if sent >= end_ns:
                    if phase == 1:
                        summary["skipped_sends"] += count - seq
                    break
                # Each publish owns its immutable-in-practice buffer, including queued QoS retries.
                TIMING_FIELDS.pack_into(packet, 28, seq, sent)
                call_ns = time.perf_counter_ns()
                info = client.publish(topic, packet, qos=case["qos"], retain=False)
                returned = time.perf_counter_ns()
                accepted = info.rc == mqtt.MQTT_ERR_SUCCESS
                writer.writerow((seq, sent, returned, int(accepted), int(info.rc), phase,
                                 len(payload), (returned - call_ns) / 1e6, 1))
                if accepted:
                    pending.append((info, len(payload), phase))
                    pending_bytes += len(payload)
                    summary["pending_peak"] = max(summary["pending_peak"], len(pending))
                    summary["max_pending_bytes"] = max(summary["max_pending_bytes"], pending_bytes)
                    if phase == 1:
                        summary["accepted"] += 1
                elif phase == 1:
                    summary["api_rejected"] += 1
                    summary["discarded_sends"] += 1
            seq += 1
        output.flush()

    try:
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        if os.name == "nt":
            import ctypes
            timer = ctypes.WinDLL("winmm")
            timer.timeBeginPeriod(1)
        client.connect_async(spec["broker_host"], spec["broker_port"], keepalive=10)
        client.loop_start()
        connect_deadline = time.perf_counter() + case["connect_timeout_s"]
        while not ready.wait(.05):
            if cancelled():
                raise RuntimeError("Cancelled while awaiting MQTT readiness")
            if time.perf_counter() >= connect_deadline:
                raise TimeoutError("MQTT CONNACK/SUBACK timed out")
        write_json(folder / f"worker-{role}-{index}.ready.json",
                   dict(role=role, index=index, ready_ns=time.perf_counter_ns(), pid=os.getpid()))
        arm_deadline = time.perf_counter() + 120
        while not (folder / "arm.json").exists():
            if cancelled():
                raise RuntimeError("Cancelled before arming")
            if time.perf_counter() >= arm_deadline:
                raise TimeoutError("Controller did not arm within 120 seconds")
            stopping.wait(.025)
        start_ns = int(read_json(folder / "arm.json")["start_ns"])
        formal_start_ns = start_ns
        delivery_deadline_ns = start_ns + round((case["duration_s"] + case["drain_s"]) * 1e9)
        finish_ns = delivery_deadline_ns + 1_000_000_000
        summary.update(start_ns=start_ns, end_ns=start_ns + round(case["duration_s"] * 1e9),
                       deadline_ns=delivery_deadline_ns, diagnostic_end_ns=finish_ns, status="running")
        if role == "publisher":
            payload = random.Random(case["random_seed"] + index).randbytes(case["payload_bytes"])
            crc = zlib.crc32(payload)
            warm_start = start_ns - round((case["warmup_s"] + case["settle_s"]) * 1e9)
            if not wait_until(warm_start):
                raise RuntimeError("Cancelled before warmup")
            if case["warmup_s"]:
                warm_payload = random.Random(case["random_seed"] + index).randbytes(1024)
                send_window(warm_start, case["warmup_s"], 0, warm_payload, zlib.crc32(warm_payload), 100)
            while time.perf_counter_ns() < start_ns and not cancelled():
                reap_pending()
                stopping.wait(min(.01, max(0, (start_ns - time.perf_counter_ns()) / 1e9 - .0005)))
            summary["pending_at_formal_start"] = len(pending)
            if pending:
                raise RuntimeError("Warmup queue did not clear during settle; formal measurement invalid")
            send_window(start_ns, case["duration_s"], 1, payload, crc, case["rate_hz"])
        while time.perf_counter_ns() < delivery_deadline_ns and not cancelled():
            if role == "publisher":
                reap_pending()
            output.flush()
            stopping.wait(min(.05, max(0, (delivery_deadline_ns - time.perf_counter_ns()) / 1e9)))
        reap_pending()
        summary["pending_at_deadline"] = len(pending)
        while time.perf_counter_ns() < finish_ns and not cancelled():
            output.flush()
            stopping.wait(min(.05, max(0, (finish_ns - time.perf_counter_ns()) / 1e9)))
        summary["status"] = "cancelled" if (folder / "cancel").exists() else ("error" if summary["errors"] else "completed")
        if summary["status"] == "cancelled":
            error("Controller cancelled endpoint", "controller")
    except Exception as exc:
        error(exc, "measurement" if isinstance(exc, (OSError, ValueError)) else None)
        summary["status"] = "cancelled" if (folder / "cancel").exists() else "error"
    finally:
        stopping.set()
        try:
            client.disconnect()
            client.loop_stop()
        except Exception as exc:
            error(exc, "cleanup")
            summary["status"] = "error"
        try:
            output.close()
        except OSError as exc:
            error(exc, "measurement")
            summary["status"] = "error"
        if summary["status"] == "completed":
            summary["stage"] = "completed"
        summary["process_end_ns"] = time.perf_counter_ns()
        write_json(summary_path, summary)
        if timer:
            timer.timeEndPeriod(1)
    return 0 if summary["status"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--role", choices=("publisher", "subscriber"), required=True)
    parser.add_argument("--index", type=int, required=True)
    args = parser.parse_args()
    return endpoint(args.spec, args.role, args.index)


if __name__ == "__main__":
    raise SystemExit(main())
