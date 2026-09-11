"""TCP control relay plus seeded, client-to-server UDP datagram loss injection."""

import contextlib
import random
import select
import socket
import threading

from bench.common import free_port


class LossProxy:
    def __init__(self, target_port, loss_rate, seed):
        self.port = free_port()
        self.target = ("127.0.0.1", target_port)
        self.loss_rate = loss_rate
        self.random = random.Random(seed)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.seen = 0
        self.dropped = 0
        self.forwarded = 0
        self.errors = []
        self.tcp = socket.socket()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sockets = [self.tcp, self.udp]
        try:
            self.tcp.bind(("127.0.0.1", self.port))
            self.tcp.listen(1)
            self.tcp.settimeout(0.1)
            self.udp.bind(("127.0.0.1", self.port))
            self.udp.settimeout(0.1)
        except Exception:
            for channel in self.sockets:
                channel.close()
            raise
        self.threads = [threading.Thread(target=self.relay_tcp, daemon=True),
                        threading.Thread(target=self.relay_udp, daemon=True)]
        for thread in self.threads:
            thread.start()

    def relay_tcp(self):
        downstream = upstream = None
        try:
            while not self.stop.is_set():
                try:
                    downstream, address = self.tcp.accept()
                    break
                except socket.timeout:
                    continue
            if downstream is None:
                return
            upstream = socket.create_connection(self.target, timeout=2)
            downstream.settimeout(2)
            with self.lock:
                self.sockets.extend([downstream, upstream])
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
                self.errors.append(str(error))
        finally:
            for channel in (downstream, upstream):
                if channel is not None:
                    with contextlib.suppress(OSError):
                        channel.close()

    def relay_udp(self):
        while not self.stop.is_set():
            try:
                data, source = self.udp.recvfrom(65535)
                with self.lock:
                    self.seen += 1
                    if self.random.random() < self.loss_rate:
                        self.dropped += 1
                    else:
                        self.udp.sendto(data, self.target)
                        self.forwarded += 1
            except socket.timeout:
                continue
            except OSError as error:
                if not self.stop.is_set():
                    self.errors.append(str(error))
                return

    def snapshot(self):
        with self.lock:
            return {"seen": self.seen, "dropped": self.dropped,
                    "forwarded": self.forwarded, "errors": list(self.errors)}

    def close(self):
        self.stop.set()
        with self.lock:
            for channel in self.sockets:
                with contextlib.suppress(OSError):
                    channel.close()
        for thread in self.threads:
            thread.join(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
