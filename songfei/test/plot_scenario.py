"""按"场景的影响因子"出对比图：X 轴 = 该场景要扫的那个参数，曲线 = 中间件。

和 plot_compare.py 的分工：

- ``plot_scenario.py``（本文件）：S01–S07 用。X 轴是该场景的影响因子
  （S01/S04=发送速率，S02/S03=payload 尺寸，S05=订阅者数，S06=发布者数，S07=P×S），
  4 个点 = 该场景的 4 组参数；曲线是中间件；4 个子图 = 该场景的核心指标。
- ``plot_compare.py``：只有 S08（长时间稳定性）才用，因为**只有 S08 的 X 轴是时间**。

数据来源：``songfei/test/<mw>/summary.json``（由 run_matrix.py 落盘，含各组的指标与参数），
不读原始样本，不做插值或生成。

用法::

    python songfei/test/plot_scenario.py --scenario S02
    python songfei/test/plot_scenario.py --scenario S01 --middlewares vsoa,mqtt,zenoh
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

DATA_ROOT = Path(__file__).resolve().parent
COLORS = {"vsoa": "#1f77b4", "mqtt": "#d62728", "zenoh": "#2ca02c", "dds": "#9467bd"}
FOLDERS = {"vsoa": "vsoa", "mqtt": "MQTT", "zenoh": "zenoh", "dds": "DDS"}

# 场景 → (X 轴标题, 该场景该看的指标组合)
SCENARIO_AXIS = {
    "S01": "发送速率（组别，Hz）",
    "S02": "payload 尺寸（组别）",
    "S03": "payload 尺寸（组别，固定 20 Hz）",
    "S04": "发送速率（组别）",
    "S05": "订阅者数量（组别）",
    "S06": "发布者数量（组别）",
    "S07": "并发规模 P×S（组别）",
}
SCENARIO_MEANING = {
    "S01": "点对点低延迟：延迟随发送速率的变化",
    "S02": "消息大小扫描：延迟/吞吐/丢失随 payload 尺寸的变化",
    "S03": "大消息吞吐：不限速下吞吐随尺寸的变化",
    "S04": "发送速率扫描：延迟/吞吐随速率的变化",
    "S05": "一对多广播：交付与资源随订阅者数量的变化",
    "S06": "多对一汇聚：交付与资源随发布者数量的变化",
    "S07": "多对多并发：吞吐与资源随并发规模的变化",
}


def load_summary(middleware: str):
    path = DATA_ROOT / FOLDERS[middleware] / "summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="按场景影响因子出对比图")
    parser.add_argument("--scenario", required=True, help="S01..S07（S08 请用 plot_compare.py）")
    parser.add_argument("--middlewares", default="vsoa,mqtt,zenoh")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    scenario = args.scenario.upper()
    if scenario not in SCENARIO_AXIS:
        raise SystemExit(f"{scenario} 的影响因子不是参数而是时间，请用 plot_compare.py --scenario {scenario} --group N")
    names = [item.strip().lower() for item in args.middlewares.split(",") if item.strip()]

    summaries = {name: load_summary(name) for name in names}
    groups = [1, 2, 3, 4]
    labels, problems = [], []
    for number in groups:
        row = next((summaries[name].get(f"{scenario}-{number}") for name in names
                    if summaries[name].get(f"{scenario}-{number}")), None)
        labels.append(row.get("label") if row else f"组{number}")
    x = np.arange(len(groups))

    figure, axes = plt.subplots(2, 2, figsize=(14, 9.5))
    loss_seen: list[float] = []
    figure.suptitle(f"{scenario} · {SCENARIO_MEANING.get(scenario, '')}\n"
                    f"X 轴 = {SCENARIO_AXIS[scenario]} · 每个点 1 轮真实测量", fontsize=13)

    for name in names:
        summary = summaries[name]
        color = COLORS.get(name)
        series = {}
        for number in groups:
            row = summary.get(f"{scenario}-{number}") or {}
            series[number] = row
            if row.get("error"):
                problems.append(f"{name.upper()} 组{number}（{row.get('label')}）无数据：{row['error']}")
        if not any(series[n].get("status") == "completed" for n in groups):
            continue

        def pick(field):
            return np.asarray([series[n].get(field) if series[n].get("status") == "completed" else np.nan
                               for n in groups], dtype=float)

        latency, p95 = pick("latency_ms"), pick("latency_p95_ms")
        throughput, offered = pick("throughput_mbps"), pick("offered_throughput_mbps")
        loss = pick("final_packet_loss")
        if np.all(np.isnan(loss)):
            loss = pick("packet_loss")
        cpu, memory = pick("cpu_percent"), pick("memory_mb")

        axes[0][0].plot(x, latency, "-o", color=color, linewidth=2, label=f"{name.upper()} 均值")
        axes[0][0].plot(x, p95, "--", color=color, linewidth=1.2, alpha=0.75, label=f"{name.upper()} P95")
        axes[0][1].plot(x, throughput, "-o", color=color, linewidth=2, label=f"{name.upper()} 实收")
        axes[0][1].plot(x, offered, ":", color=color, linewidth=1.1, alpha=0.7,
                        label="目标吞吐（按该组参数）" if name == names[0] else None)
        axes[1][0].plot(x, loss * 100.0, "-o", color=color, linewidth=2, label=f"{name.upper()} 最终丢包率")
        axes[1][1].plot(x, cpu, "-o", color=color, linewidth=2, label=f"{name.upper()} CPU%")
        axes[1][1].plot(x, memory, "--", color=color, linewidth=1.2, alpha=0.75,
                        label="内存 MB（同色虚线）" if name == names[0] else None)
        loss_seen.append(np.nanmax(loss * 100.0) if not np.all(np.isnan(loss)) else np.nan)

        for axis, values in ((axes[0][0], latency), (axes[0][1], throughput),
                             (axes[1][0], loss * 100.0), (axes[1][1], cpu)):
            for slot, value in enumerate(values):
                if not np.isnan(value):
                    axis.annotate(f"{value:.3g}", (x[slot], value), textcoords="offset points",
                                  xytext=(0, 6), ha="center", fontsize=7.5, color=color)

    axes[0][0].set(title="延迟（实线=均值，虚线=P95）", ylabel="ms")
    axes[0][1].set(title="吞吐（实线=实收，点线=该组参数的目标值）", ylabel="Mbps")
    axes[1][0].set(title="最终丢包率", ylabel="%")
    axes[1][1].set(title="资源占用（实线=CPU%，虚线=内存 MB）", ylabel="CPU% / MB")
    for axis in axes.ravel():
        axis.set_xticks(x)
        axis.set_xticklabels(labels)
        axis.set_xlabel(SCENARIO_AXIS[scenario])
        axis.grid(alpha=0.3)
        axis.legend(fontsize=7.5, loc="best", framealpha=0.85)
    if loss_seen and not np.all(np.isnan(loss_seen)) and float(np.nanmax(loss_seen)) == 0.0:
        axes[1][0].set_ylim(-0.5, 0.5)
        axes[1][0].text(0.5, 0.82, "本场景三家最终丢包率均为 0.00%（曲线重合在 0）",
                        transform=axes[1][0].transAxes, ha="center", fontsize=9, color="#444444")

    note = ("X 轴是本场景的影响因子（不是时间）；S08 的时间序列请用 plot_compare.py。"
            "CPU 口径：VSOA=仅端点、MQTT=端点+broker、Zenoh=其自报范围。数据来自真实测量，未插值。")
    if problems:
        note += "\n无数据的点：" + "；".join(problems)
    figure.text(0.5, 0.005, note, ha="center", fontsize=8.5, color="#555555")
    figure.tight_layout(rect=(0, 0.04, 1, 0.93))

    out_path = Path(args.out) if args.out else (DATA_ROOT / f"compare_{scenario}_by_factor.png")
    figure.savefig(out_path, dpi=150)
    print(f"[图] {out_path}  X轴={SCENARIO_AXIS[scenario]}  组标签={labels}")
    for line in problems:
        print("  [!]", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
