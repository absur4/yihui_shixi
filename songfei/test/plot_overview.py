"""总览图：把 S01–S07 七个场景的核心指标 vs 影响因子合并到一张图（4×2 网格）。

每格 = 一个场景；X 轴 = 该场景的影响因子（4 档）；曲线 = 三种中间件；
每格只画该场景**最有区分度**的核心指标（延迟类或吞吐类），并在标题里写明。

数据来源：songfei/test/<mw>/summary.json（真实测量，单轮/组，未插值）。

用法::

    python songfei/test/plot_overview.py
    python songfei/test/plot_overview.py --out songfei/test/report_overview.png
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
FOLDERS = {"vsoa": "vsoa", "mqtt": "MQTT", "zenoh": "zenoh"}
COLORS = {"vsoa": "#1f77b4", "mqtt": "#d62728", "zenoh": "#2ca02c"}

# 场景 → (该场景的影响因子说明, 主指标字段, 主指标名, 次指标字段或 None, 次指标名)
SPECS = {
    "S01": ("发送速率", "latency_ms", "延迟均值", "latency_p95_ms", "P95"),
    "S02": ("payload 尺寸", "latency_ms", "延迟均值", "latency_p95_ms", "P95"),
    "S03": ("payload 尺寸", "throughput_mbps", "实收吞吐", None, None),
    "S04": ("发送速率", "throughput_mbps", "实收吞吐", None, None),
    "S05": ("订阅者数量", "latency_p95_ms", "P95 延迟", None, None),
    "S06": ("发布者数量", "latency_p95_ms", "P95 延迟", None, None),
    "S07": ("并发规模 P×S", "latency_ms", "延迟均值", "latency_p95_ms", "P95"),
}
ORDER = ("S01", "S02", "S03", "S04", "S05", "S06", "S07")


def load(middleware: str) -> dict:
    path = DATA_ROOT / FOLDERS[middleware] / "summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="S01–S07 核心指标总览图")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    summaries = {name: load(name) for name in FOLDERS}
    figure, axes = plt.subplots(2, 4, figsize=(20, 9.5))
    figure.suptitle("四种中间件横向对比总览 · S01–S07 核心指标随影响因子的变化"
                    "（曲线=中间件，X 轴=该场景的影响因子，单轮真实测量）", fontsize=15)

    for slot, scenario in enumerate(ORDER):
        axis = axes.ravel()[slot]
        axis_name, primary_key, primary_label, secondary_key, secondary_label = SPECS[scenario]
        labels = []
        for index in (1, 2, 3, 4):
            label = ""
            for name in FOLDERS:
                row = summaries[name].get(f"{scenario}-{index}") or {}
                if row.get("label"):
                    label = row["label"]
                    break
            labels.append(label or f"组{index}")
        x = np.arange(4)

        def series(name: str, key: str):
            rows = [summaries[name].get(f"{scenario}-{index}") or {} for index in (1, 2, 3, 4)]
            return np.asarray([
                row.get(key) if row.get("status") == "completed" and isinstance(row.get(key), (int, float))
                else np.nan for row in rows], dtype=float)

        for name in FOLDERS:
            values = series(name, primary_key)
            axis.plot(x, values, "-o", color=COLORS[name], linewidth=2,
                      label=f"{name.upper()} {primary_label}")
            for index, value in enumerate(values):
                if not np.isnan(value):
                    axis.annotate(f"{value:.3g}", (x[index], value), textcoords="offset points",
                                  xytext=(0, 7), ha="center", fontsize=7.5, color=COLORS[name])
            if secondary_key:
                axis.plot(x, series(name, secondary_key), "--", color=COLORS[name],
                          linewidth=1.1, alpha=0.7, label=f"{name.upper()} {secondary_label}")
        if scenario in ("S03", "S04"):
            for name in FOLDERS:
                offered = series(name, "offered_throughput_mbps")
                if not np.all(np.isnan(offered)):
                    axis.plot(x, offered, ":", color=COLORS[name], linewidth=1.0, alpha=0.6,
                              label="目标吞吐（按参数）" if name == "vsoa" else None)

        axis.set_xticks(x)
        axis.set_xticklabels(labels, fontsize=9)
        axis.set_xlabel(f"影响因子：{axis_name}", fontsize=9.5)
        axis.set_ylabel("ms" if primary_key.startswith("latency") else "Mbps", fontsize=9.5)
        axis.set_title(f"{scenario} · 看 {primary_label}"
                       + (f" / {secondary_label}" if secondary_label else ""), fontsize=11.5)
        axis.grid(alpha=0.3)
        axis.legend(fontsize=7.5, loc="best", framealpha=0.85)
        axis.tick_params(labelsize=8.5)

    notes = axes.ravel()[7]
    notes.axis("off")
    notes.text(0.0, 0.95, "口径说明", fontsize=12, fontweight="bold", va="top")
    notes.text(0.0, 0.82, "\n".join([
        "· 数据：四家同一组参数的真实测量，每组 1 轮（无跨轮重复）",
        "· 延迟 = 订阅端收到时刻 − 发布端发送时刻（同机 perf_counter_ns）",
        "· 吞吐 = 该轮唯一交付字节 × 8 ÷ 测量窗口（per_publisher）",
        "· CPU/内存口径：VSOA/Zenoh/DDS = 仅端点；MQTT = 端点 + broker",
        "   （对比结论请用 *_endpoints 字段，或看图例中的口径说明）",
        "· S05/S06/S07 的吞吐三家几乎重合，故此处改看 P95 延迟（有区分度）",
        "· S05–S07 使用 2.0 s 收尾窗口；其余场景为旧收尾窗口（见报告说明）",
        "· S09–S12（弱网/启动/故障/正确性）未覆盖",
        "",
        "图来源：songfei/test/plot_overview.py",
        "逐场景细图：compare_S<xx>_by_factor.png",
        "S08 时间序列：compare_S08_G<1..4>.png",
    ]), fontsize=9, va="top", linespacing=1.6)

    figure.tight_layout(rect=(0, 0.01, 1, 0.955))
    out_path = Path(args.out) if args.out else (DATA_ROOT / "compare_overview.png")
    figure.savefig(out_path, dpi=150)
    print(f"[图] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
