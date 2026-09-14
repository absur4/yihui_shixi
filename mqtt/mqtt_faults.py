"""A local TCP relay ONLY for S11 connection outage/remote close tests.

It does not claim packet-loss injection. Infrastructure resources are recorded separately.
"""
import select
import socket
import threading
import time


def relay_worker(upstream_port, ready, stop, pause, close_generation):
    threads = []
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(64)
        listener.settimeout(.1)
        ready.put(listener.getsockname()[1])

        def connection(client):
            generation = close_generation.value
            try:
                with client, socket.create_connection(('127.0.0.1', upstream_port), timeout=2) as upstream:
                    client.settimeout(.5)
                    upstream.settimeout(.5)
                    while not stop.is_set():
                        if close_generation.value != generation:
                            return  # FIN both legs: remote connection close, broker stays running
                        if pause.is_set():
                            time.sleep(.02)  # stop all forwarding during the network outage
                            continue
                        readable, _, _ = select.select([client, upstream], [], [], .1)
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            (upstream if source is client else client).sendall(data)
            except OSError:
                pass

        while not stop.is_set():
            try:
                client, _ = listener.accept()
                t = threading.Thread(target=connection, args=(client,), daemon=True)
                t.start()
                threads.append(t)
            except socket.timeout:
                pass
        for thread in threads:
            thread.join(.5)
