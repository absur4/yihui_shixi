"""Authenticated VSOA worker agent used by the multi-machine coordinator."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hmac
import json
import os
import platform
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psutil
import vsoa

from standalone.processes import ProcessGroup


SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class Run:
    def __init__(self, root: Path, run_id: str, owner: str | None = None):
        self.folder = root / run_id
        self.owner = owner
        self.group = ProcessGroup(self.folder, self.folder / "logs")
        self.roles: dict[str, object] = {}

    def start(self, spec):
        name = f"{spec['kind']}-{spec.get('identifier', 0)}"
        if name in self.roles:
            raise ValueError(f"worker already exists: {name}")
        process = self.group.start(spec)
        self.roles[name] = process
        return {"name": name, "pid": process.pid}

    def resources(self):
        output = {}
        for name, child in self.roles.items():
            try:
                process = psutil.Process(child.pid)
                family = [process, *process.children(recursive=True)]
                cpu = sum(item.cpu_times().user + item.cpu_times().system for item in family)
                rss = sum(item.memory_info().rss for item in family)
                output[name] = {"pid": child.pid, "cpu_seconds": cpu, "rss_bytes": rss}
            except psutil.Error:
                continue
        return output

    def status(self):
        errors = []
        for path in self.folder.glob("*.error.json"):
            with contextlib.suppress(Exception):
                errors.append(json.loads(path.read_text(encoding="utf-8")))
        return {"workers": {name: {"pid": child.pid, "returncode": child.poll()}
                            for name, child in self.roles.items()}, "errors": errors}

    def close(self):
        self.group.close()


class AgentState:
    def __init__(self, root, token, advertise_host):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.advertise_host = advertise_host
        self.runs: dict[str, Run] = {}
        self.lock = threading.RLock()

    def run(self, run_id, create=False, owner=None):
        if not SAFE_NAME.fullmatch(run_id or ""):
            raise ValueError("invalid run id")
        if owner is not None and not SAFE_NAME.fullmatch(owner):
            raise ValueError("invalid owner")
        with self.lock:
            if create:
                run = self.runs.setdefault(run_id, Run(self.root, run_id, owner))
                if owner and run.owner not in {None, owner}:
                    raise ValueError("run owner mismatch")
                run.owner = run.owner or owner
                return run
            if run_id not in self.runs:
                raise ValueError("run not found")
            return self.runs[run_id]

    def stop(self, run_id):
        with self.lock:
            run = self.runs.pop(run_id, None)
        if run:
            run.close()

    def stop_owner(self, owner):
        if not SAFE_NAME.fullmatch(owner or ""):
            raise ValueError("invalid owner")
        with self.lock:
            ids = [run_id for run_id, run in self.runs.items() if run.owner == owner]
        for run_id in ids:
            self.stop(run_id)
        return len(ids)

    def close(self):
        with self.lock:
            ids = list(self.runs)
        for run_id in ids:
            self.stop(run_id)


def handler_for(state: AgentState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VSOA-Agent/1.0"

        def log_message(self, fmt, *args):
            print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

        def authorized(self):
            supplied = self.headers.get("X-VSOA-Agent-Token", "")
            return bool(state.token) and hmac.compare_digest(supplied, state.token)

        def reply(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 20 * 1024 * 1024:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(size) or b"{}")

        def do_GET(self):
            if not self.authorized():
                self.reply({"error": "unauthorized"}, 403)
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/v1/health":
                    self.reply({"ok": True, "advertise_host": state.advertise_host,
                                "machine": platform.node(), "os": platform.platform(),
                                "python": platform.python_version(), "vsoa": vsoa.__version__})
                elif parsed.path == "/v1/clock":
                    self.reply({"monotonic_ns": time.perf_counter_ns(), "wall_time_ns": time.time_ns()})
                elif parsed.path == "/v1/status":
                    self.reply(state.run(query.get("run_id", [""])[0]).status())
                elif parsed.path == "/v1/resources":
                    self.reply(state.run(query.get("run_id", [""])[0]).resources())
                elif parsed.path == "/v1/file":
                    run = state.run(query.get("run_id", [""])[0])
                    name = query.get("name", [""])[0]
                    if not SAFE_NAME.fullmatch(name):
                        raise ValueError("invalid file name")
                    path = run.folder / name
                    if not path.is_file():
                        self.reply({"exists": False})
                    else:
                        self.reply({"exists": True, "data": base64.b64encode(path.read_bytes()).decode("ascii")})
                else:
                    self.reply({"error": "not found"}, 404)
            except Exception as error:
                self.reply({"error": str(error)}, 400)

        def do_POST(self):
            if not self.authorized():
                self.reply({"error": "unauthorized"}, 403)
                return
            try:
                body = self.body()
                if self.path == "/v1/start":
                    run = state.run(body.get("run_id"), create=True, owner=body.get("owner"))
                    spec = dict(body.get("spec") or {})
                    spec.setdefault("bind_host", "0.0.0.0")
                    self.reply(run.start(spec), 201)
                elif self.path == "/v1/file":
                    run = state.run(body.get("run_id"), create=True)
                    name = body.get("name", "")
                    if not SAFE_NAME.fullmatch(name):
                        raise ValueError("invalid file name")
                    data = base64.b64decode(body.get("data", ""), validate=True)
                    temporary = run.folder / f".{name}.tmp"
                    temporary.write_bytes(data)
                    os.replace(temporary, run.folder / name)
                    self.reply({"ok": True})
                elif self.path == "/v1/stop":
                    state.stop(body.get("run_id"))
                    self.reply({"ok": True})
                elif self.path == "/v1/stop-owner":
                    self.reply({"ok": True, "stopped": state.stop_owner(body.get("owner"))})
                else:
                    self.reply({"error": "not found"}, 404)
            except Exception as error:
                self.reply({"error": str(error)}, 400)

    return Handler


def serve(host, port, token, root, advertise_host):
    if not token:
        raise ValueError("agent token must not be empty")
    advertised = advertise_host or socket.gethostbyname(socket.gethostname())
    state = AgentState(root, token, advertised)
    server = ThreadingHTTPServer((host, port), handler_for(state))
    print(f"VSOA agent listening on http://{host}:{port}; advertise={advertised}", flush=True)
    try:
        server.serve_forever()
    finally:
        state.close()
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default=os.environ.get("VSOA_AGENT_TOKEN"))
    parser.add_argument("--root", default="vsoa-agent-runs")
    parser.add_argument("--advertise-host")
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.token, args.root, args.advertise_host)


if __name__ == "__main__":
    main()
