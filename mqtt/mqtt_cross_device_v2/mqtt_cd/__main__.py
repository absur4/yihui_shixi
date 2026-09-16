from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
import secrets
import sys

from .common import read_json, validate_case, write_json


def directions(value):
    if value == "both":
        return ["A_to_B", "B_to_A"]
    if value not in ("A_to_B", "B_to_A"):
        raise argparse.ArgumentTypeError("direction must be A_to_B, B_to_A or both")
    return [value]


def parser():
    root = argparse.ArgumentParser(description="Two-host MQTT CD1-CD4 benchmark (independent v2)")
    root.add_argument("--version",action="version",version="mqtt-cross-device 2.0.0")
    subs = root.add_subparsers(dest="command",required=True)
    subs.add_parser("token",help="Generate a shared random token; set it in MQTT_CD_TOKEN on each computer")
    agent = subs.add_parser("agent",help="Start one physical node agent")
    agent.add_argument("--node-id",choices=["A","B"],required=True)
    agent.add_argument("--host",default="0.0.0.0")
    agent.add_argument("--port",type=int,default=8765)
    agent.add_argument("--data-dir",type=Path,default=Path("agent-data"))
    agent.add_argument("--broker-executable",type=Path,required=True)
    agent.add_argument("--broker-password-executable",type=Path)
    agent.add_argument("--broker-bind",default="0.0.0.0")
    check = subs.add_parser("check",help="Read-only connectivity, dependency and node checks")
    check.add_argument("--config",type=Path,default=Path("config.json"))
    check.add_argument("--allow-local-check",action="store_true")
    for name in ("run","plan"):
        p = subs.add_parser(name,help="Run a suite" if name=="run" else "Generate a scenario plan without running anything")
        p.add_argument("--scenarios",default="SMOKE",help="SMOKE, CD1,CD2,CD3, CD4, or all")
        p.add_argument("--direction",type=directions,default=["A_to_B"])
        p.add_argument("--repeats",type=int,default=5)
        p.add_argument("--seed",type=int,default=20260916)
        p.add_argument("--output",type=Path)
        p.add_argument("--duration",type=float,help="Exploratory override; not a formal matrix")
        p.add_argument("--rate",type=int,help="Exploratory override of every selected rate")
        p.add_argument("--payload",type=int,help="Exploratory override in payload bytes")
        p.add_argument("--subscribers",type=int,help="Exploratory override 1..4")
        p.add_argument("--warmup",type=float)
        p.add_argument("--drain",type=float)
        if name=="run":
            p.add_argument("--config",type=Path,default=Path("config.json"))
            p.add_argument("--allow-local-check",action="store_true")
            p.add_argument("--refine",action="store_true",help="Confirm CD1 boundaries and refine up to three midpoint rates")
            p.add_argument("--prior-results",type=Path,help="Earlier MQTT suite used for boundary/CD4 planning")
            p.add_argument("--capacity-reference",type=Path,help="Validated external capacity references for CD4-C")
            p.add_argument("--manual-cd4",help="Exploratory low,high rates, e.g. 500,2000; no saturation claim")
            p.add_argument("--common-rate",type=int,help="Exploratory CD4-C rate, not a validated cross-middleware baseline")
    analyze = subs.add_parser("analyze",help="Recompute a stored run, preserving its raw data")
    analyze.add_argument("run_dir",type=Path)
    analyze.add_argument("--output",type=Path,help="Default: reanalysis subdirectory")
    return root


