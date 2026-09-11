"""Bidirectional VSOA relay with real server-to-subscriber UDP loss and delay."""

import contextlib
import heapq
import os
import random
import select
import socket
import struct
import threading
import time
from pathlib import Path

from standalone.io_utils import wait_file, write_json
from standalone.io_utils import read_json


class Relay:
    def __init__(self, mapping, config, seed):
        self.port = mapping["proxy_port"]
        self.upstream_address = ("127.0.0.1", mapping["publisher_port"])
        self.destination = None
        self.config = config
        self.random = random.Random(seed)
        self.stop = threading.Event()
        self.window_start_ns = None
        self.lock = threading.Lock()
        self.counts = {"seen": 0, "dropped": 0, "blackout_dropped": 0, "forwarded": 0, "delayed_pending": 0, "errors": []}
        self.tcp = socket.socket()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.channels = [self.tcp, self.udp]
        try:
            self.tcp.bind(("127.0.0.1", self.port))
            self.tcp.listen(1)
            self.udp.bind(("127.0.0.1", self.port))
            self.tcp.settimeout(0.1)
        except Exception:
            for channel in self.channels:
                channel.close()
            raise
        self.threads = [threading.Thread(target=self.tcp_loop, daemon=True),
                        threading.Thread(target=self.udp_loop, daemon=True)]
        for thread in self.threads:
            thread.start()

    @staticmethod
    def read_exact(channel, count):
        buffer = bytearray()
        while len(buffer) < count:
            data = channel.recv(count - len(buffer))
            if not data:
                raise ConnectionError("Client closed during VSOA handshake")
            buffer.extend(data)
        return buffer

    def tcp_loop(self):
        downstream = upstream = None
        try:
            while not self.stop.is_set():
                try:
                    downstream, remote = self.tcp.accept()
                    break
                except socket.timeout:
                    continue
            if downstream is None:
                return
            downstream.settimeout(5)
            upstream = socket.create_connection(self.upstream_address, timeout=5)
            with self.lock:
                self.channels.extend([downstream, upstream])
            header = self.read_exact(downstream, 20)
            if header[0] != 0x29 or header[1] != 0 or not header[2] & 2:
                raise ValueError("Expected VSOA 2 SERVINFO with quick-channel port")
            client_port = struct.unpack_from(">H", header, 8)[0]
            self.destination = ("127.0.0.1", client_port)
            length = sum(struct.unpack_from(">HII", header, 10)) + ((header[2] >> 6) & 3)
            if length > 262124:
                raise ValueError("Invalid VSOA handshake length")
            body = self.read_exact(downstream, length)
            struct.pack_into(">H", header, 8, self.port)
            upstream.sendall(header + body)
            peers = {downstream: upstream, upstream: downstream}
            while not self.stop.is_set():
                readable, writable, exceptional = select.select(list(peers), [], [], 0.1)
                for channel in readable:
                    data = channel.recv(65536)
                    if not data:
                        return
                    peers[channel].sendall(data)
        except OSError as error:
            if not self.stop.is_set():
                self.counts["errors"].append(str(error))
        except Exception as error:
            self.counts["errors"].append(str(error))
        finally:
            for channel in (downstream, upstream):
                if channel:
                    with contextlib.suppress(OSError):
                        channel.close()

    def udp_loop(self):
        delayed = []
        serial = 0
        while not self.stop.is_set():
            try:
                now = time.perf_counter()
                while delayed and delayed[0][0] <= now:
                    due, order, data = heapq.heappop(delayed)
                    self.udp.sendto(data, self.destination)
                    self.counts["forwarded"] += 1
                self.counts["delayed_pending"] = len(delayed)
                timeout = min(0.05, max(0, delayed[0][0] - now)) if delayed else 0.05
                readable, writable, exceptional = select.select([self.udp], [], [], timeout)
                if not readable:
                    continue
                data, source = self.udp.recvfrom(65535)
                if source == self.upstream_address:
                    self.counts["seen"] += 1
                    offset = (time.perf_counter_ns() - self.window_start_ns) / 1e9 if self.window_start_ns else -1
                    blackout_start = self.config.get("blackout_offset_seconds", -2)
                    blackout_end = blackout_start + self.config.get("blackout_duration_seconds", 0)
                    if blackout_start <= offset < blackout_end:
                        self.counts["dropped"] += 1
                        self.counts["blackout_dropped"] += 1
                        continue
                    if self.random.random() < self.config["loss_rate"]:
                        self.counts["dropped"] += 1
                        continue
                    if self.destination is None:
                        raise RuntimeError("UDP data arrived before handshake mapping")
                    delay_ms = max(0, self.config["network_delay_ms"] + self.random.uniform(
                        -self.config["network_jitter_ms"], self.config["network_jitter_ms"]))
                    heapq.heappush(delayed, (time.perf_counter() + delay_ms / 1000, serial, data))
                    serial += 1
                elif source == self.destination:
                    self.udp.sendto(data, self.upstream_address)
            except OSError as error:
                if not self.stop.is_set():
                    self.counts["errors"].append(str(error))
                return
            except Exception as error:
                self.counts["errors"].append(str(error))
                return

    def close(self):
        self.stop.set()
        with self.lock:
            for channel in self.channels:
                with contextlib.suppress(OSError):
                    channel.close()
        for thread in self.threads:
            thread.join(timeout=2)


def serve_proxy(spec):
    relays = []
    folder = Path(spec["folder"])
    try:
        for index, mapping in enumerate(spec["mappings"]):
            relays.append(Relay(mapping, spec["config"], spec["config"]["seed"] + index))
        write_json(folder / "proxy-0.ready.json", {"pid": os.getpid()})
        control = wait_file(folder / "start.json", spec["config"]["startup_timeout_seconds"] + 10)
        for relay in relays:
            relay.window_start_ns = control["start_ns"]
        wait_file(folder / "finish.json", spec["config"]["startup_timeout_seconds"]
                  + spec["config"]["duration_seconds"] + spec["config"]["recovery_timeout_seconds"] + 60)
        write_json(folder / "proxy-0.result.json", {"relays": [{"port": relay.port, **relay.counts} for relay in relays]})
    finally:
        for relay in relays:
            relay.close()
