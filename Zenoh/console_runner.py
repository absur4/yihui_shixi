from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, pstdev

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from .adapter import Adapter, _flat_configuration, _single_result  # type: ignore
except ImportError:
    from adapter import Adapter, _flat_configuration, _single_result  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ConsoleLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", encoding="utf-8")

    def write(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        self.file.write(line + "\n")
        self.file.flush()

    def close(self) -> None:
        self.file.close()


def _resolve(root: Path, value: str | None, default: str) -> Path:
    path = Path(value or default)
    return path if path.is_absolute() else root / path


def _case_list(spec: dict) -> list[dict]:
    cases = spec.get("cases")
    if cases:
        return [dict(item) for item in cases]
    plan = spec.get("plan")
    if isinstance(plan, list) and plan:
        return [dict(item.get("configuration", item)) for item in plan]
    config = dict(spec.get("config", {}))
    return [config]


def _aggregate(runs: list[dict]) -> dict:
    completed = [run for run in runs if run.get("status") == "completed"]
    numeric_fields = ("latency_ms", "latency_p95_ms", "latency_p99_ms", "latency_std_ms", "throughput_mbps", "jitter_ms", "packet_loss", "final_packet_loss", "startup_time_ms", "discovery_time_ms", "cpu_percent", "memory_mb")
    summary: dict[str, dict | None] = {}
    for field in numeric_fields:
        values = [float(run[field]) for run in completed if run.get(field) is not None]
        if not values:
            summary[field] = None
            continue
        ordered = sorted(values)
        h95 = (len(ordered) - 1) * .95
        h99 = (len(ordered) - 1) * .99
        percentile = lambda h: ordered[int(h)] if h.is_integer() else ordered[int(h)] + (ordered[int(h) + 1] - ordered[int(h)]) * (h - int(h))
        summary[field] = {"count": len(values), "mean": mean(values), "median": median(values),
                          "variance": pstdev(values) ** 2 if len(values) > 1 else 0.0,
                          "std": pstdev(values) if len(values) > 1 else 0.0,
                          "min": min(values), "max": max(values), "p95": percentile(h95), "p99": percentile(h99)}
    statuses = {}
    for run in runs:
        statuses[run["status"]] = statuses.get(run["status"], 0) + 1
    return {"measurement_level": "run_level", "run_count": len(runs), "valid_run_count": len(completed), "status_counts": statuses, "metrics": summary}


def run(spec_path: str) -> int:
    spec_file = Path(spec_path).resolve()
    root = spec_file.parent
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    output = _resolve(root, spec.get("output"), "output")
    runs_dir = output / "runs"
    artifacts_dir = output / "artifacts"
    logs_dir = _resolve(root, spec.get("logs"), "output/logs")
    output.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    (root / "job.json").write_text(json.dumps({"middleware_id": "zenoh", "job_id": root.name, "status": "running", "started_at": _utc_now()}, ensure_ascii=False, indent=2), encoding="utf-8")
    log = ConsoleLog(root / "console.log")
    adapter = Adapter()
    all_runs: list[dict] = []
    cases = _case_list(spec)
    default_repeats = max(5, int(spec.get("config", {}).get("repeats", 5)))
    try:
        log.write(f"Zenoh adapter started; cases={len(cases)}")
        for case_index, case in enumerate(cases, 1):
            repeats = max(5, int(case.get("repeats", default_repeats)))
            scenario = case.get("scenario_name", "S01")
            condition_id = case.get("condition_id", f"{scenario}-{case_index}")
            log.write(f"case {case_index}/{len(cases)} {scenario} {condition_id}; repeats={repeats}")
            for repeat in range(1, repeats + 1):
                run_id = f"{root.name}-{condition_id}-r{repeat:02d}"
                artifact = artifacts_dir / run_id
                try:
                    result = adapter.run(case, {"run_id": run_id, "repeat": repeat, "artifact_dir": artifact})
                except Exception as exc:
                    result = _single_result(
                        _flat_configuration(case, {}),
                        {"counts": {}, "metrics": {}, "samples": [], "environment": {},
                         "errors": [{"type": "adapter_error", "message": f"{type(exc).__name__}: {exc}"}]},
                        repeat,
                        run_id,
                        status_override="error",
                    )
                    log.write(f"error {run_id}: {type(exc).__name__}: {exc}")
                result_path = runs_dir / f"{run_id}.json"
                result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                all_runs.append(result)
                log.write(f"completed {run_id}: status={result['status']} received={result.get('messages_received')}")
        suite = {
            "schema_version": "1.0",
            "metrics_definition_version": "unified-middleware-1.0",
            "middleware_id": "zenoh",
            "middleware_version": adapter.metadata()["version"],
            "status": "completed" if all(run.get("status") in {"completed", "unsupported", "not_tested"} for run in all_runs) else "error",
            "started_at": spec.get("started_at", _utc_now()),
            "finished_at": _utc_now(),
            "runs": all_runs,
            "summary": _aggregate(all_runs),
        }
        (output / "result.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "job.json").write_text(json.dumps({"middleware_id": "zenoh", "job_id": root.name, "status": suite["status"], "run_count": len(all_runs), "finished_at": suite["finished_at"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        log.write(f"RESULT {output / 'result.json'} status={suite['status']}")
        return 0
    except KeyboardInterrupt:
        log.write(f"RESULT {output / 'result.json'} status=cancelled")
        return 130
    except Exception as exc:
        log.write(f"error: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        (root / "job.json").write_text(json.dumps({"middleware_id": "zenoh", "job_id": root.name, "status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2), encoding="utf-8")
        log.write(f"RESULT {output / 'result.json'} status=error")
        return 1
    finally:
        log.close()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: zenoh-console <job_dir>/spec.json", file=sys.stderr)
        return 2
    return run(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
