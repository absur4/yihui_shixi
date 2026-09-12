"""统一通信性能实验台：为 example.html 控制台提供 API，VSOA 全量真实测量。

前端:  static/index.html（源自 example.html，token 注入）
引擎:  VSOA/vsoa_py/standalone（S01-S12 标准条件全部由真实引擎执行）
产物:  results/vsoa/<job_id>/{job.json, spec.json, console.log, output/...}

不做任何模拟或伪造：未接入的中间件不会出现在下拉列表中。
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
RESULTS = HERE / "results"
VSOA_PY = ROOT / "VSOA" / "vsoa_py"
RUNNER = HERE / "vsoa_runner.py"
BASE_CONFIG = VSOA_PY / "delivery_template" / "config.yaml"

for _path in (str(ROOT), str(VSOA_PY)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from interfaces.scenarios import SCENARIOS  # noqa: E402

try:
    import vsoa as vsoa_package  # noqa: E402
    from standalone.configuration import load_config, validate_case  # noqa: E402
    from standalone.qualification import qualification_cases  # noqa: E402
    VSOA_IMPORT_ERROR: str | None = None
except Exception as error:  # 环境异常时控制台仍可启动，但 VSOA 不可用
    VSOA_IMPORT_ERROR = f"{type(error).__name__}: {error}"

SCENARIO_TITLES = {scenario.scenario_id: scenario.title for scenario in SCENARIOS}
ACTIVE_STATES = {"starting", "running", "stopping"}
LOG_LIMIT = 400
SAMPLE_LIMIT = 4000
TOKEN = secrets.token_hex(16)
LOG_LINE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(.*)$")


def read_json_safe(path, attempts=3):
    for attempt in range(attempts):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if attempt == attempts - 1:
                return None
            time.sleep(0.02)
    return None


def parse_log_line(text):
    match = LOG_LINE.match(text)
    if match:
        return {"time": match.group(1), "text": match.group(2)}
    return {"time": datetime.now().strftime("%H:%M:%S"), "text": text}


def read_log_tail(path, limit=LOG_LIMIT):
    if not Path(path).exists():
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries = [parse_log_line(line.strip()) for line in lines[-limit * 2:]]
    return [entry for entry in entries if entry["text"]][-limit:]


def translate_validation(message):
    """把引擎的英文校验错误翻译为面向使用者的中文提示。"""
    rules = (
        ("message_size_bytes", "消息大小超出限制：UDP 单消息最大 60000 字节，TCP 最大 16 MiB"),
        ("publish_rate_hz", "发送速率需在 1–50000 Hz 之间"),
        ("publisher_count", "VSOA 发布者数量需在 1–8 之间"),
        ("subscriber_count", "VSOA 订阅者数量需在 1–16 之间"),
        ("Network impairment", "网络损伤（丢包/延迟/抖动）仅支持 UDP 传输模式"),
        ("planned deliveries", "单轮计划交付总数超过 200 万，请降低时长、速率或拓扑规模"),
        ("duration_seconds", "正式时长需在 0.1–3600 秒之间"),
        ("loss_rate", "丢包率需在 0–100% 之间"),
        ("network_delay_ms", "基础延迟需在 0–1000 ms 之间"),
        ("network_jitter_ms", "网络抖动需在 0–1000 ms 之间"),
        ("transport_mode", "传输模式仅支持 TCP 或 UDP"),
    )
    for key, text in rules:
        if key in message:
            return text
    return f"配置校验失败：{message}"


def build_catalog():
    """加载 VSOA 标准配置并展开 S01–S12 全部标准条件。"""
    config, _expanded = load_config(BASE_CONFIG)
    cases, _plan = qualification_cases(config, "all")
    templates = []
    for case in cases:
        templates.append({
            "scenario_name": case["scenario_id"],
            "case": case["scenario_name"],
            "condition_id": case["scenario_name"],
            "title": case.get("scenario_title"),
            "payload_size_bytes": case["message_size_bytes"],
            "publish_rate_hz": case["publish_rate_hz"],
            "publisher_count": case["publisher_count"],
            "subscriber_count": case["subscriber_count"],
            "message_count": case["message_count"],
            "duration_seconds": case["duration_seconds"],
            "repeats": case["case_repeats"],
            "random_seed": case["seed"],
            "warmup_seconds": case["warmup_seconds"],
            "drain_seconds": case["drain_seconds"],
            "timeout_seconds": case["startup_timeout_seconds"],
            "network_delay_ms": case["network_delay_ms"],
            "network_jitter_ms": case["network_jitter_ms"],
            "network_loss_rate": case["loss_rate"],
            "network_profile": case.get("network_profile"),
            "transport_mode": case["transport_mode"],
            "qos_profile": case.get("qos_profile", "default"),
        })
    return config, templates, cases


if VSOA_IMPORT_ERROR is None:
    VSOA_CONFIG, TEMPLATES, ENGINE_CASES = build_catalog()
else:  # pragma: no cover
    VSOA_CONFIG, TEMPLATES, ENGINE_CASES = None, [], []


def vsoa_backend():
    if VSOA_IMPORT_ERROR:
        return {"id": "vsoa", "label": "VSOA", "available": False, "version": None,
                "transport_options": [], "qos_options": [],
                "notes": [f"VSOA 引擎导入失败：{VSOA_IMPORT_ERROR}"]}
    return {"id": "vsoa", "label": "VSOA", "available": True,
            "version": getattr(vsoa_package, "__version__", None),
            "transport_options": ["tcp", "udp"],
            "qos_options": [{"value": "default", "label": "原生默认"}],
            "notes": [f"VSOA 已全量接入：S01–S12 共 {len(TEMPLATES)} 个标准条件，全部指标由真实引擎测量，不生成模拟数据。",
                      "网络条件通过真实 UDP 代理进程注入（丢包/延迟/抖动/断网）；TCP 模式不支持网络损伤。",
                      "UDP 单消息上限 60 KiB，TCP 大消息自动应用层分片；发布者 ≤ 8、订阅者 ≤ 16。"]}


def map_run(run):
    """把引擎单轮报告映射为控制台字段（保留全部原始字段供详情查看）。"""
    configuration = dict(run.get("configuration") or {})
    configuration["case"] = run.get("scenario_title") or run.get("scenario_name")
    mapped = dict(run)
    mapped.update({
        "unique_deliveries": run.get("messages_received"),
        "achieved_publish_rate_hz": run.get("achieved_publish_rate_hz_per_publisher"),
        "latency_sample_count": ((run.get("statistics") or {}).get("latency_ms") or {}).get("count"),
        "configuration": configuration,
    })
    return mapped


def _kill_tree(pid):
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(children, timeout=3)
    try:
        parent.kill()
    except psutil.Error:
        pass
    try:
        parent.wait(timeout=3)
    except psutil.Error:
        pass


class JobManager:
    """单活动作业管理：启动、跟随日志、停止、查询结果与历史。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        self.process = None
        self.logs: list[dict] = []
        self.stopping = False

    def job_dir(self, job_id):
        return RESULTS / "vsoa" / str(job_id)

    def _write_job(self, job):
        path = self.job_dir(job["id"]) / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("job.json.tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def load_jobs(self):
        records = []
        base = RESULTS / "vsoa"
        if base.exists():
            for job_file in sorted(base.glob("*/job.json")):
                data = read_json_safe(job_file, attempts=1)
                if data and "id" in data:
                    records.append(data)
        records.sort(key=lambda item: item.get("started") or 0, reverse=True)
        return records

    def recover(self):
        """服务重启后，把残留的运行中状态标记为已中断。"""
        for job in self.load_jobs():
            if job.get("status") in ACTIVE_STATES:
                job["status"] = "interrupted"
                job["ended"] = job.get("ended") or time.time()
                self._write_job(job)

    def _number(self, values, key, default, minimum, maximum, integer=False):
        raw = values.get(key, default)
        try:
            value = int(raw) if integer else float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"参数 {key} 必须是数字") from None
        if minimum is not None and value < minimum:
            raise ValueError(f"参数 {key} 低于允许的最小值 {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"参数 {key} 超过允许的最大值 {maximum}")
        return value

    def _build_case(self, template_index, configuration):
        """用控制台表单参数覆盖标准条件，返回可直接交给引擎的 case。"""
        case = dict(ENGINE_CASES[template_index])
        values = configuration or {}
        case.update(
            message_size_bytes=self._number(values, "payload_size_bytes", case["message_size_bytes"], 1, 16 * 1024 ** 2, True),
            publish_rate_hz=self._number(values, "publish_rate_hz", case["publish_rate_hz"], 1, 50000),
            publisher_count=self._number(values, "publisher_count", case["publisher_count"], 1, 8, True),
            subscriber_count=self._number(values, "subscriber_count", case["subscriber_count"], 1, 16, True),
            message_count=self._number(values, "message_count", case["message_count"], 0, 100_000_000, True),
            duration_seconds=self._number(values, "duration_seconds", case["duration_seconds"], 0.1, 3600),
            warmup_seconds=self._number(values, "warmup_seconds", case["warmup_seconds"], 0, 60),
            drain_seconds=self._number(values, "drain_seconds", case["drain_seconds"], 0.05, 30),
            network_delay_ms=self._number(values, "network_delay_ms", case["network_delay_ms"], 0, 1000),
            network_jitter_ms=self._number(values, "network_jitter_ms", case["network_jitter_ms"], 0, 1000),
        )
        case["case_repeats"] = self._number(values, "repeats", case["case_repeats"], 1, 30, True)
        case["seed"] = self._number(values, "random_seed", case["seed"], 0, 2 ** 31 - 1, True)
        case["startup_timeout_seconds"] = self._number(values, "timeout_seconds", case["startup_timeout_seconds"], 2, 120)
        case["loss_rate"] = self._number(values, "network_loss_rate", case["loss_rate"], 0, 1)
        transport = str(values.get("transport_mode") or case["transport_mode"]).lower()
        if transport not in {"tcp", "udp"}:
            raise ValueError("传输模式仅支持 TCP 或 UDP")
        case["transport_mode"] = transport
        case["qos_profile"] = str(values.get("qos_profile") or case.get("qos_profile") or "default")
        case["payload_size_bytes"] = case["message_size_bytes"]
        case["network_loss_rate"] = case["loss_rate"]
        case["random_seed"] = case["seed"]
        try:
            validate_case(case)
        except ValueError as error:
            raise ValueError(translate_validation(str(error))) from None
        return case

    def start(self, template_index, configuration, matrix):
        with self.lock:
            if self.job and self.job["status"] in ACTIVE_STATES:
                raise ValueError("已有一个实验正在运行，请先停止或等待其完成")
        if VSOA_IMPORT_ERROR or not ENGINE_CASES:
            raise ValueError("VSOA 引擎不可用，无法启动测试")
        try:
            index = int(template_index)
        except (TypeError, ValueError):
            raise ValueError("无效的测试条件") from None
        if not 0 <= index < len(ENGINE_CASES):
            raise ValueError("无效的测试条件")
        template = TEMPLATES[index]
        scenario_id = template["scenario_name"]
        if matrix:
            cases = [dict(case) for case in ENGINE_CASES if case["scenario_id"] == scenario_id]
        else:
            cases = [self._build_case(index, configuration)]
        total = sum(case["case_repeats"] for case in cases)
        plan = [{"scenario_id": case["scenario_id"], "scenario_name": case["scenario_name"],
                 "scenario_title": case.get("scenario_title"), "phase": case.get("phase"),
                 "status": "planned", "reason": None, "configuration": case,
                 "planned_repeats": case["case_repeats"]} for case in cases]
        job_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        output = job_dir / "output"
        job = {"id": job_id, "middleware_id": "vsoa", "status": "starting",
               "started": time.time(), "ended": None, "total": total, "matrix": bool(matrix),
               "scenario": scenario_id, "case": template["case"], "template_index": index,
               "configuration": configuration}
        spec = {"config": VSOA_CONFIG, "cases": cases, "plan": plan,
                "output": str(output), "logs": str(output / "logs")}
        (job_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_job(job)
        command = [sys.executable, str(RUNNER), str(job_dir / "spec.json")]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=flags, cwd=str(VSOA_PY),
                                   env=dict(os.environ, PYTHONUTF8="1"))
        with self.lock:
            self.job = job
            self.process = process
            self.stopping = False
            self.logs = [{"time": datetime.now().strftime("%H:%M:%S"),
                          "text": f"控制台已启动 VSOA 引擎：{template['case']}" + ("（完整矩阵）" if matrix else "（当前条件）")}]
        threading.Thread(target=self._follow, args=(process, job, job_dir), daemon=True).start()
        return job

    def _follow(self, process, job, job_dir):
        console = open(job_dir / "console.log", "w", encoding="utf-8", errors="replace")
        final = None
        stream = io.TextIOWrapper(process.stdout, encoding="utf-8", errors="replace")
        try:
            for raw in stream:
                text = raw.strip()
                if not text:
                    continue
                entry = parse_log_line(text)
                with self.lock:
                    self.logs.append(entry)
                    if len(self.logs) > 4000:
                        del self.logs[:2000]
                console.write(raw if raw.endswith("\n") else raw + "\n")
                console.flush()
                if "RESULT " in text and "status=" in text:
                    final = text.rsplit("status=", 1)[-1].strip()
        finally:
            try:
                stream.close()
            except Exception:
                pass
            console.close()
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill_tree(process.pid)
            code = -1
        with self.lock:
            if self.stopping:
                status = "cancelled"
            elif final == "completed" and code == 0:
                status = "completed"
            elif final in {"error", "cancelled"}:
                status = final
            else:
                status = "error"
            self.stopping = False
            job["status"] = status
            job["ended"] = time.time()
            self.logs.append({"time": datetime.now().strftime("%H:%M:%S"),
                              "text": f"实验结束：{status}（引擎退出码 {code}）"})
            self._write_job(job)

    def stop(self):
        with self.lock:
            job, process = self.job, self.process
            if not job or job["status"] not in ACTIVE_STATES:
                return {"ok": True, "message": "当前没有正在运行的实验"}
            job["status"] = "stopping"
            self.stopping = True
        self._write_job(job)
        if process is not None:
            _kill_tree(process.pid)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        return {"ok": True, "message": "停止请求已发送，正在结束引擎进程"}

    def state(self):
        with self.lock:
            job = dict(self.job) if self.job else None
            logs = list(self.logs[-LOG_LIMIT:])
        return {"job": job, "logs": logs}

    def _samples(self, job_dir, run):
        base = job_dir / "output" / (run.get("artifacts_directory") or f"artifacts/{run.get('run_id')}")
        path = base / "subscriber-0.result.json"
        samples = []
        if path.exists():
            data = read_json_safe(path)
            for index, value in enumerate((data or {}).get("latencies_ms") or []):
                if index >= SAMPLE_LIMIT:
                    break
                samples.append({"sequence": index, "latency": round(float(value), 4)})
        link = None
        for item in run.get("link_metrics") or []:
            if item.get("publisher") == 0 and item.get("subscriber") == 0:
                link = {"latency_p95_ms": (item.get("latency_ms") or {}).get("p95")}
                break
        if link is None:
            link = {"latency_p95_ms": run.get("latency_p95_ms")}
        return samples, link

    def results(self, job_id, run_id=None):
        job_dir = self.job_dir(job_id)
        if not (job_dir / "job.json").exists():
            raise ValueError("找不到该实验记录")
        result = read_json_safe(job_dir / "output" / "result.json")
        runs, mapped, samples, link = [], None, [], None
        if result:
            engine_runs = result.get("runs") or []
            for run in engine_runs:
                runs.append({"run_id": run.get("run_id"), "scenario_name": run.get("scenario_name"),
                             "repeat": run.get("repeat"), "payload_size_bytes": run.get("payload_size_bytes"),
                             "status": run.get("status")})
            selected = None
            if run_id:
                selected = next((run for run in engine_runs if run.get("run_id") == run_id), None)
                if selected is None:
                    raise ValueError("找不到该轮次结果")
            elif engine_runs:
                selected = engine_runs[-1]
            if selected:
                mapped = map_run(selected)
                samples, link = self._samples(job_dir, selected)
        with self.lock:
            if self.job and self.job["id"] == job_id:
                logs = list(self.logs[-LOG_LIMIT:])
            else:
                logs = None
        if logs is None:
            logs = read_log_tail(job_dir / "console.log")
        return {"runs": runs, "result": mapped, "samples": samples, "link": link,
                "report": bool(result and result.get("runs")), "logs": logs}


MANAGER = JobManager()


def api_init():
    return {"middleware": [vsoa_backend()],
            "catalogs": {"vsoa": TEMPLATES},
            "names": SCENARIO_TITLES,
            "history": [{"id": job["id"], "status": job.get("status")} for job in MANAGER.load_jobs()]}


def escape(value):
    return (str(value) if value is not None else "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_metric(value, digits=3):
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return escape(value)


def ratio_percent(value, digits=2):
    return "—" if value is None else f"{value * 100:.{digits}f}"


def build_report(job, result):
    runs = result.get("runs") or []
    summaries = result.get("scenario_summaries") or []
    completed = sum(1 for run in runs if run.get("status") == "completed")

    def summary_row(item):
        return (f"<tr><td>{escape(item.get('scenario_name'))}</td><td>{escape(item.get('repeat_count'))}</td>"
                f"<td>{format_metric(item.get('latency_ms'))}</td><td>{format_metric(item.get('latency_p95_ms'))}</td>"
                f"<td>{format_metric(item.get('latency_p99_ms'))}</td><td>{format_metric(item.get('throughput_mbps'))}</td>"
                f"<td>{ratio_percent(item.get('packet_loss'))}</td><td>{format_metric(item.get('cpu_percent'), 2)}</td>"
                f"<td>{format_metric(item.get('memory_mb'), 2)}</td></tr>")

    def run_row(run):
        return (f"<tr><td>{escape(run.get('run_id'))}</td><td>{escape(run.get('scenario_name'))}</td>"
                f"<td>{escape(run.get('repeat'))}</td><td>{escape(run.get('status'))}</td>"
                f"<td>{escape(run.get('payload_size_bytes'))}</td><td>{escape(run.get('publish_rate_hz'))}</td>"
                f"<td>{escape(run.get('publisher_count'))}P/{escape(run.get('subscriber_count'))}S</td>"
                f"<td>{format_metric(run.get('latency_ms'))}</td><td>{format_metric(run.get('latency_p95_ms'))}</td>"
                f"<td>{format_metric(run.get('latency_p99_ms'))}</td><td>{format_metric(run.get('throughput_mbps'))}</td>"
                f"<td>{ratio_percent(run.get('packet_loss'))}</td><td>{ratio_percent(run.get('final_packet_loss'))}</td>"
                f"<td>{format_metric(run.get('jitter_ms'))}</td><td>{format_metric(run.get('cpu_percent'), 2)}</td>"
                f"<td>{format_metric(run.get('memory_mb'), 2)}</td></tr>")

    environment = result.get("environment") or {}
    limitations = "".join(f"<li>{escape(item)}</li>" for item in result.get("limitations") or [])
    header = (f"<h1>VSOA 实验报告 · {escape(job['id'])}</h1>"
              f"<p class='meta'>状态 {escape(result.get('status'))} · 完成 {completed}/{escape(result.get('planned_runs') or len(runs))} 轮 · "
              f"开始 {escape(result.get('test_start_time'))} · 结束 {escape(result.get('test_end_time'))}</p>"
              f"<p class='meta'>环境：{escape(environment.get('os'))} · Python {escape(environment.get('python_embedded'))} · "
              f"VSOA {escape(environment.get('vsoa'))} · {escape(environment.get('logical_cpus'))} 逻辑核心 · "
              f"{escape(environment.get('topology'))}</p>")
    summary_table = ("<h2>条件汇总</h2><table><thead><tr><th>条件</th><th>轮数</th><th>延迟均值 ms</th>"
                     "<th>P95 ms</th><th>P99 ms</th><th>吞吐 Mbps</th><th>原始丢失 %</th><th>CPU %</th><th>内存 MB</th>"
                     f"</tr></thead><tbody>{''.join(summary_row(item) for item in summaries) or '<tr><td colspan=9>暂无</td></tr>'}</tbody></table>")
    runs_table = ("<h2>全部轮次</h2><table><thead><tr><th>run_id</th><th>条件</th><th>轮</th><th>状态</th><th>payload B</th>"
                  "<th>速率 Hz</th><th>拓扑</th><th>延迟 ms</th><th>P95 ms</th><th>P99 ms</th><th>吞吐 Mbps</th>"
                  "<th>丢失 %</th><th>最终丢失 %</th><th>抖动 ms</th><th>CPU %</th><th>内存 MB</th>"
                  f"</tr></thead><tbody>{''.join(run_row(run) for run in runs)}</tbody></table>")
    limitations_block = f"<h2>测量范围与限制</h2><ul>{limitations}</ul>" if limitations else ""
    style = ("body{background:#0c1114;color:#e5eff1;font:13px 'Microsoft YaHei',sans-serif;margin:0;padding:30px}"
             "h1{font-size:22px;margin:0 0 6px}h2{font-size:15px;margin:26px 0 10px;color:#55d2cc}"
             ".meta{color:#91a7ae;margin:4px 0;font-size:12px}table{border-collapse:collapse;width:100%;font-size:12px}"
             "th,td{border:1px solid #2c3b40;padding:7px 9px;text-align:right}th:first-child,td:first-child{text-align:left}"
             "th{background:#151d20;color:#9bb2bd}tr:nth-child(even) td{background:#111a1f}li{color:#91a7ae;margin:4px 0}")
    return (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<title>VSOA 实验报告 {escape(job['id'])}</title><style>{style}</style></head>"
            f"<body>{header}{summary_table}{runs_table}{limitations_block}</body></html>")


def load_index():
    source = STATIC / "index.html"
    html = source.read_text(encoding="utf-8").replace("__SESSION_TOKEN__", TOKEN)
    return html.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "UnifiedBench/2.0"

    def log_message(self, *_args):
        return

    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self.headers.get("X-MQTT-Token") == TOKEN

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in {"", "/"} or path == "/index.html":
                self._html(INDEX_HTML)
            elif path == "/api/init":
                self._json(api_init())
            elif path == "/api/state":
                self._json(MANAGER.state())
            elif path == "/api/history":
                middleware = (parse_qs(parsed.query).get("middleware") or [""])[0]
                if middleware != "vsoa":
                    raise ValueError("当前控制台仅接入 VSOA")
                self._json(MANAGER.load_jobs())
            elif path == "/api/results":
                params = parse_qs(parsed.query)
                middleware = (params.get("middleware") or [""])[0]
                job_id = (params.get("job") or [""])[0]
                run_id = (params.get("run") or [None])[0]
                if middleware != "vsoa":
                    raise ValueError("当前控制台仅接入 VSOA")
                self._json(MANAGER.results(job_id, run_id))
            elif path.startswith("/files/"):
                self._files(path)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as error:
            self._json({"error": str(error)}, 400)

    def _files(self, path):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 4 or parts[1] != "vsoa" or parts[3] != "report.html":
            self._json({"error": "not found"}, 404)
            return
        job_id = parts[2]
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", job_id):
            self._json({"error": "not found"}, 404)
            return
        job = read_json_safe(MANAGER.job_dir(job_id) / "job.json")
        result = read_json_safe(MANAGER.job_dir(job_id) / "output" / "result.json")
        if not job or not result:
            self._json({"error": "not found"}, 404)
            return
        self._html(build_report(job, result).encode("utf-8"))

    def do_POST(self):
        if not self._authorized():
            self._json({"error": "会话令牌校验失败，请刷新页面"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/start":
                if body.get("middleware") != "vsoa":
                    raise ValueError("当前控制台仅接入 VSOA")
                self._json(MANAGER.start(body.get("template_index"), body.get("configuration") or {}, bool(body.get("matrix"))))
            elif self.path == "/api/stop":
                self._json(MANAGER.stop())
            elif self.path == "/api/shutdown":
                self._json({"ok": True})
                MANAGER.stop()
                threading.Thread(target=self.server.shutdown).start()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as error:
            self._json({"error": str(error)}, 400)


INDEX_HTML = None


def serve(host="127.0.0.1", port=8787, open_browser=True):
    global INDEX_HTML
    MANAGER.recover()
    INDEX_HTML = load_index()
    server = ThreadingHTTPServer((host, port), Handler)
    if open_browser:
        import webbrowser
        threading.Timer(.35, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    print(f"统一通信性能实验台: http://{host}:{port}/", flush=True)
    if VSOA_IMPORT_ERROR:
        print(f"警告：VSOA 引擎导入失败：{VSOA_IMPORT_ERROR}", flush=True)
    else:
        print(f"VSOA 已接入：{len(TEMPLATES)} 个标准条件（S01–S12）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.stop()
        server.server_close()


if __name__ == "__main__":
    serve()
