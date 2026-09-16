"""三件中间件（VSOA / MQTT / Zenoh）在 S01–S08 下的参数矩阵测试。

设计：
- 每个场景取 4 组参数，组选择的是"该场景的影响因子"（速率 / 尺寸 / 拓扑规模 / 持续负载）。
- 三件中间件使用**完全相同的四组参数**，便于横向对比。
- S08 每组 100 秒（规范要求 ≥5 分钟，正式报告需注明本次实际为 100 s）；其余每组 ≤ 1 分钟。
- **不使用 ``publish_rate_hz=0``（不限速）**：VSOA 引擎要求速率 ≥1 Hz（`period_ns = 1e9/rate`），
  改为官方档位 —— S03 统一 20 Hz（官方 256 KiB 档）、S04 最高档 10000 Hz、S08 大消息 5 Hz。
- 收尾窗口固定 ``drain_seconds = 1.0``（原 0.3 s 不足以让高负载下的尾部在途消息送达，
  会被误记成"丢包"；实测 Zenoh S07 组1 在 drain=1.0 时缺失归零）。三家与 DDS 侧同参数。
- 调用各模块适配器文档化的"单轮直调"接口 ``adapter.run(scenario, parameters)``：
  跑 1 轮真实测量，返回统一指标记录（不走控制台 spec，避免 ≥5 轮下限与作业目录校验）。

用法：
    python run_matrix.py --list
    python run_matrix.py --middleware vsoa --scenario S08 --groups 1
    python run_matrix.py --middleware vsoa --scenario S01 --groups 1,2 --scale 0.3
    python run_matrix.py --middleware all --scenario S01-S08 --groups 1-4

产物：
    songfei/test/<middleware>/<scenario>/group<N>.json   单组完整结果
    songfei/test/<middleware>/summary.json               该中间件的汇总（含指标与错误）
    songfei/test/<middleware>/changes.json               场景内"性能变化"（组N 相对 组1 的变化率）

运行方式（在仓库根 D:\\AI_project\\yihui_shixi 下执行，用控制台同一解释器）：
    cd D:\\AI_project\\yihui_shixi
    python songfei\\test\\run_matrix.py --list                       # 先核对矩阵
    python songfei\\test\\run_matrix.py --middleware vsoa --scenario S08 --groups 1   # 单组，约 5.5 分钟
    python songfei\\test\\run_matrix.py --middleware all --scenario S01-S08 --groups 1-4
    python songfei\\test\\run_matrix.py --middleware zenoh --scenario S01 --scale 0.2  # 冒烟（时长缩到 20%）
  断点续跑：每组结果写 <middleware>/<scenario>/group<N>.json，文件里带该组参数的
            指纹（_matrix.fingerprint）。重跑同一命令时按下面规则决定跑不跑：
              · 文件存在 + 指纹一致 + status=completed  → 跳过 [skip]
              · 文件存在但 status 不是 completed        → 重跑 [retry]（改完代码接着修）
              · 文件存在但指纹不一致（改参数/时长/--scale）→ 重跑 [retry]
              · 文件不存在                              → 跑   [run]
            Ctrl+C 中断不会丢已完成的结果，重跑同一命令即从断点继续（不要加 --force）。
            要无条件全部重跑（或强制覆盖缩短版数据）才加 --force。
  时间预算：S01–S07 每组约 15–25 秒（含启动/清理），S08 每组 100 秒（4 组约 8 分钟）；
            三件中间件合计约 45–60 分钟，建议后台运行并看日志。
  前置条件：MQTT 需要本机 Mosquitto broker 在运行；三件中间件均使用控制台解释器
            （D:\\soft_anzhuang\\Miniconda\\python.exe，已装 paho / zenoh / vsoa 依赖）。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import json
import sys
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # D:\AI_project\yihui_shixi
DATA_ROOT = ROOT / "songfei" / "test"
MODULES = {
    "vsoa": ROOT / "VSOA" / "adapter.py",
    "mqtt": ROOT / "mqtt" / "adapter.py",
    "zenoh": ROOT / "Zenoh" / "adapter.py",
}
for extra in ("VSOA", "mqtt", "Zenoh"):
    if str(ROOT / extra) not in sys.path:
        sys.path.insert(0, str(ROOT / extra))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 注意：各模块内部存在裸 `import adapter` / `from adapter import ...`，
# 所以**不能**把三个模块目录同时塞进 sys.path / PYTHONPATH —— 会互相串味
# （曾实测 MQTT 导入到 Zenoh/adapter.py，报 cannot import name '_probe'）。
# 改成在 load_adapter() 里按模块逐个隔离。
os.environ.setdefault("PYTHONPATH", str(ROOT))

# 指标字段：即"该场景的主要测试目标"的可量化结果
METRIC_FIELDS = (
    "latency_ms", "latency_p95_ms", "latency_p99_ms", "latency_std_ms", "jitter_ms",
    "throughput_mbps", "offered_throughput_mbps", "packet_loss", "final_packet_loss",
    "startup_time_ms", "discovery_time_ms", "cpu_percent", "memory_mb",
    "messages_sent", "messages_received", "unique_deliveries", "expected_deliveries",
    "duplicate_count", "out_of_order_count", "corrupted_count", "latency_sample_count",
    "status",
)

# 每组参数：该场景的影响因子 + 时长控制（S08 以外均 <= 60 s）
PLAN: dict[str, dict] = {
    "S01": {  # 影响因子：发送速率
        "factor": "publish_rate_hz",
        "groups": [
            {"label": "100Hz", "payload_size_bytes": 1024, "publish_rate_hz": 100, "message_count": 1000, "duration_seconds": 10},
            {"label": "500Hz", "payload_size_bytes": 1024, "publish_rate_hz": 500, "message_count": 5000, "duration_seconds": 10},
            {"label": "1000Hz", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "message_count": 10000, "duration_seconds": 10},
            {"label": "2000Hz", "payload_size_bytes": 1024, "publish_rate_hz": 2000, "message_count": 20000, "duration_seconds": 10},
        ],
    },
    "S02": {  # 影响因子：payload 大小
        "factor": "payload_size_bytes",
        "groups": [
            {"label": "1KiB", "payload_size_bytes": 1024, "publish_rate_hz": 100, "message_count": 1000, "duration_seconds": 10},
            {"label": "4KiB", "payload_size_bytes": 4096, "publish_rate_hz": 100, "message_count": 1000, "duration_seconds": 10},
            {"label": "16KiB", "payload_size_bytes": 16384, "publish_rate_hz": 100, "message_count": 1000, "duration_seconds": 10},
            {"label": "64KiB", "payload_size_bytes": 65536, "publish_rate_hz": 100, "message_count": 1000, "duration_seconds": 10},
        ],
    },
    "S03": {  # 影响因子：大消息尺寸（固定 20 Hz：官方 S03 的 256 KiB 档速率）
        "factor": "payload_size_bytes",
        "groups": [
            {"label": "64KiB@20Hz", "payload_size_bytes": 65536, "publish_rate_hz": 20, "message_count": 0, "duration_seconds": 10},
            {"label": "128KiB@20Hz", "payload_size_bytes": 131072, "publish_rate_hz": 20, "message_count": 0, "duration_seconds": 10},
            {"label": "256KiB@20Hz", "payload_size_bytes": 262144, "publish_rate_hz": 20, "message_count": 0, "duration_seconds": 10},
            {"label": "512KiB@20Hz", "payload_size_bytes": 524288, "publish_rate_hz": 20, "message_count": 0, "duration_seconds": 10},
        ],
    },
    "S04": {  # 影响因子：发送速率（最高档取官方 10000 Hz）
        "factor": "publish_rate_hz",
        "groups": [
            {"label": "100Hz", "payload_size_bytes": 1024, "publish_rate_hz": 100, "message_count": 0, "duration_seconds": 8},
            {"label": "1000Hz", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "message_count": 0, "duration_seconds": 8},
            {"label": "5000Hz", "payload_size_bytes": 1024, "publish_rate_hz": 5000, "message_count": 0, "duration_seconds": 8},
            {"label": "10000Hz", "payload_size_bytes": 1024, "publish_rate_hz": 10000, "message_count": 0, "duration_seconds": 8},
        ],
    },
    "S05": {  # 影响因子：订阅者数量
        "factor": "subscriber_count",
        "groups": [
            {"label": "1S", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 1, "subscriber_count": 1, "message_count": 0, "duration_seconds": 10},
            {"label": "2S", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 1, "subscriber_count": 2, "message_count": 0, "duration_seconds": 10},
            {"label": "3S", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 1, "subscriber_count": 3, "message_count": 0, "duration_seconds": 10},
            {"label": "4S", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 1, "subscriber_count": 4, "message_count": 0, "duration_seconds": 10},
        ],
    },
    "S06": {  # 影响因子：发布者数量
        "factor": "publisher_count",
        "groups": [
            {"label": "1P", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 1, "subscriber_count": 1, "message_count": 0, "duration_seconds": 10},
            {"label": "2P", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 2, "subscriber_count": 1, "message_count": 0, "duration_seconds": 10},
            {"label": "3P", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 3, "subscriber_count": 1, "message_count": 0, "duration_seconds": 10},
            {"label": "4P", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 4, "subscriber_count": 1, "message_count": 0, "duration_seconds": 10},
        ],
    },
    "S07": {  # 影响因子：并发规模（P x S）
        "factor": "publisher_count x subscriber_count",
        "groups": [
            {"label": "1P1S", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 1, "subscriber_count": 1, "message_count": 0, "duration_seconds": 8},
            {"label": "2P2S", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 2, "subscriber_count": 2, "message_count": 0, "duration_seconds": 8},
            {"label": "3P3S", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 3, "subscriber_count": 3, "message_count": 0, "duration_seconds": 8},
            {"label": "4P4S", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "publisher_count": 4, "subscriber_count": 4, "message_count": 0, "duration_seconds": 8},
        ],
    },
    "S08": {  # 影响因子：持续负载（每组 100 s；规范要求 >=5 分钟，报告需注明本次为 100 s）
        "factor": "sustained_load_100s",
        "groups": [
            {"label": "1KiB_100Hz", "payload_size_bytes": 1024, "publish_rate_hz": 100, "message_count": 0, "duration_seconds": 100},
            {"label": "1KiB_1000Hz", "payload_size_bytes": 1024, "publish_rate_hz": 1000, "message_count": 0, "duration_seconds": 100},
            {"label": "64KiB_100Hz", "payload_size_bytes": 65536, "publish_rate_hz": 100, "message_count": 0, "duration_seconds": 100},
            {"label": "1MiB_5Hz", "payload_size_bytes": 1048576, "publish_rate_hz": 5, "message_count": 0, "duration_seconds": 100},
        ],
    },
}
SCENARIO_ORDER = ["S08", "S01", "S02", "S03", "S04", "S05", "S06", "S07"]  # 从 S08 开始


def load_adapter(name: str):
    path = MODULES[name]
    # 按模块隔离：只让"当前模块目录 + 仓库根"在最前面，其它模块目录从
    # sys.path 与 PYTHONPATH 中摘掉。模块内部的裸 `import adapter` 才不会串味。
    others = {
        str(ROOT / extra) for extra in ("VSOA", "mqtt", "Zenoh") if ROOT / extra != path.parent
    }
    sys.path[:] = [item for item in sys.path if item not in others]
    for wanted, position in ((str(ROOT), 1), (str(path.parent), 0)):
        if wanted in sys.path:
            sys.path.remove(wanted)
        sys.path.insert(position, wanted)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [
            str(path.parent),
            str(ROOT),
            *[
                item
                for item in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                if item and item not in others
            ],
        ]
    )
    spec = importlib.util.spec_from_file_location(f"matrix_{name}_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Adapter()


def find_condition(adapter, scenario: str) -> tuple[int, dict]:
    rows = list(adapter.catalog())
    for index, row in enumerate(rows):
        sid = str(row.get("scenario_name") or row.get("scenario_id") or "")
        if sid.upper() == scenario:
            return index, row
    raise SystemExit(f"{scenario} 不在该中间件的条件下拉里（可能已被范围裁剪）")


def effective_parameters(scenario: str, group: dict, scale: float) -> dict:
    """本组下发给适配器的参数子集（单一事实来源）。

    指纹与 run_group 都用它，避免"改了参数但指纹没变 → 旧数据被当成有效"。
    """
    return {
        "payload_size_bytes": group["payload_size_bytes"],
        "publish_rate_hz": group["publish_rate_hz"],
        "publisher_count": group.get("publisher_count", 1),
        "subscriber_count": group.get("subscriber_count", 1),
        "message_count": group["message_count"],
        "duration_seconds": max(1.0, float(group["duration_seconds"]) * scale),
        "warmup_seconds": 1.0,
        # 收尾窗口 2.0s：0.3s 与 1.0s 都不足以让高负载下的尾部在途消息送达，会被误记成"丢包"。
        # 阶梯实验（S07 组4，4P4S/1000Hz）：drain=1.0 缺 6.1%，drain=2.0/3.0/5.0 缺失全部归零。
        "drain_seconds": 2.0,
        "timeout_seconds": 120,
        "repeats": 1,
    }


def group_fingerprint(middleware: str, scenario: str, group: dict, scale: float) -> str:
    """一组参数的指纹：参数（含时长与 --scale）一变，旧结果就不再算"已完成"。"""
    payload = {
        "middleware": middleware,
        "scenario": scenario,
        "group": group["label"],
        "factor": PLAN[scenario]["factor"],
        "parameters": effective_parameters(scenario, group, scale),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def run_group(adapter, scenario: str, group: dict, scale: float, out_dir: Path) -> dict:
    index, row = find_condition(adapter, scenario)
    template = types.SimpleNamespace(
        scenario_id=scenario,
        scenario_name=row.get("scenario_name") or scenario,
        condition_id=row.get("condition_id"),
        case=row.get("case"),
        title=row.get("title") or row.get("scenario_title") or scenario,
        default_parameters=dict(row),
    )
    params = dict(row)
    params.update(effective_parameters(scenario, group, scale))
    params.update({
        "case_repeats": 1,
        "repeat": 1,
        "output_dir": str(out_dir),
        "artifact_dir": str(out_dir / "artifacts"),
        "network_profile": "baseline",
        "transport_mode": row.get("transport_mode") or "tcp",
        "qos_profile": row.get("qos_profile") or "default",
    })
    started = time.time()
    result = adapter.run(template, params)
    metrics = getattr(result, "metrics", None)
    if metrics is None:
        metrics = result if isinstance(result, dict) else {}
    record = dict(metrics)
    # 补齐"有效样本数 / 唯一交付数"（适配器不一定提供，控制台是在 map_run 里补的）
    if record.get("unique_deliveries") is None:
        record["unique_deliveries"] = record.get("messages_received")
    if record.get("latency_sample_count") is None:
        samples = getattr(result, "samples", None)
        if isinstance(samples, dict):
            values = samples.get("latencies_ms") or samples.get("latency") or []
            record["latency_sample_count"] = len(values)
    record["_matrix"] = {
        "middleware": None,
        "scenario": scenario,
        "group": group["label"],
        "factor": PLAN[scenario]["factor"],
        "template_index": index,
        "parameters": {k: v for k, v in params.items() if k in {
            "payload_size_bytes", "publish_rate_hz", "publisher_count", "subscriber_count",
            "message_count", "duration_seconds", "warmup_seconds", "drain_seconds",
            "timeout_seconds", "repeats", "transport_mode", "qos_profile"}},
        "wall_seconds": round(time.time() - started, 2),
    }
    return record


def select(values: list[str], order: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if "-" in value:
            start, end = value.split("-", 1)
            if start in order and end in order:
                # 区间按字母序取成员（S01..S08 / 1..4 都适用），再按 order 排列。
                # 不能用下标切片：SCENARIO_ORDER 把 S08 放在最前面，用下标切
                # "S01-S08" 会得到空列表，脚本就会静默地什么都不跑。
                members = {item for item in order if start <= item <= end}
                if not members:
                    members = {start, end}
                out.extend(item for item in order if item in members)
                continue
        out.append(value)
    picked = set(out)
    return [item for item in order if item in picked]


def main() -> int:
    parser = argparse.ArgumentParser(description="S01–S08 参数矩阵测试（VSOA / MQTT / Zenoh）")
    parser.add_argument("--middleware", default="all", help="vsoa / mqtt / zenoh / all")
    parser.add_argument("--scenario", action="append", default=None, help="S01..S08 或 S01-S08，可重复")
    parser.add_argument("--groups", default="1-4", help="1,2 或 1-4")
    parser.add_argument("--scale", type=float, default=1.0, help="时长缩放（冒烟用 0.2）")
    parser.add_argument("--s08-seconds", type=float, default=None, help="覆盖 S08 的每组时长")
    parser.add_argument("--force", action="store_true", help="覆盖已有结果")
    parser.add_argument("--list", action="store_true", help="只打印矩阵")
    args = parser.parse_args()

    scenarios = select(args.scenario or SCENARIO_ORDER, SCENARIO_ORDER)
    groups = [int(g) for g in select(args.groups.split(","), [str(i) for i in range(1, 5)])]
    middlewares = list(MODULES) if args.middleware == "all" else [args.middleware]
    unknown = [m for m in middlewares if m not in MODULES]
    if unknown:
        raise SystemExit(f"未知中间件：{unknown}（可选：{', '.join(MODULES)} 或 all）")
    if not scenarios or not groups:
        # 别静默什么都不跑：参数写错时直接报错退出。
        raise SystemExit(f"没有可执行的条件：scenarios={scenarios} groups={groups}")

    if args.list:
        for scenario in SCENARIO_ORDER:
            info = PLAN[scenario]
            print(f"{scenario}  影响因子={info['factor']}")
            for i, group in enumerate(info["groups"], 1):
                print(f"   组{i} {group['label']:14s} {group}")
        return 0

    for name in middlewares:
        adapter = load_adapter(name)
        base = DATA_ROOT / name
        base.mkdir(parents=True, exist_ok=True)
        summary_path = base / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        for scenario in scenarios:
            info = PLAN[scenario]
            for number in groups:
                group = dict(info["groups"][number - 1])
                if scenario == "S08" and args.s08_seconds:
                    group["duration_seconds"] = args.s08_seconds
                scenario_dir = base / scenario
                scenario_dir.mkdir(parents=True, exist_ok=True)
                out_file = scenario_dir / f"group{number}.json"
                fingerprint = group_fingerprint(name, scenario, group, args.scale)

                # 断点续跑：指纹一致且上一轮 completed 才跳过，其余一律重跑
                #（上次失败/被中断、改了代码后重试、改了参数或时长、旧的缩短版数据）。
                if out_file.exists() and not args.force:
                    try:
                        previous = json.loads(out_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        previous = None
                    old_matrix = (previous or {}).get("_matrix") or {}
                    old_fingerprint = old_matrix.get("fingerprint")
                    old_status = (previous or {}).get("status")
                    if old_fingerprint == fingerprint and old_status == "completed":
                        print(f"[skip ] {name} {scenario} 组{number} 已完成，参数未变", flush=True)
                        continue
                    reason = (
                        f"上次 status={old_status}"
                        if old_fingerprint == fingerprint
                        else f"参数已变（{old_fingerprint or '旧结果无指纹'} → {fingerprint}）"
                    )
                    print(f"[retry] {name} {scenario} 组{number} {reason}", flush=True)
                print(f"[run ] {name} {scenario} 组{number} ({group['label']}, "
                      f"{max(1.0, group['duration_seconds'] * args.scale):g}s)", flush=True)
                try:
                    record = run_group(adapter, scenario, group, args.scale, scenario_dir)
                    error = None
                except Exception as exc:  # noqa: BLE001
                    record = {"status": "error",
                              "_matrix_error": f"{type(exc).__name__}: {exc}",
                              "_traceback": traceback.format_exc()[-1200:]}
                    error = record["_matrix_error"]
                # 异常路径下 record 里没有 _matrix，直接取 parameters 会 KeyError，
                # 结果是"某一个条件失败"把后面所有场景和中间件全部带走（已实测）。
                # 这里用当前组参数把 _matrix 补全，保证单组失败只影响该组。
                matrix = record.setdefault("_matrix", {})
                matrix["middleware"] = name
                matrix.setdefault("scenario", scenario)
                matrix.setdefault("group", group["label"])
                matrix.setdefault("factor", info["factor"])
                matrix.setdefault("parameters", effective_parameters(scenario, group, args.scale))
                matrix["fingerprint"] = fingerprint
                out_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                row = {"scenario": scenario, "group": number, "label": group["label"],
                       "factor": info["factor"], "error": error, "fingerprint": fingerprint,
                       "parameters": matrix["parameters"]}
                row.update({k: record.get(k) for k in METRIC_FIELDS})
                if error:
                    row["detail"] = record.get("_traceback", "")
                summary[f"{scenario}-{number}"] = row
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                if error:
                    print(f"       ERROR {error}", flush=True)
                else:
                    print(f"       status={record.get('status')} latency_ms={record.get('latency_ms')} "
                          f"p95={record.get('latency_p95_ms')} loss={record.get('final_packet_loss')} "
                          f"thr={record.get('throughput_mbps')}", flush=True)
                    if record.get("status") != "completed":
                        # 模块自己返回的失败（如 status=error / not_tested）要打印原因，
                        # 否则只能看到一排 None，无法定位。
                        for line in (record.get("errors") or [])[:6]:
                            print(f"       ! {line}", flush=True)
        # 场景内"性能变化"：组 N 相对组 1 的变化率，即该场景主要测试目标随影响因子的变化
        changes: dict[str, dict] = {}
        for scenario in PLAN:
            first = summary.get(f"{scenario}-1")
            if not first:
                continue
            block: dict = {"factor": PLAN[scenario]["factor"], "baseline_group": 1, "deltas_percent": {}}
            for number in range(2, 5):
                row = summary.get(f"{scenario}-{number}")
                if not row:
                    continue
                for metric in METRIC_FIELDS:
                    # 变量名不能用 base：base 是外层中间件结果目录（Path），
                    # 覆盖掉之后下面的 (base / "changes.json") 会变成 str / str → TypeError。
                    baseline_value = first.get(metric)
                    value = row.get(metric)
                    if (
                        isinstance(baseline_value, (int, float))
                        and not isinstance(baseline_value, bool)
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and baseline_value
                    ):
                        block["deltas_percent"][f"组{number}/{metric}"] = round(
                            (value - baseline_value) / baseline_value * 100.0, 2
                        )
            changes[scenario] = block
        (base / "changes.json").write_text(
            json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[done] {name} 汇总：{summary_path}", flush=True)
        print(f"[done] {name} 变化率：{base / 'changes.json'}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Ctrl+C：已完成的组都已落盘，重跑同一命令会从断点继续。
        print("\n[stop] 收到 Ctrl+C：已完成的结果已保留，"
              "重跑同一命令即可从断点继续（不要加 --force）。", flush=True)
        raise SystemExit(130) from None
