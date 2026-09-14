from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import BenchConfig
from .distributed import DistributedOrchestrator
from .runner import ZenohBench
from .scenarios import SCENARIOS
from .schema import validate_result
from .worker import run_publisher, run_subscriber


def _load(value: str) -> dict:
    path = Path(value)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else json.loads(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="zenoh-bench", description="Zenoh loopback and multi-host unified benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("loopback", help="run publishers/subscribers as separate local processes")
    run.add_argument("--config", required=True, help="JSON file or inline JSON")
    run.add_argument("--out-dir", default="results")
    worker = sub.add_parser("worker", help="run one publisher or subscriber on this host")
    worker.add_argument("role", choices=["publisher", "subscriber"])
    worker_config = worker.add_mutually_exclusive_group(required=True)
    worker_config.add_argument("--config-json")
    worker_config.add_argument("--config", help="path to a JSON config file")
    worker.add_argument("--index", type=int, default=0)
    worker.add_argument("--output", required=True)
    agg = sub.add_parser("aggregate", help="combine multi-host result fragments")
    agg.add_argument("--config", required=True)
    agg.add_argument("--fragments", nargs="+", required=True)
    agg.add_argument("--output", required=True)
    validate = sub.add_parser("validate", help="validate a result JSON against the unified schema")
    validate.add_argument("result")
    scenarios = sub.add_parser("scenarios", help="list standard S01-S12 scenarios")
    agent = sub.add_parser("agent", help="serve this host as a distributed benchmark node")
    agent.add_argument("--host", default="0.0.0.0")
    agent.add_argument("--port", type=int, default=8770)
    agent.add_argument("--work-dir", default="agent-results")
    agent.add_argument("--token", default="", help="optional shared controller token")
    probe = sub.add_parser("probe", help="probe distributed agents and clock offsets")
    probe.add_argument("--nodes", required=True, help="node topology JSON file")
    probe.add_argument("--token", default="")
    distributed = sub.add_parser("distributed", help="run and aggregate a multi-host benchmark")
    distributed.add_argument("--config", required=True)
    distributed.add_argument("--nodes", required=True, help="node topology JSON file")
    distributed.add_argument("--token", default="")
    distributed.add_argument("--out-dir", default="results")
    web = sub.add_parser("web", help="start the visual test console")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--out-dir", default="results")
    args = parser.parse_args(argv)
    if args.command == "worker":
        cfg = BenchConfig.from_dict(_load(args.config or args.config_json))
        (run_publisher if args.role == "publisher" else run_subscriber)(cfg, args.index, args.output)
        return 0
    if args.command == "loopback":
        result = ZenohBench(args.out_dir).run_loopback(BenchConfig.from_dict(_load(args.config)))
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    if args.command == "aggregate":
        cfg = BenchConfig.from_dict(_load(args.config))
        fragments = [_load(x) for x in args.fragments]
        result = ZenohBench().aggregate(cfg, fragments)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(args.output); return 0
    if args.command == "validate":
        errors = validate_result(_load(args.result))
        if errors:
            print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2)); return 1
        print(json.dumps({"valid": True}, ensure_ascii=False)); return 0
    if args.command == "scenarios":
        print(json.dumps(SCENARIOS, ensure_ascii=False, indent=2)); return 0
    if args.command == "agent":
        from .agent import serve_agent
        serve_agent(args.host, args.port, args.work_dir, args.token); return 0
    if args.command == "probe":
        result = DistributedOrchestrator(token=args.token).probe(_load(args.nodes))
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    if args.command == "distributed":
        result = DistributedOrchestrator(args.out_dir, args.token).run(
            BenchConfig.from_dict(_load(args.config)),
            _load(args.nodes),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    from .web import serve
    serve(args.host, args.port, args.out_dir); return 0


if __name__ == "__main__":
    raise SystemExit(main())
