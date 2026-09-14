from __future__ import annotations

import json
import threading
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import BenchConfig
from .distributed import DistributedOrchestrator
from .runner import ZenohBench
from .scenarios import SCENARIOS

ROOT = Path(__file__).parent / "static"
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_JOB_ID: str | None = None


class Handler(SimpleHTTPRequestHandler):
    out_dir = Path("results")

    def log_message(self, format, *args):
        if args and isinstance(args[0], str) and "GET /api/jobs/" in args[0] and " 200 " in args[0]:
            return
        super().log_message(format, *args)

    def translate_path(self, path):
        relative = urlparse(path).path.lstrip("/") or "index.html"
        candidate = (ROOT / relative).resolve()
        return str(candidate if candidate.is_relative_to(ROOT.resolve()) else ROOT / "index.html")

    def _json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            with JOBS_LOCK:
                health = {
                    "ok": True,
                    "service": "zenoh-bench",
                    "jobs": len(JOBS),
                    "active_job_id": ACTIVE_JOB_ID,
                }
            return self._json(health)
        if path == "/api/scenarios":
            return self._json(SCENARIOS)
        if path == "/api/results":
            results = []
            if self.out_dir.exists():
                result_paths = sorted(
                    self.out_dir.glob("*/result.json"),
                    key=lambda item: item.stat().st_mtime,
                )
                for result_path in result_paths:
                    try:
                        results.append(json.loads(result_path.read_text(encoding="utf-8")))
                    except (OSError, json.JSONDecodeError):
                        continue
            return self._json(results[-20:])
        if path.startswith("/api/jobs/"):
            with JOBS_LOCK:
                job = JOBS.get(path.rsplit("/", 1)[-1], {"status": "missing"})
            return self._json(job)
        return super().do_GET()

    def do_POST(self):
        global ACTIVE_JOB_ID
        path = urlparse(self.path).path
        try:
            size = int(self.headers.get("Content-Length", "0"))
            raw = json.loads(self.rfile.read(size))
            if path == "/api/agents/probe":
                result = DistributedOrchestrator(token=str(raw.get("token", ""))).probe(
                    raw.get("nodes", [])
                )
                return self._json(result)
            if path not in {"/api/run", "/api/distributed/run"}:
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            cfg = BenchConfig.from_dict(raw)
            is_distributed = path == "/api/distributed/run"
            nodes = raw.get("nodes", [])
            token = str(raw.get("token", ""))
            if is_distributed:
                DistributedOrchestrator(self.out_dir, token).probe(nodes)
            else:
                ZenohBench.validate_loopback_config(cfg)
            job_id = cfg.run_id or uuid.uuid4().hex[:12]; cfg.run_id = job_id
            with JOBS_LOCK:
                if ACTIVE_JOB_ID is not None:
                    return self._json(
                        {
                            "error": "another loopback test is already running",
                            "active_job_id": ACTIVE_JOB_ID,
                        },
                        HTTPStatus.CONFLICT,
                    )
                if job_id in JOBS or (self.out_dir / job_id).exists():
                    return self._json({"error": f"run_id already exists: {job_id}"}, HTTPStatus.CONFLICT)
                ACTIVE_JOB_ID = job_id
                JOBS[job_id] = {
                    "status": "running",
                    "run_id": job_id,
                    "scenario_name": cfg.scenario_name,
                    "execution_mode": "multi_host" if is_distributed else "loopback",
                    "stage": "queued",
                    "repeat_current": 0,
                    "repeat_total": cfg.repeats,
                }

            def execute():
                global ACTIVE_JOB_ID
                def loopback_progress(current, total):
                    with JOBS_LOCK:
                        JOBS[job_id].update(
                            {
                                "stage": "running",
                                "message": "running local workers",
                                "repeat_current": current,
                                "repeat_total": total,
                            }
                        )

                def distributed_progress(stage, current, total, message):
                    with JOBS_LOCK:
                        JOBS[job_id].update(
                            {
                                "stage": stage,
                                "message": message,
                                "repeat_current": current,
                                "repeat_total": total,
                            }
                        )

                try:
                    if is_distributed:
                        result = DistributedOrchestrator(self.out_dir, token).run(
                            cfg, nodes, distributed_progress
                        )
                    else:
                        result = ZenohBench(self.out_dir).run_loopback(cfg, loopback_progress)
                    with JOBS_LOCK:
                        JOBS[job_id] = {
                            "status": "complete",
                            "run_id": job_id,
                            "execution_mode": "multi_host" if is_distributed else "loopback",
                            "stage": "complete",
                            "repeat_current": cfg.repeats,
                            "repeat_total": cfg.repeats,
                            "result": result,
                        }
                except Exception as exc:
                    with JOBS_LOCK:
                        JOBS[job_id] = {
                            **JOBS[job_id],
                            "status": "failed",
                            "stage": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                finally:
                    with JOBS_LOCK:
                        if ACTIVE_JOB_ID == job_id:
                            ACTIVE_JOB_ID = None

            threading.Thread(target=execute, daemon=True).start()
            with JOBS_LOCK:
                job = dict(JOBS[job_id])
            return self._json(job, HTTPStatus.ACCEPTED)
        except Exception as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def serve(host="127.0.0.1", port=8765, out_dir="results"):
    ROOT.mkdir(exist_ok=True)
    Handler.out_dir = Path(out_dir)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Zenoh Bench Console: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

