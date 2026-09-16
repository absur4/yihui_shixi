from __future__ import annotations

from html import escape
import json
import math
from pathlib import Path

from .planner import summarize_scans


def _text(value):
    if value is None:
        return "不可测 / 未提供"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.6g}" if math.isfinite(value) else "不可测"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    return str(value)


def _esc(value):
    return escape(_text(value), quote=True)


def _table(headers, rows):
    head = "".join(f"<th scope=col>{_esc(h)}</th>" for h in headers)
    body = "".join("<tr>"+"".join(f"<td>{_esc(v)}</td>" for v in row)+"</tr>" for row in rows)
    style = f' style="min-width:{max(680, len(headers)*90)}px"' if len(headers) >= 5 else ""
    return f'<div class="scroll"><table{style}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _pairs(values):
    return _table(("字段", "实际值"), values.items()) if values else "<p>未提供数据。</p>"


def _details(title, value):
    return f"<details><summary>{_esc(title)}</summary><pre>{_esc(value)}</pre></details>"


def _identity(run):
    spec = run.get("spec", {})
    return {key: run.get(key, spec.get(key)) for key in
            ("run_id", "scenario", "direction", "profile", "level", "repeat", "rate_hz",
             "payload_bytes", "subscribers", "duration_s")}


def _bar_chart(runs):
    rows = []
    for run in runs:
        value = run.get("metrics", {}).get("throughput_mbps")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            identity = _identity(run)
            label = " / ".join(str(identity[k]) for k in ("scenario", "direction", "rate_hz", "payload_bytes", "repeat"))
            rows.append((label, value))
    if not rows:
        return "<p>没有可绘制的实测吞吐数据。</p>"
    maximum = max((value for _, value in rows), default=0)
    html = ['<figure><figcaption>各轮接收净荷吞吐（Mbit/s）。标签依次为场景、方向、目标 Hz、净荷 byte、重复槽位；仅作数据展示。</figcaption>']
    for label, value in rows:
        width = 100*value/maximum if maximum > 0 else 0
        html.append(f'<div class="bar-row"><span>{_esc(label)}</span><div class="bar-track"><div class="bar" style="width:{width:.4f}%"></div></div><strong>{_esc(value)}</strong></div>')
    html.append("</figure>")
    return "".join(html)


def _run_section(run, index):
    identity = _identity(run)
    html = [f'<section id="run-{index}"><h2>第 {index} 轮：{_esc(identity["scenario"])} / {_esc(identity["direction"])}</h2>']
    html.append(_pairs(identity))
    verdict = {key: run.get(key) for key in ("execution_status", "run_validity", "capacity_verdict", "latency_slo_verdict", "failure_origin")}
    html.extend(["<h3>执行与指标质量</h3>", _pairs(verdict), _details("时钟质量", run.get("clock_quality", {})),
                 _details("分项指标质量", run.get("metric_quality", {})),
                 "<h3>计数与指标</h3>", _pairs(run.get("counts", {})), _pairs(run.get("metrics", {}))])
    subscribers = run.get("per_subscriber", [])
    if subscribers:
        html.append("<h3>逐订阅者结果</h3>")
        html.append(_table(("订阅者", "容量判定", "目标达成下界", "目标达成上界", "计数", "指标"),
                           ((s.get("subscriber_id"), s.get("capacity_verdict"), s.get("achievement_lower"),
                             s.get("achievement_upper"), s.get("counts"), s.get("metrics")) for s in subscribers)))
    resources = run.get("resources", {})
    html.append("<h3>物理主机资源</h3>")
    resource_rows = []
    for node, values in resources.get("nodes", {}).items():
        resource_rows.append((node, values.get("host_cpu_average_percent"), values.get("memory_peak_mb"),
                              values.get("coverage_ratio"), values.get("roles")))
    html.append(_table(("物理主机", "整机平均 CPU %", "RSS 峰值 MB", "采样覆盖率", "分角色 CPU / RSS"), resource_rows)
                if resource_rows else "<p>没有有效资源采样；不以零填补。</p>")
    html.append(_pairs({key: resources.get(key) for key in ("peak_sum_mb", "synchronous_peak_mb")}))
    cd4 = run.get("cd4")
    if cd4:
        html.append("<h3>持续负载与 10 秒分桶</h3>")
        html.append(_pairs({key: value for key, value in cd4.items() if key not in ("arrival_buckets", "source_cohort_buckets")}))
        for key, title in (("arrival_buckets", "接收端本地到达桶"), ("source_cohort_buckets", "源批次桶")):
            buckets = cd4.get(key, [])
            if isinstance(buckets, list) and buckets and all(isinstance(b, dict) for b in buckets):
                columns = list(dict.fromkeys(k for b in buckets for k in b))
                html.extend([f"<h4>{title}</h4>", _table(columns, ([b.get(k) for k in columns] for b in buckets))])
            else:
                html.append(_details(title, buckets))
    html.append(_details("限制与说明", run.get("limitations", [])))
    html.append(_details("完整单轮结果", run))
    html.append("</section>")
    return "".join(html)


