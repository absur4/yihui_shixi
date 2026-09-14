"""统一通信性能实验台：为 example.html 控制台提供 API，多中间件通用加载。

前端:     static/index.html（源自 example.html，token 注入）
适配器:    <middleware>/adapter.py（契约见仓库根 README.md）
执行器:    <middleware>/console_runner.py（由适配器 runner_command() 指定，子进程运行）
产物:      results/<middleware>/<job_id>/{job.json, spec.json, console.log, output/...}

控制台只依赖适配器的五个钩子：
    metadata() / catalog() / build_cases(template_index, configuration, matrix)
    / base_config()（可选）/ runner_command()
按固定顺序扫描 vsoa → dds → mqtt → zenoh（目录大小写均可）。
只注册存在 adapter.py 的目录；导入失败时注册为"入口缺失"并给出原因，不让控制台启动失败。
"""

from __future__ import annotations

import importlib.util
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

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interfaces.scenarios import SCENARIOS  # noqa: E402

SCENARIO_TITLES = {scenario.scenario_id: scenario.title for scenario in SCENARIOS}
MIDDLEWARE_ORDER = ("vsoa", "dds", "mqtt", "zenoh")
PRETTY_LABELS = {"vsoa": "VSOA", "dds": "DDS", "mqtt": "MQTT", "zenoh": "Zenoh"}
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


def _find_adapter_folder(name):
    """按 小写 / 原样 / 大写 / 首字母大写 查找含 adapter.py 的目录。"""
    for candidate in (name, name.lower(), name.upper(), name.capitalize()):
        folder = ROOT / candidate
        if (folder / "adapter.py").exists():
            return folder
    return None


