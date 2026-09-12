"""VSOA 作业执行器：读取控制台生成的 spec.json 并调用真实测量引擎。

该脚本作为独立子进程运行，便于控制台随时终止整个进程树；
引擎、指标与产物格式与 VSOA/vsoa_py 官方 standalone 完全一致。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

VSOA_PY = Path(__file__).resolve().parents[1] / "VSOA" / "vsoa_py"
if str(VSOA_PY) not in sys.path:
    sys.path.insert(0, str(VSOA_PY))


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        print("用法: vsoa_runner.py <spec.json>", file=sys.stderr)
        return 2
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    from standalone.engine import run_suite

    output, logs = Path(spec["output"]), Path(spec["logs"])
    total = sum(case["case_repeats"] for case in spec["cases"])
    log(f"SUITE 开始：{len(spec['cases'])} 个条件，共 {total} 轮")
    report = run_suite(spec["config"], spec["cases"], output, logs, log, "all", spec.get("plan") or [], False)
    log(f"RESULT {output / 'result.json'} status={report['status']}")
    return 0 if report["status"] == "completed" else 130 if report["status"] == "cancelled" else 1


if __name__ == "__main__":
    raise SystemExit(main())
