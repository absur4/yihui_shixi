from __future__ import annotations

import hmac
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import BenchConfig

JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


class AgentState:
    def __init__(self, work_dir: str | Path, token: str = ""):
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def public_job(self, job: dict) -> dict:
        return {
            key: value
            for key, value in job.items()
            if key not in {"process", "result_path", "log_path"}
        }

    def create_job(self, payload: dict) -> dict:
        job_id = str(payload.get("job_id", ""))
        role = payload.get("role")
        index = payload.get("index")
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("job_id must contain only letters, digits, dot, dash or underscore")
        if role not in {"publisher", "subscriber"}:
            raise ValueError("role must be publisher or subscriber")
        if not isinstance(index, int) or index < 0:
            raise ValueError("index must be a non-negative integer")
        cfg = BenchConfig.from_dict(payload.get("config", {}))
        job_dir = self.work_dir / job_id
        config_path = job_dir / "config.json"
        result_path = job_dir / "fragment.json"
        log_path = job_dir / "worker.log"
        with self.lock:
            if job_id in self.jobs or job_dir.exists():
                raise FileExistsError(f"job already exists: {job_id}")
            job_dir.mkdir(parents=True)
            config_path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            job = {
                "job_id": job_id,
                "role": role,
                "index": index,
                "status": "starting",
                "ready": False,
                "host": socket.gethostname(),
                "created_at_epoch_ns": time.time_ns(),
                "updated_at_epoch_ns": time.time_ns(),
                "log_tail": [],
                "result_path": result_path,
                "log_path": log_path,
                "process": None,
            }
            self.jobs[job_id] = job

        command = [
            sys.executable,
            "-m",
            f"{__package__}.cli",
            "worker",
            role,
            "--config",
            str(config_path),
            "--index",
            str(index),
            "--output",
            str(result_path),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        with self.lock:
            job["process"] = process
            job["pid"] = process.pid
            job["status"] = "running"
            job["updated_at_epoch_ns"] = time.time_ns()
        threading.Thread(
            target=self._watch_job,
            args=(job_id, process, log_path, result_path),
            daemon=True,
        ).start()
        return self.public_job(job)

    def _watch_job(
        self,
        job_id: str,
        process: subprocess.Popen,
        log_path: Path,
        result_path: Path,
    ) -> None:
        lines: list[str] = []
        with log_path.open("w", encoding="utf-8") as log_file:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip()
                lines.append(line)
                log_file.write(raw_line)
                log_file.flush()
                is_ready = False
                try:
                    is_ready = json.loads(line).get("event") == "ready"
                except (json.JSONDecodeError, AttributeError):
                    pass
                with self.lock:
                    job = self.jobs[job_id]
                    job["log_tail"] = lines[-30:]
                    if is_ready:
                        job["ready"] = True
                        job["status"] = "ready"
                    job["updated_at_epoch_ns"] = time.time_ns()
        return_code = process.wait()
        with self.lock:
            job = self.jobs[job_id]
            job["return_code"] = return_code
            job["updated_at_epoch_ns"] = time.time_ns()
            if return_code == 0 and result_path.exists():
                job["status"] = "complete"
            else:
                job["status"] = "failed"
                job["error"] = f"worker exited with code {return_code}"

    def get_job(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self.public_job(job) if job else None

    def get_result(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job["status"] != "complete":
                raise RuntimeError(f"job is {job['status']}")
            result_path = job["result_path"]
        return json.loads(result_path.read_text(encoding="utf-8"))

    def get_log(self, job_id: str) -> str:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            log_path = job["log_path"]
        return log_path.read_text(encoding="utf-8") if log_path.exists() else ""

    def cancel_job(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            process = job.get("process")
            if process is not None and process.poll() is None:
                process.terminate()
                job["status"] = "cancelled"
                job["updated_at_epoch_ns"] = time.time_ns()
            return self.public_job(job)


class AgentHandler(BaseHTTPRequestHandler):
    state: AgentState

    def log_message(self, format, *args):
        if args and isinstance(args[0], str) and "GET /api/v1/jobs/" in args[0]:
            return
        super().log_message(format, *args)

    def _json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = self.state.token
        supplied = self.headers.get("X-Zenoh-Bench-Token", "")
        return not expected or hmac.compare_digest(expected, supplied)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self._require_auth():
            return
        if path == "/api/v1/health":
            with self.state.lock:
                active = sum(
                    job["status"] in {"starting", "running", "ready"}
                    for job in self.state.jobs.values()
                )
            return self._json(
                {
                    "ok": True,
                    "service": "zenoh-bench-agent",
                    "hostname": socket.gethostname(),
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "time_ns": time.time_ns(),
                    "active_jobs": active,
                }
            )
        match = re.fullmatch(r"/api/v1/jobs/([^/]+)(/result|/log)?", path)
        if match:
            job_id, result_suffix = match.groups()
            try:
                if result_suffix:
                    if result_suffix == "/log":
                        return self._json({"log": self.state.get_log(job_id)})
                    return self._json(self.state.get_result(job_id))
                job = self.state.get_job(job_id)
                if job is None:
                    return self._json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                return self._json(job)
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except (KeyError, OSError, json.JSONDecodeError) as exc:
                return self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self._require_auth():
            return
        try:
            if path == "/api/v1/jobs":
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size))
                return self._json(self.state.create_job(payload), HTTPStatus.ACCEPTED)
            match = re.fullmatch(r"/api/v1/jobs/([^/]+)/cancel", path)
            if match:
                return self._json(self.state.cancel_job(match.group(1)))
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except FileExistsError as exc:
            return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except KeyError as exc:
            return self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)


def serve_agent(host="0.0.0.0", port=8770, work_dir="agent-results", token=""):
    AgentHandler.state = AgentState(work_dir, token)
    server = ThreadingHTTPServer((host, port), AgentHandler)
    print(
        f"Zenoh Bench Agent: http://{host}:{port} "
        f"({socket.gethostname()}, auth={'on' if token else 'off'})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
