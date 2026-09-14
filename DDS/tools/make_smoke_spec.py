"""生成一份最小冒烟 spec.json，供人工复现 `console_runner.py` 的协议。

用法::

    python DDS/tools/make_smoke_spec.py --job DDS/results/smoke --index 0 --repeats 1
    python DDS/console_runner.py DDS/results/smoke/spec.json

只使用标准库（契约层零原生依赖），因此主解释器也能运行。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_adapter():
    spec = importlib.util.spec_from_file_location("dds_adapter", PROJECT_ROOT / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a minimal DDS console spec.json")
    parser.add_argument(
        "--job",
        type=Path,
        default=PROJECT_ROOT / "results" / "smoke",
        help="job directory (spec.json and output/ are written here)",
    )
    parser.add_argument("--index", type=int, default=0, help="catalog index of the condition")
    parser.add_argument("--repeats", type=int, default=1, help="case_repeats for the smoke run")
    args = parser.parse_args(argv)

    adapter = load_adapter()
    cases = adapter.build_cases(args.index, {"repeats": args.repeats}, False)
    job = Path(args.job).resolve()
    output = job / "output"
    output.mkdir(parents=True, exist_ok=True)
    case = cases[0]
    spec = {
        "middleware": adapter.MIDDLEWARE_ID,
        "job_id": job.name,
        "config": adapter.base_config(),
        "cases": cases,
        "plan": [
            {
                "scenario_id": case.get("scenario_id"),
                "scenario_name": case.get("scenario_id"),
                "scenario_title": case.get("title"),
                "planned_repeats": case.get("case_repeats"),
            }
        ],
        "output": str(output),
        "logs": str(output / "logs"),
    }
    target = job / "spec.json"
    target.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    print(f"spec.json written: {target}")
    print(f"condition: {case['condition_id']} ({case['title']}) case_repeats={case['case_repeats']}")
    print(f"next: python DDS/console_runner.py {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