def _import_adapter(name, folder):
    spec = importlib.util.spec_from_file_location(f"console_adapter_{name}", folder / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_backends():
    """扫描各中间件目录并注册适配器。

    条件目录与可用性无关：环境未就绪时也要能列出 S01–S12（执行时由执行器降级为 not_tested），
    这样"冒烟跑通"在缺少依赖/绑定/注入器时同样可验证，且不伪造任何数据。
    """
    registry: dict[str, dict] = {}
    for name in MIDDLEWARE_ORDER:
        folder = _find_adapter_folder(name)
        if folder is None:
            continue
        module, meta, templates, config = None, None, [], None
        try:
            module = _import_adapter(name, folder)
            meta = module.metadata() if hasattr(module, "metadata") else module.create_adapter().metadata()
        except Exception as error:
            print(f"警告：{name} 适配器加载失败：{type(error).__name__}: {error}", flush=True)
            module, meta = None, None
        if module is not None:
            try:
                templates = list(module.catalog())
                if hasattr(module, "base_config"):
                    config = module.base_config()
            except Exception as error:
                print(f"警告：{name} 条件目录读取失败：{type(error).__name__}: {error}", flush=True)
        if meta is None:
            meta = {"id": name, "name": name, "label": PRETTY_LABELS.get(name, name.upper()),
                    "available": False, "version": None,
                    "transport_options": [], "qos_options": [],
                    "notes": [f"{name}/adapter.py 加载失败，请查看控制台启动日志。"]}
        middleware_id = meta.get("id") or getattr(module, "MIDDLEWARE_ID", None) or name
        registry[middleware_id] = {"module": module, "meta": meta, "folder": folder,
                                   "templates": templates, "config": config}
    return registry


BACKENDS = load_backends()


def map_run(run):
    """把引擎单轮报告映射为控制台字段（保留全部原始字段供详情查看）。"""
    configuration = dict(run.get("configuration") or {})
    configuration.setdefault("case", run.get("scenario_title") or run.get("scenario_name"))
    mapped = dict(run)
    mapped.update({
        "unique_deliveries": run.get("unique_deliveries", run.get("messages_received")),
        "achieved_publish_rate_hz": run.get("achieved_publish_rate_hz", run.get("achieved_publish_rate_hz_per_publisher")),
        "latency_sample_count": run.get("latency_sample_count")
                                or ((run.get("statistics") or {}).get("latency_ms") or {}).get("count"),
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
    """单活动作业管理：启动、跟随日志、停止、查询结果与历史（多中间件通用）。"""

    def __init__(self, backends):
        self.backends = backends
        self.lock = threading.Lock()
        self.job = None
        self.process = None
        self.logs: list[dict] = []
        self.stopping = False

    def job_dir(self, middleware_id, job_id):
        return RESULTS / str(middleware_id) / str(job_id)

    def _write_job(self, job):
        path = self.job_dir(job["middleware_id"], job["id"]) / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("job.json.tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def load_jobs(self, middleware_id):
        records = []
        base = RESULTS / str(middleware_id)
        if base.exists():
            for job_file in sorted(base.glob("*/job.json")):
                data = read_json_safe(job_file, attempts=1)
                if data and "id" in data:
                    records.append(data)
        records.sort(key=lambda item: item.get("started") or 0, reverse=True)
        return records

    def recover(self):
        """服务重启后，把残留的运行中状态标记为已中断。"""
        if not RESULTS.exists():
            return
        for job_file in RESULTS.glob("*/*/job.json"):
            data = read_json_safe(job_file, attempts=1)
            if data and data.get("status") in ACTIVE_STATES:
                data["status"] = "interrupted"
                data["ended"] = data.get("ended") or time.time()
                temporary = job_file.with_name("job.json.tmp")
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, job_file)

    def start(self, middleware_id, template_index, configuration, matrix):
        with self.lock:
            if self.job and self.job["status"] in ACTIVE_STATES:
                raise ValueError("已有一个实验正在运行，请先停止或等待其完成")
        entry = self.backends.get(middleware_id)
        if entry is None:
            raise ValueError(f"未知中间件：{middleware_id}（已接入：{', '.join(self.backends) or '无'}）")
        label = entry["meta"].get("label") or middleware_id
        if entry["module"] is None:
            raise ValueError(f"{label} 适配器加载失败，无法启动测试（详见控制台启动日志）")
        if not entry["templates"]:
            raise ValueError(f"{label} 没有可用条件，无法启动测试")
        # available=False 仍允许启动：执行器会按契约返回 not_tested 并写明原因（用于冒烟验证，不伪造数据）
        cases = list(entry["module"].build_cases(template_index, configuration, matrix))  # ValueError → 400
        if not cases:
            raise ValueError("没有可执行的条件")
        try:
            index = int(template_index)
            template = entry["templates"][index]
        except (TypeError, ValueError, IndexError):
            template = {"scenario_name": "", "case": ""}
        scenario_id = template.get("scenario_name", "")
        total = sum(int(case.get("case_repeats") or case.get("repeats") or 1) for case in cases)
        plan = [{"scenario_id": case.get("scenario_id"), "scenario_name": case.get("scenario_name"),
                 "scenario_title": case.get("scenario_title"), "phase": case.get("phase"),
                 "status": "planned", "reason": None, "configuration": case,
                 "planned_repeats": int(case.get("case_repeats") or 1)} for case in cases]
        job_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        job_dir = self.job_dir(middleware_id, job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        output = job_dir / "output"
        job = {"id": job_id, "middleware_id": middleware_id, "status": "starting",
               "started": time.time(), "ended": None, "total": total, "matrix": bool(matrix),
               "scenario": scenario_id, "case": template.get("case", ""), "template_index": template_index,
               "configuration": configuration}
        spec = {"middleware": middleware_id, "config": entry["config"] or {}, "cases": cases, "plan": plan,
                "output": str(output), "logs": str(output / "logs")}
        (job_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._write_job(job)
        command = list(entry["module"].runner_command()) + [str(job_dir / "spec.json")]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=flags, cwd=str(entry["folder"]),
                                   env=dict(os.environ, PYTHONUTF8="1"))
        with self.lock:
            self.job = job
            self.process = process
            self.stopping = False
            degraded = "" if entry["meta"].get("available") else "｜环境未就绪，预期结果为 not_tested（不伪造数据）"
            self.logs = [{"time": datetime.now().strftime("%H:%M:%S"),
                          "text": f"控制台已启动 {label} 引擎：{template.get('case', '')}"
                                  + ("（完整矩阵）" if matrix else "（当前条件）") + degraded}]
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
        if not samples:
            # 兼容未写 artifacts 样本文件的适配器：直接使用单轮结果里的 samples 字段
            for item in run.get("samples") or []:
                if len(samples) >= SAMPLE_LIMIT:
                    break
                if not isinstance(item, dict):
                    continue
                latency = item.get("latency", item.get("latency_ms"))
                if latency is None:
                    continue
                samples.append({"sequence": item.get("sequence", len(samples)),
                                "latency": round(float(latency), 4)})
        link = None
        for item in run.get("link_metrics") or []:
            publisher = item.get("publisher", item.get("publisher_id"))
            subscriber = item.get("subscriber", item.get("subscriber_id"))
            if publisher == 0 and subscriber == 0:
                latency = item.get("latency_ms")
                link = {"latency_p95_ms": latency.get("p95") if isinstance(latency, dict) else latency}
                break
        if link is None:
            link = {"latency_p95_ms": run.get("latency_p95_ms")}
        return samples, link

    def _run_documents(self, job_dir, result):
        """逐轮结果列表：先取套件结果的 runs，再补上 output/runs/ 里已落盘但还没进套件结果的轮次。

        部分引擎（如 Zenoh）只在全部轮次跑完后才写 output/result.json，只读它会导致
        控制台直到实验结束才有数据。逐轮文件是根 README §8 的契约产物，这里统一按轮读取，
        每轮结束即可显示该轮结果与曲线，不依赖各引擎的写入时机。
        """
        documents, seen = [], set()
        for run in (result or {}).get("runs") or []:
            key = run.get("run_id") or run.get("condition_id_repeat_key")
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            documents.append(run)
        extra = []
        for path in (job_dir / "output" / "runs").glob("*.json"):
            run = read_json_safe(path)
            if not isinstance(run, dict):
                continue
            key = run.get("run_id") or path.stem
            if key in seen:
                continue
            seen.add(key)
            extra.append((path.stat().st_mtime, run))
        extra.sort(key=lambda item: item[0])
        documents.extend(run for _, run in extra)
        return documents

    def results(self, middleware_id, job_id, run_id=None):
        job_dir = self.job_dir(middleware_id, job_id)
        if not (job_dir / "job.json").exists():
            raise ValueError("找不到该实验记录")
        result = read_json_safe(job_dir / "output" / "result.json")
        engine_runs = self._run_documents(job_dir, result)
        runs, mapped, samples, link = [], None, [], None
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


MANAGER = JobManager(BACKENDS)


def api_init():
    default_middleware = next(iter(BACKENDS), None)
    history = ([{"id": job["id"], "status": job.get("status")}
                for job in MANAGER.load_jobs(default_middleware)] if default_middleware else [])
    return {"middleware": [entry["meta"] for entry in BACKENDS.values()],
            "catalogs": {middleware_id: entry["templates"] for middleware_id, entry in BACKENDS.items()},
            "names": SCENARIO_TITLES,
            "history": history}


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
    label = (BACKENDS.get(job.get("middleware_id"), {}).get("meta", {}) or {}).get("label") or job.get("middleware_id", "")
    environment = result.get("environment") or {}

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

    limitations = "".join(f"<li>{escape(item)}</li>" for item in result.get("limitations") or [])
    header = (f"<h1>{escape(label)} 实验报告 · {escape(job['id'])}</h1>"
              f"<p class='meta'>状态 {escape(result.get('status'))} · 完成 {completed}/{escape(result.get('planned_runs') or len(runs))} 轮 · "
              f"开始 {escape(result.get('test_start_time'))} · 结束 {escape(result.get('test_end_time'))}</p>"
              f"<p class='meta'>环境：{escape(environment.get('os'))} · Python {escape(environment.get('python_version') or environment.get('python_embedded'))} · "
              f"{escape(label)} {escape(environment.get('version') or environment.get('vsoa') or environment.get('middleware_version'))} · "
              f"{escape(environment.get('logical_cpus') or environment.get('logical_cpu_count'))} 逻辑核心 · "
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
            f"<title>{escape(label)} 实验报告 {escape(job['id'])}</title><style>{style}</style></head>"
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
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
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

    def _middleware_param(self, query):
        middleware = (query.get("middleware") or [""])[0]
        if middleware not in BACKENDS:
            raise ValueError(f"未知中间件：{middleware or '（空）'}（已接入：{', '.join(BACKENDS) or '无'}）")
        return middleware

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
                self._json(MANAGER.load_jobs(self._middleware_param(parse_qs(parsed.query))))
            elif path == "/api/results":
                params = parse_qs(parsed.query)
                middleware = self._middleware_param(params)
                job_id = (params.get("job") or [""])[0]
                run_id = (params.get("run") or [None])[0]
                self._json(MANAGER.results(middleware, job_id, run_id))
            elif path.startswith("/files/"):
                self._files(path)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as error:
            self._json({"error": str(error)}, 400)

    def _files(self, path):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 4 or parts[3] != "report.html" or parts[1] not in BACKENDS:
            self._json({"error": "not found"}, 404)
            return
        middleware, job_id = parts[1], parts[2]
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", job_id):
            self._json({"error": "not found"}, 404)
            return
        job = read_json_safe(MANAGER.job_dir(middleware, job_id) / "job.json")
        result = read_json_safe(MANAGER.job_dir(middleware, job_id) / "output" / "result.json")
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
                middleware = str(body.get("middleware", ""))
                if middleware not in BACKENDS:
                    raise ValueError(f"未知中间件：{middleware or '（空）'}（已接入：{', '.join(BACKENDS) or '无'}）")
                self._json(MANAGER.start(middleware, body.get("template_index"),
                                         body.get("configuration") or {}, bool(body.get("matrix"))))
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
    for middleware_id, entry in BACKENDS.items():
        state_text = f"{len(entry['templates'])} 个标准条件" if entry["meta"].get("available") else "不可用（入口缺失）"
        print(f"  已加载 {middleware_id}: {entry['meta'].get('label')} · {state_text}", flush=True)
    if not BACKENDS:
        print("  警告：没有找到任何 <目录>/adapter.py，控制台没有可测中间件。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.stop()
        server.server_close()


if __name__ == "__main__":
    serve()
