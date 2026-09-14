import html
import json
from pathlib import Path


def render(output, suite, groups):
    output = Path(output)
    runs = suite['runs']
    lines = ['# MQTT 统一测试报告', '',
        '本报告仅包含 MQTT 实测。没有其他中间件同条件数据，因此不生成跨中间件排名。', '',
        f"套件开始：{suite['suite_start_time']}；最近保存：{suite['suite_end_time']}。", '',
        '指标定义 1.0；延迟单位 ms，吞吐 Mbit/s，内存十进制 MB，丢失为 0–1 比例。',
        '汇总的 P95/P99 为重复轮次指标值的分位数（run_level），不是合并消息分位数。',
        '完整实际配置、原始数据路径、限制和错误见各轮 JSON。HTML 报告支持筛选并查看逐轮结果。', '',
        '| 场景/条件 | payload B | Hz/pub | P/S | 完成/总轮数 | 平均延迟 ms | 交付吞吐 Mbit/s | 失败/超时 |',
        '|---|---:|---:|---|---:|---:|---:|---|']
    fmt = lambda x: 'null' if x is None else f'{x:.6g}'
    for g in groups:
        c, s = g['conditions'], g['statistics']
        lines.append(f"| {c['scenario_name']}/{c['case']} | {c['payload_size_bytes']} | {c['publish_rate_hz']} | "
                     f"{c['publisher_count']}/{c['subscriber_count']} | {g['valid_repeats']}/{g['count']} | "
                     f"{fmt(s['latency_ms']['mean'])} | {fmt(s['throughput_mbps']['mean'])} | {g['failed_count']}/{g['timeout_count']} |")
    lines += ['', '## 不可忽略的限制', '',
              '- 烟雾测试不满足正式时长、消息数及重复次数，benchmark_profile=smoke。',
              '- S09 非零弱网条件未注入时标记 not_tested，不计为有效重复。',
              '- S10 故意触发的失败/超时保留真实 status；expected_negative_outcome 单独表示负向测试符合预期。',
              '- S11 故障及恢复探针在原始通信排空后独立执行，不能据此推断业务流量在故障期间的丢失量。',
              '- 最大稳定速率是候选网格内、当前适配器与阈值下的实测值，不是 Broker 理论峰值。',
              '- 逐条件保留未完成、失败和超时；只汇总 completed 轮次，不用零填补失败。', '']
    (output/'report.md').write_text('\n'.join(lines), encoding='utf-8')
    data = json.dumps(dict(groups=groups, runs=runs), ensure_ascii=False).replace('</', '<\\/')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>MQTT 统一测试报告</title>
<style>body{font:15px system-ui;margin:32px;background:#f4f7fa;color:#172d43}h1{font-size:28px}select{padding:8px;margin:4px}table{border-collapse:collapse;background:white;width:100%;font-size:13px}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left}th{background:#dce8f1;position:sticky;top:0}pre{white-space:pre-wrap;background:#fff;padding:16px;max-height:70vh;overflow:auto}button{cursor:pointer;padding:5px}label{display:inline-block}</style>
<h1>MQTT 统一测试报告</h1><p>统一指标 1.0 · 单机独立端点进程 + Mosquitto · 单位：ms / Mbit/s / MB</p>
<p>仅同条件结果可直接比较。当前不含其他中间件结果，不生成跨中间件排名。点击“详情”查看完整配置、统计窗口、资源进程、交付矩阵、各轮结果与限制。</p>
<div id="filters"></div><p id="counts"></p><table><thead><tr><th>场景 / 条件</th><th>大小 B</th><th>Hz/pub</th><th>P/S</th><th>QoS</th><th>网络</th><th>有效/总轮次</th><th>延迟均值</th><th>吞吐均值</th><th></th></tr></thead><tbody id="body"></tbody></table>
<h2>条件详情 / 原始轮次</h2><div id="links"></div><pre id="details">选择一行查看 count、mean、median、variance、std、min、max、P95、P99；汇总分位数层级为 run_level。</pre>
<script type="application/json" id="data">DATA</script><script>
const data=JSON.parse(document.getElementById('data').textContent);
const fields=['scenario_name','payload_size_bytes','publish_rate_hz','publisher_count','subscriber_count','qos_profile','network_profile','benchmark_profile','case'];
const labels=['场景','消息大小','速率','发布者','订阅者','QoS','网络条件','测试类型','子条件'];
const controls={}; fields.forEach((f,i)=>{let l=document.createElement('label');l.textContent=labels[i]+' ';let s=document.createElement('select');
let a=document.createElement('option');a.value='';a.textContent='全部';s.append(a);
[...new Set(data.groups.map(g=>String(g.conditions[f])))].sort().forEach(v=>{let o=document.createElement('option');o.value=v;o.textContent=v;s.append(o)});
l.append(s);document.getElementById('filters').append(l);controls[f]=s;s.onchange=draw});
function fmt(v){return v==null?'未测量':Number(v).toPrecision(5)}
function draw(){let gs=data.groups.filter(g=>fields.every(f=>!controls[f].value||String(g.conditions[f])===controls[f].value));
document.getElementById('counts').textContent='显示 '+gs.length+' 个独立条件；失败与超时不计入有效重复。';let body=document.getElementById('body');body.replaceChildren();
gs.forEach(g=>{let c=g.conditions,s=g.statistics,tr=document.createElement('tr');
[c.scenario_name+' / '+c.case,c.payload_size_bytes,c.publish_rate_hz,c.publisher_count+'/'+c.subscriber_count,c.qos_profile,c.network_profile,g.valid_repeats+'/'+g.count,fmt(s.latency_ms.mean),fmt(s.throughput_mbps.mean)].forEach(v=>{let td=document.createElement('td');td.textContent=v;tr.append(td)});
let td=document.createElement('td'),b=document.createElement('button');b.textContent='详情';b.onclick=()=>{let selected=data.runs.filter(r=>g.run_ids.includes(r.run_id));document.getElementById('details').textContent=JSON.stringify({summary:g,runs:selected},null,2);let links=document.getElementById('links');links.replaceChildren();selected.forEach(r=>{let a=document.createElement('a');a.href='runs/'+r.run_id+'.json';a.textContent='第 '+r.repeat+' 轮 ('+r.status+') ';links.append(a)})};td.append(b);tr.append(td);body.append(tr)})}draw();
</script></html>'''.replace('DATA', data)
    (output/'report.html').write_text(page, encoding='utf-8')

