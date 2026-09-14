from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import BenchConfig
from .runner import ZenohBench, _utc_now
from .stats import round_numbers

ProgressCallback = Callable[[str, int, int, str], None]


@dataclass(slots=True)
class NodeSpec:
    name: str
    url: str
    advertise_host: str
    publishers: int = 0
    subscribers: int = 0
    base_port: int = 7447

    @classmethod
    def from_dict(cls, raw: dict) -> "NodeSpec":
        node = cls(
            name=str(raw.get("name", "")).strip(),
            url=str(raw.get("url", "")).strip().rstrip("/"),
            advertise_host=str(raw.get("advertise_host", "")).strip(),
            publishers=int(raw.get("publishers", 0)),
            subscribers=int(raw.get("subscribers", 0)),
            base_port=int(raw.get("base_port", 7447)),
        )
        parsed = urlparse(node.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{node.name or 'node'}: agent URL must be http:// or https://")
        if not node.name:
            node.name = parsed.hostname
        if not node.advertise_host:
            node.advertise_host = parsed.hostname
        if node.publishers < 0 or node.subscribers < 0:
            raise ValueError(f"{node.name}: worker counts cannot be negative")
        if not node.publishers and not node.subscribers:
            raise ValueError(f"{node.name}: assign at least one publisher or subscriber")
        if not 1 <= node.base_port <= 65535:
            raise ValueError(f"{node.name}: base_port must be between 1 and 65535")
        if node.base_port + max(0, node.subscribers - 1) > 65535:
            raise ValueError(f"{node.name}: subscriber port range exceeds 65535")
        return node


class AgentClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Zenoh-Bench-Token"] = self.token
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except Exception:
                detail = str(exc)
            raise RuntimeError(f"{self.base_url}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{self.base_url}: {exc}") from exc

    def probe_clock(self, samples: int = 3) -> dict:
        measurements = []
        health = None
        for _ in range(samples):
            before = time.time_ns()
            health = self.request("/api/v1/health")
            after = time.time_ns()
            if not health.get("ok") or health.get("service") != "zenoh-bench-agent":
                raise RuntimeError(f"{self.base_url}: incompatible agent")
            midpoint = (before + after) // 2
            measurements.append(
                {
                    "offset_ns": int(health["time_ns"]) - midpoint,
                    "rtt_ns": after - before,
                }
            )
        best = min(measurements, key=lambda item: item["rtt_ns"])
        return {
            "url": self.base_url,
            "hostname": health.get("hostname"),
            "platform": health.get("platform"),
            "python": health.get("python"),
            "clock_offset_ns": best["offset_ns"],
            "clock_uncertainty_ms": best["rtt_ns"] / 2e6,
            "round_trip_ms": best["rtt_ns"] / 1e6,
        }


def _endpoint_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def validate_topology(cfg: BenchConfig, nodes: list[NodeSpec]) -> None:
    publishers = sum(node.publishers for node in nodes)
    subscribers = sum(node.subscribers for node in nodes)
    if publishers != cfg.publisher_count or subscribers != cfg.subscriber_count:
        raise ValueError(
            "node assignments do not match topology: "
            f"assigned {publishers}P/{subscribers}S, configured "
            f"{cfg.publisher_count}P/{cfg.subscriber_count}S"
        )
    if cfg.transport_mode.upper() not in {"TCP", "UDP"}:
        raise ValueError("visual multi-host orchestration currently supports TCP and UDP endpoints")


def build_assignments(
    cfg: BenchConfig,
    nodes: list[NodeSpec],
    clock_sync: dict[str, dict],
    scheduled_start_epoch_ns: int,
) -> list[dict]:
    validate_topology(cfg, nodes)
    scheme = cfg.transport_mode.lower()
    endpoints = []
    subscriber_index = 0
    for node in nodes:
        for local_index in range(node.subscribers):
            port = node.base_port + local_index
            endpoints.append(
                {
                    "node": node,
                    "subscriber_id": subscriber_index,
                    "listen": f"{scheme}/0.0.0.0:{port}",
                    "connect": f"{scheme}/{_endpoint_host(node.advertise_host)}:{port}",
                }
            )
            subscriber_index += 1

    assignments = []
    for endpoint in endpoints:
        node = endpoint["node"]
        worker_cfg = BenchConfig.from_dict(cfg.to_dict())
        worker_cfg.listen = [endpoint["listen"]]
        worker_cfg.connect = []
        worker_cfg.clock_offset_ns = clock_sync[node.url]["clock_offset_ns"]
        worker_cfg.scheduled_start_epoch_ns = scheduled_start_epoch_ns
        assignments.append(
            {
                "node": node,
                "role": "subscriber",
                "index": endpoint["subscriber_id"],
                "config": worker_cfg,
            }
        )

    publisher_index = 0
    connect_endpoints = [endpoint["connect"] for endpoint in endpoints]
    for node in nodes:
        for _ in range(node.publishers):
            worker_cfg = BenchConfig.from_dict(cfg.to_dict())
            worker_cfg.listen = []
            worker_cfg.connect = connect_endpoints
            worker_cfg.clock_offset_ns = clock_sync[node.url]["clock_offset_ns"]
            worker_cfg.scheduled_start_epoch_ns = scheduled_start_epoch_ns
            assignments.append(
                {
                    "node": node,
                    "role": "publisher",
                    "index": publisher_index,
                    "config": worker_cfg,
                }
            )
            publisher_index += 1
    return assignments


class DistributedOrchestrator:
    def __init__(self, out_dir: str | Path = "results", token: str = ""):
        self.out_dir = Path(out_dir)
        self.token = token

    def probe(self, raw_nodes: list[dict]) -> list[dict]:
        nodes = [NodeSpec.from_dict(raw) for raw in raw_nodes]
        return [
            {"name": node.name, **AgentClient(node.url, self.token).probe_clock()}
            for node in nodes
        ]

    def run(
        self,
        config: BenchConfig | dict,
        raw_nodes: list[dict],
        progress_callback: ProgressCallback | None = None,
    ) -> dict:
        cfg = config if isinstance(config, BenchConfig) else BenchConfig.from_dict(config)
        nodes = [NodeSpec.from_dict(raw) for raw in raw_nodes]
        validate_topology(cfg, nodes)
        cfg.run_id = cfg.run_id or uuid.uuid4().hex[:12]
        run_dir = self.out_dir / cfg.run_id
        if run_dir.exists():
            raise FileExistsError(f"run_id already exists: {cfg.run_id}")
        run_dir.mkdir(parents=True)
        (run_dir / "config.json").write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "topology.json").write_text(
            json.dumps([asdict(node) for node in nodes], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        started = time.perf_counter()
        start_utc = _utc_now()
        repeat_results = []
        try:
            for repeat_index in range(1, cfg.repeats + 1):
                if progress_callback:
                    progress_callback("probing", repeat_index, cfg.repeats, "probing agents and clocks")
                repeat_cfg = BenchConfig.from_dict(cfg.to_dict())
                repeat_cfg.run_id = f"{cfg.run_id}-r{repeat_index:02d}"
                repeat_dir = run_dir / f"repeat-{repeat_index:02d}"
                repeat_results.append(
                    self._run_once(repeat_cfg, nodes, repeat_dir, repeat_index, progress_callback)
                )
            result = ZenohBench(self.out_dir)._aggregate_repeats(
                cfg,
                repeat_results,
                start_utc,
                _utc_now(),
                (time.perf_counter() - started) * 1000,
            )
            result["execution_mode"] = "multi_host"
            result["topology"] = [asdict(node) for node in nodes]
            result["environment"]["distributed"] = True
            (run_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return result
        except Exception as exc:
            (run_dir / "failure.json").write_text(
                json.dumps(
                    {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "test_start_time": start_utc,
                        "test_end_time": _utc_now(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise

    def _run_once(
        self,
        cfg: BenchConfig,
        nodes: list[NodeSpec],
        repeat_dir: Path,
        repeat_index: int,
        progress_callback: ProgressCallback | None,
    ) -> dict:
        repeat_dir.mkdir(parents=True)
        start_utc = _utc_now()
        clients = {node.url: AgentClient(node.url, self.token) for node in nodes}
        clock_sync = {url: client.probe_clock() for url, client in clients.items()}
        lead_seconds = max(5.0, min(cfg.discovery_timeout_seconds, 15.0))
        scheduled_start = time.time_ns() + int(lead_seconds * 1e9)
        assignments = build_assignments(cfg, nodes, clock_sync, scheduled_start)
        submitted = []
        try:
            for assignment in sorted(assignments, key=lambda item: item["role"] != "subscriber"):
                role = assignment["role"]
                index = assignment["index"]
                node = assignment["node"]
                job_id = f"{cfg.run_id}-{role[0]}{index:03d}"
                payload = {
                    "job_id": job_id,
                    "role": role,
                    "index": index,
                    "config": assignment["config"].to_dict(),
                }
                clients[node.url].request("/api/v1/jobs", "POST", payload)
                submitted.append({**assignment, "job_id": job_id})

            if progress_callback:
                progress_callback("waiting_ready", repeat_index, cfg.repeats, "waiting for workers")
            self._wait_ready(clients, submitted, scheduled_start)
            if progress_callback:
                progress_callback("running", repeat_index, cfg.repeats, "workers are running")
            recovery_wait = cfg.recovery_timeout_seconds if cfg.scenario_name == "S11" else 0.0
            finish_deadline = (
                scheduled_start / 1e9
                + cfg.duration_seconds
                + cfg.drain_seconds
                + recovery_wait
                + 20
            )
            self._wait_complete(clients, submitted, finish_deadline)
            fragments = []
            for item in submitted:
                fragment = clients[item["node"].url].request(
                    f"/api/v1/jobs/{item['job_id']}/result"
                )
                fragment["agent"] = {
                    "name": item["node"].name,
                    "url": item["node"].url,
                    **clock_sync[item["node"].url],
                }
                fragments.append(fragment)
                fragment_name = f"{item['role']}-{item['index']}.json"
                (repeat_dir / fragment_name).write_text(
                    json.dumps(fragment, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                log = clients[item["node"].url].request(
                    f"/api/v1/jobs/{item['job_id']}/log"
                ).get("log", "")
                log_name = f"{item['role']}-{item['index']}.log"
                (repeat_dir / log_name).write_text(log, encoding="utf-8")
        except Exception:
            for item in submitted:
                try:
                    clients[item["node"].url].request(
                        f"/api/v1/jobs/{item['job_id']}/cancel", "POST", {}
                    )
                except Exception:
                    pass
            raise

        result = ZenohBench(self.out_dir).aggregate(cfg, fragments)
        result["execution_mode"] = "multi_host"
        result["clock_sync"] = list(clock_sync.values())
        result["environment"]["distributed"] = True
        result["environment"]["max_clock_uncertainty_ms"] = max(
            sync["clock_uncertainty_ms"] for sync in clock_sync.values()
        )
        if result["environment"]["max_clock_uncertainty_ms"] > 2:
            result["errors"].append(
                {
                    "type": "clock_sync_warning",
                    "message": "one-way latency uncertainty exceeds 2 ms; verify NTP/PTP",
                }
            )
        result["test_start_time"] = start_utc
        result["test_end_time"] = _utc_now()
        result = round_numbers(result)
        (repeat_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    @staticmethod
    def _wait_ready(clients: dict[str, AgentClient], jobs: list[dict], start_ns: int) -> None:
        deadline = start_ns / 1e9 + 1
        while time.time() < deadline:
            pending = 0
            for item in jobs:
                state = clients[item["node"].url].request(f"/api/v1/jobs/{item['job_id']}")
                if state["status"] in {"failed", "cancelled"}:
                    detail = "; ".join(state.get("log_tail", [])[-3:])
                    raise RuntimeError(f"{item['job_id']} {state['status']}: {detail}")
                if not state.get("ready") and state["status"] != "complete":
                    pending += 1
            if not pending:
                return
            time.sleep(0.1)
        raise TimeoutError("workers did not become ready before scheduled start")

    @staticmethod
    def _wait_complete(
        clients: dict[str, AgentClient], jobs: list[dict], deadline_epoch_seconds: float
    ) -> None:
        while time.time() < deadline_epoch_seconds:
            pending = 0
            for item in jobs:
                state = clients[item["node"].url].request(f"/api/v1/jobs/{item['job_id']}")
                if state["status"] in {"failed", "cancelled"}:
                    detail = "; ".join(state.get("log_tail", [])[-3:])
                    raise RuntimeError(f"{item['job_id']} {state['status']}: {detail}")
                if state["status"] != "complete":
                    pending += 1
            if not pending:
                return
            time.sleep(0.25)
        raise TimeoutError("distributed workers exceeded the run deadline")
