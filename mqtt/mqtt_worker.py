"""Each endpoint runs in its own measured OS process; the harness is separate."""
import csv
import json
import random
import threading
import time
import uuid
import os
from pathlib import Path

import paho.mqtt.client as mqtt

from mqtt_wire import HEADER, MAGIC, pack, unpack


def endpoint(role, index, cfg, run_id, folder, *args):
    import contextlib
    import logging
    import signal
    # Parent owns graceful cancellation and sets the shared stop event.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logs = Path(cfg.get('logs_dir', folder)) / run_id
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f'{role}-{index}.log').open('a', encoding='utf-8', buffering=1) as log:
        logging.basicConfig(stream=log, level=logging.WARNING, force=True)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            try:
                return _endpoint(role, index, cfg, run_id, folder, *args)
            except BaseException:
                import traceback
                traceback.print_exc()
                raise


def _endpoint(role, index, cfg, run_id, folder, ready, start, stop, recovery,
             start_ns, restart=False, connect_gate=None):
    timer = None
    if os.name == 'nt':
        import ctypes
        timer = ctypes.WinDLL('winmm')
        timer.timeBeginPeriod(1)
    folder = Path(folder)
    connected, subscribed = threading.Event(), threading.Event()
    run_bytes = uuid.UUID(run_id).bytes
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f'{run_id[:12]}-{role}-{index}',
                         protocol=mqtt.MQTTv311, clean_session=False)
    client.max_inflight_messages_set(cfg['max_inflight'])
    client.max_queued_messages_set(cfg['max_inflight'] * 4)
    client.reconnect_delay_set(1, 2)
    client.enable_logger()
    topic = f"unified/{run_id}/data"
    conn_start = time.perf_counter_ns()
    events = open(folder / f'{role}-{index}-events.jsonl', 'a', encoding='utf-8')
    output = open(folder / f'{role}-{index}.csv', 'a', newline='', encoding='utf-8', buffering=1024*1024)
    writer = csv.writer(output)
    first_ready = [False]

    def event(name, **extra):
        events.write(json.dumps(dict(event=name, timestamp_ns=time.perf_counter_ns(), **extra)) + '\n')
        events.flush()

    def on_connect(c, u, flags, reason, props):
        event('connack', reason=str(reason), session_present=flags.session_present)
        if reason.is_failure:
            ready.put(dict(role=role, index=index, error=str(reason)))
            return
        connected.set()
        if role == 'subscriber':
            c.subscribe(topic, qos=cfg['qos'])
        elif not first_ready[0]:
            first_ready[0] = True
            ready.put(dict(role=role, index=index, conn_start_ns=conn_start,
                           ready_ns=time.perf_counter_ns()))

    def on_subscribe(c, u, mid, reasons, props):
        event('suback', reasons=[str(r) for r in reasons])
        if any(r.is_failure for r in reasons):
            ready.put(dict(role=role, index=index, error='SUBACK rejected'))
            return
        subscribed.set()
        if not first_ready[0]:
            first_ready[0] = True
            ready.put(dict(role=role, index=index, conn_start_ns=conn_start,
                           ready_ns=time.perf_counter_ns()))

    def on_disconnect(c, u, flags, reason, props):
        event('disconnect', reason=str(reason))
        connected.clear()

    def on_message(c, u, message):
        received_ns = time.perf_counter_ns()  # callback entry, before decoding/checksum
        row, status = unpack(message.payload, run_bytes, cfg['payload_size_bytes'], cfg['publisher_count'])
        if row is None:
            row = (-1, -1, 0, -1)
        pub, seq, sent, phase = row
        writer.writerow([pub, seq, sent, received_ns, phase, status, len(message.payload),
                         int(recovery.is_set())])

    client.on_connect, client.on_subscribe = on_connect, on_subscribe
    client.on_disconnect, client.on_message = on_disconnect, on_message
    try:
        if connect_gate is not None:
            ready.put(dict(role=role, index=index, event='initialized'))
            while not connect_gate.wait(.1):
                if stop.is_set():
                    return
        conn_start = time.perf_counter_ns()
        client.connect(cfg['host'], cfg['port'], keepalive=5)
        client.loop_start()
        if not (subscribed if role == 'subscriber' else connected).wait(cfg['connect_timeout_seconds']):
            raise TimeoutError('MQTT CONNACK/SUBACK timed out')
        while not start.wait(.1):
            if stop.is_set():
                return
        if role == 'subscriber':
            while not stop.wait(.2):
                output.flush()
        else:
            payload = random.Random(cfg['random_seed'] + index).randbytes(cfg['payload_size_bytes'])
            # Cache CRC/header invariant work outside the timed send path.
            import zlib
            crc = zlib.crc32(payload)

            def send(seq, phase):
                sent = time.perf_counter_ns()
                message = HEADER.pack(MAGIC, run_bytes, index, 65535, seq, sent,
                                      len(payload), crc, phase) + payload
                info = client.publish(topic, message, qos=cfg['qos'])
                ok = info.rc == mqtt.MQTT_ERR_SUCCESS
                if ok:
                    # Successful means handed to transport (QoS0) / protocol acknowledged (QoS1/2).
                    info.wait_for_publish(timeout=cfg['publish_timeout_seconds'])
                    ok = info.is_published()
                writer.writerow([seq, sent, time.perf_counter_ns(), phase, int(ok), int(info.rc)])
                return ok

            if not restart:
                seq = 0
                while time.perf_counter_ns() < start_ns.value and not stop.is_set():
                    send(seq, 0)
                    seq += 1
                    time.sleep(.01)
                formal_start = start_ns.value / 1e9
                limit = cfg['message_count'] or None
                deadline = formal_start + cfg['duration_seconds'] if not limit else float('inf')
                rate = cfg['publish_rate_hz']
                seq = 0
                while not stop.is_set() and (limit is None or seq < limit):
                    now = time.perf_counter()
                    if now >= deadline:
                        break
                    due = formal_start + seq / rate if rate else now
                    # Do not burst to catch up: missed duration slots are recorded as not sent.
                    if rate and not limit and now > due + 1 / rate:
                        seq = max(seq, int((now - formal_start) * rate))
                        due = formal_start + seq / rate
                    if due >= deadline:
                        break
                    if due > now:
                        if due - now > .0015:
                            stop.wait(due - now - .0005)
                        while time.perf_counter() < due and not stop.is_set():
                            pass
                    if stop.is_set():
                        break
                    send(seq, 1)
                    seq += 1
                if not limit and not stop.is_set():
                    stop.wait(max(0, deadline-time.perf_counter()))
                output.flush()
                ready.put(dict(role=role, index=index, event='sent', timestamp_ns=time.perf_counter_ns()))
            while not recovery.wait(.1):
                if stop.is_set():
                    return
            event('recovery_probes_started')
            first_probe = 0
            if restart:
                with open(folder / f'publisher-{index}.csv', encoding='utf-8') as previous:
                    first_probe = max((int(r[0])+1 for r in csv.reader(previous) if int(r[3])==2), default=0)
            for seq in range(first_probe, first_probe+cfg['recovery_probe_count']):
                if stop.is_set():
                    break
                if connected.wait(.1):
                    try:
                        send(seq, 2)
                    except (RuntimeError, ValueError) as exc:
                        event('probe_error', error=str(exc))
                output.flush()
                stop.wait(.02)
            output.flush()
            while not stop.wait(.1):
                pass
    except Exception as exc:
        event('error', error=repr(exc))
        ready.put(dict(role=role, index=index, error=repr(exc)))
        raise
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        finally:
            output.close()
            events.close()
            if timer:
                timer.timeEndPeriod(1)
