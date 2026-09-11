"""Run all 15 metrics sequentially and export nested JSON plus flattened CSV."""

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from bench.common import ROOT, positive, positive_int

CASES = ["discovery", "communication_models", "cross_platform", "qos", "reliability",
         "realtime", "transports", "ecosystem", "latency", "throughput", "jitter", "cpu",
         "memory", "startup_discovery", "packet_loss_recovery"]


def flatten(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from flatten(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        yield prefix, json.dumps(value, ensure_ascii=False)
    else:
        yield prefix, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--case-timeout", type=positive, default=120)
    parser.add_argument("--repeat", type=positive_int, default=1)
    options, forwarded = parser.parse_known_args()
    if any(argument == "--output" or argument.startswith("--output=") for argument in forwarded):
        parser.error("Use --output-dir; individual --output paths are managed by the runner")
    output = (options.output_dir or ROOT / "results" / datetime.now().strftime("%Y%m%d_%H%M%S")).resolve()
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    for repeat in range(1, options.repeat + 1):
        for case in CASES:
            result_path = output / f"{case}.{repeat}.json"
            if result_path.exists():
                parser.error(f"Refusing to overwrite an existing run: {result_path}")
            command = [sys.executable, str(ROOT / f"test_{case}.py"), *forwarded,
                       "--case-timeout", str(options.case_timeout), "--output", str(result_path)]
            started = time.perf_counter()
            with open(output / f"{case}.{repeat}.log", "w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log)
                try:
                    exit_code = process.wait(timeout=options.case_timeout + 10)
                except subprocess.TimeoutExpired:
                    family = psutil.Process(process.pid).children(recursive=True)
                    for child in reversed(family):
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                    process.kill()
                    process.wait(timeout=5)
                    exit_code = 1
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (ValueError, OSError) as error:
                    result = {"metric": case, "status": "error", "error": str(error)}
            else:
                result = {"metric": case, "status": "error", "measurements": {},
                          "error": "No result JSON; inspect per-case log"}
            if exit_code != 0 and result.get("status") not in {"fail", "error"}:
                result["status"] = "error"
                result["error"] = "Process exit code contradicts its success report"
            result.update(repeat=repeat, exit_code=exit_code,
                          wall_seconds=time.perf_counter() - started, result_file=str(result_path))
            reports.append(result)
            print(f"[{repeat}/{options.repeat}] {case}: {result['status']}", flush=True)
    summary = {"schema_version": "1.0", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
               "counts": {status: sum(result["status"] == status for result in reports)
                          for status in ("pass", "partial", "fail", "error", "skip")}, "results": reports}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                                   allow_nan=False) + "\n", encoding="utf-8")
    with open(output / "summary.csv", "w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["middleware", "metric", "repeat", "status", "field", "value"])
        for result in reports:
            for field, value in flatten(result.get("measurements", {})):
                writer.writerow(["vsoa", result["metric"], result["repeat"], result["status"], field, value])
    print(json.dumps({"output_directory": str(output), "counts": summary["counts"]}, ensure_ascii=False))
    return 1 if summary["counts"]["fail"] or summary["counts"]["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