def make_cases(args):
    from .planner import build_scan
    scenes = [s.strip().upper() for s in args.scenarios.split(",")]
    if scenes == ["ALL"]:
        scenes = ["CD1","CD2","CD3","CD4"]
    if any(s not in {"SMOKE","CD1","CD2","CD3","CD4"} for s in scenes):
        raise ValueError("Unknown scenario; use SMOKE, CD1, CD2, CD3, CD4 or all")
    if not 1 <= args.repeats <= 5:
        raise ValueError("repeats outside 1..5; formal conditions require five slots")
    scan = [s for s in scenes if s in {"CD1","CD2","CD3"}]
    cases = build_scan(scan,args.direction,args.repeats,args.seed) if scan else []
    if "SMOKE" in scenes:
        for direction in args.direction:
            cases.append(validate_case(dict(scenario="SMOKE",level="connectivity",direction=direction,
                         rate_hz=100,duration_s=2,warmup_s=.3,settle_s=.3,drain_s=1,
                         repeat=1,measurement_kind="smoke")))
    overrides = {"duration_s":args.duration,"rate_hz":args.rate,"payload_bytes":args.payload,
                 "subscribers":args.subscribers,"warmup_s":args.warmup,"drain_s":args.drain}
    overrides = {k:v for k,v in overrides.items() if v is not None}
    if overrides:
        cases = [validate_case(dict(case,**overrides,measurement_kind="exploratory")) for case in cases]
    return cases, "CD4" in scenes


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "token":
            print(secrets.token_urlsafe(32))
            return 0
        if args.command == "agent":
            from .agent import serve_agent
            serve_agent(args)
            return 0
        if args.command == "check":
            from .controller import validate_config, health
            import json
            _, reports = health(validate_config(read_json(args.config)),args.allow_local_check)
            print(json.dumps(reports,ensure_ascii=False,indent=2))
            return 0
        if args.command == "analyze":
            from .analysis import analyze_run
            from .report import write_report
            result = analyze_run(args.run_dir,read_json(args.run_dir/"spec.json"),read_json(args.run_dir/"clocks.json"))
            output = args.output or args.run_dir/"reanalysis"
            output.mkdir(parents=True,exist_ok=True)
            write_json(output/"result.json",result)
            write_report(output/"report.html",result)
            print(output.resolve()/"report.html")
            return 0
        cases, cd4 = make_cases(args)
        if args.command == "plan":
            output = args.output or Path("plan.json")
            write_json(output,dict(cases=cases,cd4_requires_capacities=cd4,
                estimated_measurement_seconds=sum(c["duration_s"] for c in cases)))
            print(f"PLAN {output.resolve()} cases={len(cases)}")
            return 0
        from .controller import collect_prior_results, validate_config, run_suite
        config = validate_config(read_json(args.config))
        prior = collect_prior_results(read_json(args.prior_results)) if args.prior_results else []
        refs = read_json(args.capacity_reference) if args.capacity_reference else None
        manual = None
        if args.manual_cd4 or args.common_rate:
            if not cd4:
                raise ValueError("Manual CD4 rates require --scenarios CD4 or all")
            rates = {}
            if args.manual_cd4:
                values = [int(v) for v in args.manual_cd4.split(",")]
                if len(values)!=2 or any(v<1 or v>1000000 for v in values):
                    raise ValueError("--manual-cd4 needs two positive rates <=1000000")
                rates.update({"CD4-L":values[0],"CD4-H":values[1]})
            if args.common_rate:
                if not 1 <= args.common_rate <= 1000000:
                    raise ValueError("invalid common rate")
                rates["CD4-C"] = args.common_rate
            manual = {direction:dict(rates) for direction in args.direction}
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+secrets.token_hex(3)
        output = args.output or Path("results")/stamp
        suite = run_suite(config,cases,output,allow_local=args.allow_local_check,
                          cd4=cd4,refine=args.refine,references=refs,manual_rates=manual,prior_results=prior,
                          planning_seed=args.seed,
                          selected_directions=args.direction,generated_repeats=args.repeats,
                          generated_overrides={k:v for k,v in {
                              "duration_s":args.duration,"rate_hz":args.rate,"payload_bytes":args.payload,
                              "subscribers":args.subscribers,"warmup_s":args.warmup,"drain_s":args.drain
                          }.items() if v is not None})
        return 130 if suite["status"]=="cancelled" else 1 if suite["status"]=="error" else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        import os
        message = str(exc)
        secret = os.environ.get("MQTT_CD_TOKEN","")
        if secret:
            message = message.replace(secret,"[REDACTED]")
        print(f"ERROR: {message}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
