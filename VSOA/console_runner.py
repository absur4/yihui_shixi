"""VSOA 控制台执行器：读取控制台生成的 spec.json 并调用真实测量引擎。

用法: python VSOA/console_runner.py <job_dir>/spec.json

spec.json 结构（控制台生成）：
  {middleware, config, cases, plan, output, logs}
- config 缺省时，本脚本自行加载 VSOA/vsoa_py/delivery_template/config.yaml，保证可独立运行。
- 产物：output/result.json、output/runs/<run_id>.json、output/artifacts/<run_id>/…

stdout 每行 "[HH:MM:SS] 消息"；结束前输出 RESULT 行；退出码 0=completed / 130=cancelled / 其他=error。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
VSOA_PY = FOLDER / "vsoa_py"
if str(VSOA_PY) not in sys.path:
    sys.path.insert(0, str(VSOA_PY))

BASE_CONFIG = VSOA_PY / "delivery_template" / "config.yaml"


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _load_fallback_config():
    from standalone.configuration import load_config
    config, _expanded = load_config(BASE_CONFIG)
    return config


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        print("用法: console_runner.py <spec.json>", file=sys.stderr)
        return 2
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    from standalone.engine import run_suite

    cases = spec.get("cases") or []
    config = spec.get("config") or _load_fallback_config()
    output = Path(spec["output"]) if spec.get("output") else Path(sys.argv[1]).resolve().parent / "output"
    logs = Path(spec["logs"]) if spec.get("logs") else output / "logs"
    total = sum(case.get("case_repeats", 1) for case in cases)
    log(f"SUITE 开始：{len(cases)} 个条件，共 {total} 轮")
    report = run_suite(config, cases, output, logs, log, "all", spec.get("plan") or [], False)
    log(f"RESULT {output / 'result.json'} status={report['status']}")
    return 0 if report["status"] == "completed" else 130 if report["status"] == "cancelled" else 1


if __name__ == "__main__":
    raise SystemExit(main())