def write_report(path: Path, result: dict) -> None:
    if not isinstance(result, dict):
        raise ValueError("report result must be an object")
    values = result.get("runs", result.get("results"))
    runs = [row for row in values if isinstance(row, dict)] if isinstance(values, list) else [result]
    suite = isinstance(values, list)
    title = "MQTT 跨设备测试汇总" if suite else "MQTT 跨设备单轮测试报告"
    html = ["<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
            f"<title>{title}</title>",
            """<style>
body{margin:0;background:#fff;color:#20252b;font:15px/1.65 'Microsoft YaHei','Noto Sans CJK SC',sans-serif;letter-spacing:0}
main{max-width:1180px;margin:auto;padding:30px 24px 60px}h1{font-size:27px;line-height:1.3;margin:0 0 14px}h2{font-size:21px;margin:0 0 16px}h3{font-size:17px;margin:22px 0 8px}h4{font-size:15px}
p{margin:8px 0 18px}.notice{border-left:4px solid #a76c11;padding:12px 16px;background:#fff9ed}section{border-top:2px solid #d8dde1;padding-top:24px;margin-top:32px}.scroll{overflow:auto;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:13px;table-layout:auto}th,td{text-align:left;vertical-align:top;padding:8px 10px;border:1px solid #d8dde1;overflow-wrap:anywhere;white-space:pre-wrap}th{background:#f0f3f4;font-weight:600}tr:nth-child(even) td{background:#fafbfb}
details{margin:12px 0;border-top:1px solid #d8dde1;padding-top:8px}summary{cursor:pointer;color:#176c55}pre{font:12px/1.6 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f7;padding:14px}
figure{margin:18px 0}figcaption{font-size:13px;margin-bottom:12px;color:#57616a}.bar-row{display:grid;grid-template-columns:minmax(180px,1fr) 2fr 75px;gap:12px;align-items:center;margin:7px 0;font-size:12px}.bar-track{height:14px;background:#edf0f2}.bar{height:14px;background:#258475}.bar-row strong{text-align:right}.bar-row span{overflow-wrap:anywhere}
@media(max-width:650px){main{padding:22px 12px}h1{font-size:23px}.bar-row{grid-template-columns:1fr 1fr 58px;gap:7px}}
@media print{main{max-width:none;padding:0}section{break-before:auto}tr{break-inside:avoid}details{display:none}.scroll{overflow:visible}.bar{print-color-adjust:exact}}
</style><main>""", f"<h1>{title}</h1>",
            '<p class="notice">本报告只展示输入结果中的实际数据。不可测值不补零；执行完成不等于容量通过。单程延迟、容量窗口和资源质量分别判断。所有结论仅适用于记录的设备、方向、profile 和观测时长。</p>']
    if suite:
        html.append(_pairs(dict(experiment_id=result.get("experiment_id"),
                                execution_status=result.get("execution_status", result.get("status")),
                                metric_definition_version=result.get("metric_definition_version", result.get("schema_version")))))
        summary_rows = []
        for run in runs:
            identity = _identity(run)
            m = run.get("metrics", {})
            summary_rows.append([identity.get(k) for k in ("scenario", "direction", "profile", "duration_s", "rate_hz", "payload_bytes", "subscribers", "repeat")]+[
                run.get("execution_status"), run.get("capacity_verdict"), run.get("latency_slo_verdict"),
                m.get("throughput_mbps"), m.get("latency_p95_ms"), m.get("final_missing_ratio")])
        html.extend(["<h2>逐轮实际结果</h2>", _table(("场景", "方向", "profile", "正式 s", "目标 Hz", "净荷 byte", "订阅数", "槽位", "执行", "容量", "延迟 SLO", "接收 Mbit/s", "p95 ms", "最终未交付率"), summary_rows)])
        references = result.get("reference_runs", [])
        references = [row for row in references if isinstance(row, dict)] if isinstance(references, list) else []
        scans = summarize_scans(references+runs)
        html.extend(["<h2>五槽位条件与边界</h2>",
                     "<p>至少 4/5 轮容量通过或失败才判为稳定条件；少于四个可测数值不发布中位数。确认与粗扫分开；尾延迟按单轮计算后汇总。</p>"])
        if references:
            html.append(f"<p>边界与条件汇总同时使用此前 {len(references)} 轮引用证据及本批 {len(runs)} 轮结果；上方逐轮表为本批实际执行记录。</p>")
        html.append(_table(("场景", "方向", "档位", "正式 s", "条件判定", "通过", "失败", "不确定", "槽位数"),
                           ((c["scenario"], c["direction"], c["level"], c["duration_s"], c["status"],
                             c["passes"], c["failures"], c["indeterminate"], c["slots_present"]) for c in scans["conditions"])))
        for key, caption in (("boundaries", "60 秒确认容量边界"), ("latency_knees", "单程延迟拐点"), ("payload_plateaus", "载荷吞吐平台")):
            html.append(_details(caption, scans[key]))
        html.append(_details("分条件数值汇总及全部原轮标识", scans["conditions"]))
        html.append(_details("未执行条件与原因", result.get("skipped", [])))
    html.extend(["<h2>实测吞吐</h2>", _bar_chart(runs)])
    for index, run in enumerate(runs, 1):
        html.append(_run_section(run, index))
    html.append(_details("完整报告输入", result))
    html.append("</main></html>")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(html), encoding="utf-8")
