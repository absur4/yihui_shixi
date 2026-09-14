# MQTT 模块化接入改造要求

> 读者：负责 `mqtt/` 的实现者与其 AI。
> **场景与指标要求以《四种通信中间件统一测试需求文档》1.0（仓库根 PDF）为准；文件与接口要求以仓库根 `README.md` 为准。**
> 最终目标：让 MQTT 成为一个**可以被 `songfei/` 控制台"即插即用"的模块**——控制台不改一行代码，仅凭你目录内的契约文件就能列出 MQTT、展开 S01–S12 条件、启动真实测量、收集结果并在同一套页面展示。
>
> 参考实现：`VSOA/`（引擎 `vsoa_py/standalone` + 控制台接入方式 `songfei/app.py`、`songfei/vsoa_runner.py`）。
> 现状评价：**引擎与指标公式的合规度已经很高**（见第 4 节），问题集中在"接入层"和"字段命名"。

---

## 0. 完成标准（先看这一节，全部勾上才算交付）

- [ ] `mqtt/adapter.py` 是**唯一**适配器入口（不再有第二份同内容文件）；能在任意解释器 import 成功；实现控制台的五个钩子：`metadata()`、`catalog()`、`build_cases(index, configuration, matrix)`、`runner_command()`（+ 可选 `base_config()`）。
- [ ] `mqtt/console_runner.py` 支持 `python mqtt/console_runner.py <job>/spec.json`，**按重复次数跑满**，日志为 `[HH:MM:SS]` 前缀，结束打印 `RESULT … status=…`。
- [ ] **冒烟跑通即可，不需要完整跑一遍测试**：任选 1 个条件、跑 1 轮（`case_repeats=1`），能产出 `output/result.json` + `output/runs/<run_id>.json` + 样本文件（`artifacts/<run_id>/subscriber-0.result.json`）即算通过。**不要求跑 S01–S12 全量矩阵，也不要求跑满 5 次重复**；全量正式测试由控制台统一发起。缺依赖时返回 `status="not_tested"` + `limitations`、指标全 `null`，同样算跑通（前提是接口链路与降级路径都已实现）。
- [ ] **更新 `mqtt/README.md`：写一份接口清单，每个接口只写"名称 + 一句话功能"**（不需要签名、字段表、示例），允许全部接口集中写在 `README.md` 一个文件里（见第 8 节）。
- [ ] `catalog()` 返回 S01–S12 全部场景条件；`link_metrics` 用 `publisher`/`subscriber` 命名。
- [ ] 依赖与前置条件可被探测：`metadata().available` 为真实值；缺依赖时给中文修复提示，不抛异常。
- [ ] `git status` 只显示 `mqtt/` 下的改动。
- [ ] 提交前清理 `__pycache__/`、`outputs/`、`ui_runs/`、日志与备份。

---

## 1. 控制台会这样调用你（这是"模块化"的全部含义）

```
控制台启动
  └─ import mqtt/adapter.py  →  create_adapter()
        ├─ metadata()                          → /api/init 的 middleware 项（含真实 available）
        ├─ catalog()                           → /api/init 的 catalogs["mqtt"]（S01–S12 全部条件）
        ├─ build_cases(index, config, matrix)   → 把"当前表单参数"或"完整矩阵"展开成引擎可执行条件
        └─ runner_command()                     → 启动执行器的命令行（含解释器选择）

用户点"开始测试"
  └─ 控制台写入 spec.json
       └─ 子进程：python mqtt/console_runner.py <job_dir>/spec.json
             ├─ 写 output/runs/<run_id>.json（每轮跑完后立即写）
             ├─ 原子更新 output/result.json（每轮结束刷新一次）
             └─ stdout: [HH:MM:SS] … ；结束打印 RESULT … status=…

控制台轮询 output/result.json → /api/results → 页面指标卡 / 曲线 / 报告
```

要点：**控制台只认字段名，不认你的库**。你内部用 paho / broker / 多进程都可以，但对外必须是本节这套形状。

---

## 2. 必须交付的文件与目录（根 README §3）

```
mqtt/
├─ adapter.py            ← 必须：唯一契约层入口（零第三方硬依赖，可 import）
├─ console_runner.py     ← 必须：子进程执行器
├─ README.md             ← 必须：依赖 + 启动命令 + S01–S12 能力矩阵 + 限制 + 一次真实测试结果
├─ requirements.txt      ← 必须（当前缺失，只有 requirements-github.txt）
├─ mqtt_*.py             ← 你的引擎（保持现状；`mqtt_repo_adapter.py` 应删除，见 5.2）
├─ config.yaml           ← 条件矩阵（按第 4、5 节调整）
└─ results/<job_id>/     ← 你的作业产物
   （控制台归档）songfei/results/mqtt/<job_id>/{job.json, spec.json, console.log, output/...}
```

每个作业目录至少包含：`job.json`、`spec.json`、`console.log`、`output/result.json`、`output/runs/<run_id>.json`、`output/artifacts/<run_id>/`。

---

## 3. 接口要求（根 README §4 / §5 / §6 / §8）

### 3.1 `adapter.py` 必须实现（根 README §4）

```python
# —— 控制台实际调用的钩子（模块级函数，必需）——
MIDDLEWARE_ID = "mqtt"

def metadata() -> dict: ...        # 见 3.3
def catalog() -> list[dict]: ...   # 见 3.4
def build_cases(template_index, configuration, matrix) -> list[dict]: ...   # 必需，语义见下方说明
def base_config() -> dict: ...     # 可选：写入 spec.json 的 config
def runner_command() -> list[str]: ...   # 必需：如 [sys.executable, str(HERE / "console_runner.py")]

# —— 兼容根 README §4 的适配器形式（控制台两种写法都支持）——
from interfaces import MiddlewareAdapter, ScenarioResult, ScenarioSpec

class Adapter(MiddlewareAdapter):
    name = "mqtt"                      # 必须等于 middleware_id 与目录归档名
    def metadata(self) -> dict: return metadata()
    def catalog(self) -> list[dict]: return catalog()
    def build_cases(self, template_index, configuration, matrix): return build_cases(template_index, configuration, matrix)
    def run(self, scenario: ScenarioSpec, parameters: dict) -> ScenarioResult: ...   # 单轮直调（可选）

def create_adapter() -> Adapter:
    return Adapter()
```

**`build_cases()` 语义**（控制台点"开始测试"时调用，"当前条件"与"完整矩阵"都靠它）：

| 参数 | 说明 |
|---|---|
| `template_index` | int，指向 `catalog()` 返回列表的下标 |
| `configuration` | dict，前端表单的输入字段（`repeats` = 重复轮数、`network_loss_rate` 为 0–1 小数、`qos_profile` 取 `qos0/1/2`） |
| `matrix` | bool。`True` = **忽略 configuration**，返回该场景全部标准条件；`False` = 用 configuration 覆盖所选标准条件，返回 1 个条件 |

- 返回的每个 case 必须带 `case_repeats`（本轮重复次数），并保留引擎所需的全部字段（含 `rate_search`、`qos_profile` 等）。
- 参数非法时 `raise ValueError("中文消息")`，控制台会把这条消息原样显示给用户。
- `runner_command()` 返回的命令行会被控制台追加 `spec.json` 路径后用 `subprocess` 启动，工作目录是你的模块目录；需要专用解释器（如 venv）就在这里指定。

- `interfaces` 从仓库根导入；场景 ID 与标题的**唯一来源是 `interfaces/scenarios.py`**，不要自建 `SCENARIO_TITLES`。
- `run()` 返回 `ScenarioResult`（不是裸 dict）；批量/矩阵由 `console_runner.py` 负责（3.5 节），两者字段必须一致。

### 3.2 命名规则（根 README §2）

| 项 | 规定值 |
|---|---|
| `middleware_id` / `Adapter.name` / 归档目录名 | `mqtt`（小写） |
| 页面显示名 `label` | `MQTT` |
| 库与 broker 版本 | 放 `metadata().version` 与结果 `environment`（如 `paho_version`、`broker_version`） |
| broker / topic / QoS 差异 | 只能体现在 `configuration`、`qos_profile`、`notes` 里，**不得改变公共字段名** |

### 3.3 `metadata()` 字段（全部必填）

| 字段 | 说明 |
|---|---|
| `id` / `name` | `"mqtt"` |
| `label` | `"MQTT"` |
| `version` | 真实版本（现在是硬编码 `'2.0.0'`，应改为读取 `paho.mqtt.__version__` 或你的 `VERSION` 常量并注明） |
| `available` | **必须真实探测**（见 5.4），不得硬编码 `True` |
| `transport_options` | `["tcp"]` |
| `qos_options` | `qos0` / `qos1` / `qos2`（保持现在的三项标签） |
| `notes` | 能力与限制；**必须包含本机缺乏依赖时的中文修复提示**（例：`"缺少 paho-mqtt：pip install -r mqtt/requirements.txt"`、`"未找到 broker：请配置 mqtt/broker/mosquitto.exe"`） |

### 3.4 `catalog()` 返回字段（根 README §5，全部必填）

```text
scenario_name, case, condition_id, title
payload_size_bytes, publish_rate_hz, publisher_count, subscriber_count
message_count, duration_seconds, repeats, random_seed
warmup_seconds, drain_seconds, timeout_seconds
network_delay_ms, network_jitter_ms, network_loss_rate, network_profile
transport_mode, qos_profile
```

- `scenario_name` 必须是 `S01`…`S12`（现状已符合）。
- `title` 建议取 `interfaces/scenarios.py` 的中文标题；`condition_id` 现状是一长串拼接，建议缩短为 `<case>` 或 `<Sxx>_<case>` 以便页面显示。
- 全部数值必须是 JSON number；`network_loss_rate` 为 0–1 小数；`publish_rate_hz = 0` 表示不限速。

### 3.5 `console_runner.py` 协议（根 README §8）

调用：`python mqtt/console_runner.py <job_dir>/spec.json`

spec.json：

```json
{
  "middleware": "mqtt",
  "config":  { "…引擎全局配置…" },
  "cases":   [ { "scenario_id": "S01", "scenario_name": "S01_1KiB_100Hz",
                 "scenario_title": "1 KiB @ 100 Hz point-to-point latency",
                 "case_repeats": 5,
                 "payload_size_bytes": 1024, "publish_rate_hz": 100,
                 "publisher_count": 1, "subscriber_count": 1,
                 "message_count": 10000, "duration_seconds": 100.0,
                 "warmup_seconds": 1, "drain_seconds": 0.5,
                 "transport_mode": "tcp", "qos_profile": "qos1",
                 "network_profile": "baseline", "random_seed": 42 } ],
  "plan":    [ { "scenario_id": "S01", "scenario_name": "S01_1KiB_100Hz", "planned_repeats": 5 } ],
  "output":  "…/songfei/results/mqtt/<job_id>/output",
  "logs":    "…/songfei/results/mqtt/<job_id>/output/logs"
}
```

必须做到：

1. **重复次数**：优先 `case["case_repeats"]`，其次 `case["repeats"]`，默认 1；**每个条件跑满重复次数**（PDF §6.7：至少 5 次有效重复）。
2. **stdout**：每行 `[HH:MM:SS] 消息`；每轮 `START <case> repeat=i/N`、`END <case> status=… sent=… received=…`。
3. **结束行**：`[HH:MM:SS] RESULT <output>/result.json status=completed|error|cancelled`。
4. **退出码**：全部完成 `0`；被取消 `130`；其他 `1`。
5. **`plan` 与 `logs` 必须使用**：`plan` 里的 `planned_repeats` 用来校验/记录；worker 日志写进 `logs` 目录。
6. **`console.log` 必须真的写**：现在是先 `write_text('')` 清空、之后从不写入 → 必须把 stdout 内容同样追加进去（控制台历史回看依赖它）。
7. **产物**：`output/result.json`（套件级：`status/planned_runs/test_start_time/test_end_time/environment/configuration/scenario_summaries/runs/limitations`）+ 每轮 `output/runs/<run_id>.json` + `output/artifacts/<run_id>/`。
8. **原子写**：临时文件 + `os.replace`（你已有 `write_json`，复用它）。
9. **进程自清理**：被终止时确保 broker、端点、relay 全部退出。

### 3.6 单轮结果字段（根 README §6，缺测写 `null`）

```text
run_id, middleware_id, middleware_version, scenario_name, scenario_title, repeat, status
payload_size_bytes, publish_rate_hz, publisher_count, subscriber_count
messages_sent, messages_received, unique_deliveries, expected_deliveries
achieved_publish_rate_hz
latency_ms, latency_p95_ms, latency_p99_ms, latency_std_ms, throughput_mbps, jitter_ms
packet_loss, final_packet_loss
duplicate_count, out_of_order_count, corrupted_count
startup_time_ms, discovery_time_ms, recovery_time_ms
cpu_percent, memory_mb, latency_sample_count
configuration, statistics, link_metrics, environment, limitations
```

现状对照（**大部分已满足**，只需补最后一列）：

| 字段 | 现状 | 结论 |
|---|---|---|
| `latency_ms/p95/p99/std`、`jitter_ms` | `mqtt_metrics.latency_metrics()`：`h=(n-1)p` 线性插值 + `pstdev` + 按链路相邻差均值 | ✅ 符合 PDF §4.2/§4.3 |
| `throughput_mbps`、`offered_throughput_mbps` | 唯一交付 / 发送侧，均只算 payload | ✅ 符合 PDF §4.4 |
| `cpu_percent`、`memory_mb` | 含 broker、不含 harness；RSS 峰值 | ✅ 符合 PDF §4.5 |
| `packet_loss`、`final_packet_loss`、`duplicate/out_of_order/corrupted_count` | `analyze()` 真实统计 | ✅ 符合 PDF §4.6 |
| `startup_time_ms`、`discovery_time_ms` | `harness→all_ready`、`all_ready−min(conn_start)` | ✅ 符合 PDF §4.7 |
| `latency_sample_count` | 由 `latency_metrics()` 返回 | ✅ |
| `middleware_id` / `middleware_version` | `_map_result` 映射 | ✅ |
| `statistics` | 结构为 `{sample_level_metrics, formulas,…}` | ❌ 需改为按指标的分布（见 5.3） |
| `link_metrics` | 直接用 `delivery_matrix`（`publisher_id/subscriber_id`） | ❌ 需改名并补延迟统计（见 5.1） |
| 图表样本 | 只有 `raw_files`（CSV 路径） | ❌ 需产出 `latencies_ms`（见 5.5） |

### 3.7 图表样本（前端延迟曲线依赖，务必提供）

控制台当前读取方式（与 VSOA 一致）：

```
output/artifacts/<run_id>/subscriber-0.result.json
  { "latencies_ms": [0.71, 0.62, 0.49, …] }     # 单位 ms，真实接收样本，按到达/序列顺序
```

你的原始数据在 `output/runs/<run_id>/subscriber-N.csv`（`sequence_id, send_ns, receive_ns, phase, validation, …`）里**已经存在**，只需在每轮结束时多导出这一份 JSON（用 `phase=original 且 validation 通过` 的记录，`(receive_ns-send_ns)/1e6`）。**不得插值、不得随机生成。**

---

## 4. 场景要求（PDF §3 / §4 / §6）

| 场景 | PDF 要求 | MQTT 现状 | 需要做的 |
|---|---|---|---|
| S01 | 1P/1S、1 KiB、100/1000 Hz | ✅ | 保持 |
| S02 | 固定拓扑/速率/QoS，遍历 1K–1M 共 8 档 | ⚠️ 多出 `4194304`（4 MiB），PDF 推荐矩阵上限 1 MiB | **删除 4 MiB 条件**（或单列为 `S02_extra` 并在结果注明"不参与跨中间件比较"），使 8 档与 VSOA/DDS 一致 |
| S03 | 64K/256K/1M，用**最大稳定速率** | ⚠️ 条件存在，但"最大稳定速率"搜索只在 CLI 生效 | 把 `rate_search` 校准抽成函数并在接入路径调用（见 5.6） |
| S04 | 100/1000/5000 Hz + 最大稳定速率 | ⚠️ 同上 | 同上 |
| S05/S06/S07 | 1P4S / 4P1S / 4P4S | ✅ | 保持（注意"每发布者速率"口径不要变，见 5.7） |
| S08 | ≥5 分钟，观察延迟漂移与内存增长 | ✅ 300 s + `long_run_trends` | 保持 |
| S09 | 1%/5%/10% 丢包 + 延迟抖动 | ✅ 4 档；非零损伤返回 `not_tested` + `network_injection` | 保持（不要为了"有数据"去伪造注入） |
| S10 | 冷/热启动、多端点发现、超时 | ⚠️ 6 项齐全，但负向用例会让整场判失败 | 修 suite 状态判定（见 5.6） |
| S11 | 进程/Broker/Router/网络故障后恢复 | ✅ 5 种（含 broker 重启、TCP relay 断网/断连） | 保持 |
| S12 | 长度、校验和、缺失、重复、乱序 | ✅ 3 种 QoS | 保持 |
| 通用 | 每条件 ≥5 次有效重复；消息含序列号/时间戳/长度/校验和 | 信封 ✅；重复在接入路径丢失 | runner 循环重复次数 |

---

## 5. 现状差距 → 逐条改法（接口与逻辑）

### 5.1 `link_metrics` 命名与内容

现状：`_map_result` 里 `link_metrics=result.get('delivery_matrix', [])`，元素形如
`{publisher_id, subscriber_id, messages_sent, unique_deliveries, final_unique_deliveries, duplicate_count, out_of_order_count, late_native_deliveries}`。

改为（根 README §6：publisher/subscriber 从 0 开始，可对照 VSOA 的 `link_metrics`）：

```python
link_metrics = [{
    "publisher": row["publisher_id"], "subscriber": row["subscriber_id"],
    "messages_sent": row["messages_sent"],
    "messages_received": row["unique_deliveries"],
    "messages_received_after_recovery": row["final_unique_deliveries"],
    "duplicate_count": row["duplicate_count"], "out_of_order_count": row["out_of_order_count"],
    "packet_loss": row["missing_count"] / row["messages_sent"] if row["messages_sent"] else None,
    "final_packet_loss": row["final_missing_count"] / row["messages_sent"] if row["messages_sent"] else None,
    "latency_ms": <该链路的 describe 分布>,   # count/min/mean/p50/p95/p99/max/std
} for row in result["delivery_matrix"]]
```

（`describe()` 已在 `mqtt_metrics.py` 里，逐链路调用即可。）

### 5.2 删除重复入口

`adapter.py` 与 `mqtt_repo_adapter.py` 目前**完全相同（SHA256 一致）**，而 `console_runner.py` 引用的是后者。

- 删除 `mqtt_repo_adapter.py`；`console_runner.py` 改为 `from adapter import Adapter, _map_result`。
- `adapter.py` 顶部的路径处理改为把**本目录**加入 `sys.path`（现在插入的是不存在的 `HERE/'package'`）：

```python
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:          # 关键：控制台按文件路径 import 时，本目录不在 sys.path 上
    sys.path.insert(0, str(HERE))
```

否则控制台用 `importlib` 按路径加载 `adapter.py` 时会 `ModuleNotFoundError: mqtt_adapter`。

### 5.3 `statistics` 结构

现状：`statistics={sample_level_metrics:{…}, formulas:{…}, sample_level:…, repeat_level:…}`，而控制台按 `statistics.latency_ms.count` 取值（与 VSOA 一致）。

改为：

```python
"statistics": {
    "latency_ms": <describe(all latencies)>,
    "jitter_ms":  <describe(all jitter diffs)>,
    "cpu_percent": <describe(cpu samples)>,
    "memory_mb": <describe(rss samples)>,
    "discovery_time_ms": <describe(discovery samples)>,
}
```

（`formulas` 等说明性内容可保留为额外键，但上面这几个分布键必须有。）

### 5.4 `metadata().available` 必须真实探测

```python
def _probe():
    problems = []
    if importlib.util.find_spec("paho") is None:
        problems.append("缺少 paho-mqtt：pip install -r mqtt/requirements.txt")
    broker = Path(broker_executable)           # 例如 broker/mosquitto.exe
    if not (broker if broker.is_absolute() else HERE / broker).exists():
        problems.append("未找到 MQTT broker（mqtt/broker/mosquitto.exe）")
    return problems
```

`available = not problems`；`notes` 里带上 `problems` 的中文说明。这样控制台显示"入口缺失"并告诉使用者怎么修，而不是"可用但一点就报错"。

### 5.5 产出图表样本（必做）

在每轮结束（`execute()` 的 `finally` 中、写完 run 结果之后）导出：

```python
artifacts = Path(output) / "artifacts" / run_id
artifacts.mkdir(parents=True, exist_ok=True)
lats = [ (rec["receive_ns"] - rec["send_ns"]) / 1e6
         for rec in read_csv(folder / "subscriber-0.csv")
         if rec["phase"] == 1 and rec["validation"] == "ok" ]
write_json(artifacts / "subscriber-0.result.json",
           {"latencies_ms": lats, "latency_sample_count": len(lats)})
```

并在单轮结果里补 `"samples_path": f"artifacts/{run_id}/subscriber-0.result.json"`。

### 5.6 三个逻辑缺陷（必须修）

**(a) 重复次数在接入路径丢失。** `console_runner.py` 现在是"每个 case 跑一次"：

```python
for case in cases:
    repeats = int(case.get("case_repeats") or case.get("repeats") or 1)
    for repeat in range(1, repeats + 1):
        merged = dict(config, **case, output_dir=output, repeat=repeat)
        print(f"[{now()}] START {case['scenario_name']} repeat={repeat}/{repeats}", flush=True)
        result = adapter.run(case, merged)
        result["repeat"] = repeat
        write_json(output / "runs" / f"{result['run_id']}.json", result)
        runs.append(result)
        write_json(output / "result.json", suite(runs))     # 每轮原子刷新
        print(f"[{now()}] END {case['scenario_name']} status={result['status']} "
              f"sent={result.get('messages_sent')} received={result.get('messages_received')}", flush=True)
```

**(b) S10 负向用例被误判为整场失败。** CLI 的 `main()` 有豁免，runner 没有：

```python
status = ("cancelled" if stopped else
          "completed" if runs and all(r["status"] == "completed" or r.get("expected_negative_outcome") for r in runs)
          else "error")
```

**(c) 取消时状态不一致。** 现在循环结束后批量把 `result['status']` 改成 suite 状态，但 per-run 文件已先写盘 → `result.json` 与 `runs/*.json` 不一致。改为：停止时**重写**已产生的 run 文件，或只在 suite 层标 `cancelled`、run 层保留真实状态。

### 5.7 `rate_search`（最大稳定速率）必须进接入路径

现状：校准循环（候选速率逐个试跑、选"首个失败前的最后一个通过速率"）只存在于 `mqtt_adapter.main()`；`execute()` 不处理 `rate_search` → 从控制台启动时，S03 的 3 个条件与 S04 的 `stable_rate` 条件会退化成"`publish_rate_hz=0` 不限速满速发送"，既没有 `rate_calibration`，`require_stable_rate` 也失效。

改法：抽成可复用函数并让两条路径共用。

```python
def calibrate(cfg, output, cache=None):
    """返回 (selected_rate_hz, trials)。逻辑与 main() 中的校准一致。"""
    ...

def execute(cfg, output, repeat=1, purpose="measurement"):
    if purpose == "measurement" and cfg.get("rate_search"):
        selected, trials = calibrate(cfg, output)
        cfg = dict(cfg, publish_rate_hz=selected or cfg["stable_rate_candidates"][0],
                   require_stable_rate=True)
        # 完成后把 rate_calibration 写进结果
    ...
```

并把 `rate_calibration={method, trials, selected_rate_hz, exact_global_maximum_claimed}` 写进单轮结果（与 CLI 路径一致）。

### 5.8 速率口径与跨中间件可比性（PDF §5）

MQTT 与 VSOA 都是**每发布者**速率（正确），但 DDS 用聚合口径。请在 `configuration` 与结果里显式写 `rate_scope: "per_publisher"`，便于报告层拒绝把不同口径放入同一比较组。

### 5.9 其他小项

| 项 | 现状 | 改成 |
|---|---|---|
| `run()` 返回类型 | `dict` | `ScenarioResult`（或在 `interfaces` 中约定统一为 dict 并注明） |
| `not_tested`/`unsupported` 早退 | `execute()` 在 `try` 之前 return，不写 `test_end_time`、无 artifacts | 早退也要写 `test_end_time`、`limitations`，并由 runner 落盘 run 文件 |
| 两套 runs 布局 | runner 写 `runs/<id>.json`，引擎写 `runs/<id>/result.json` | 统一为 `output/runs/<run_id>.json` + `output/artifacts/<run_id>/` |
| `job.json` | 无 | 由 runner 写（内容：`id/middleware_id/status/started/ended/total/matrix/case`） |
| 依赖声明 | 只有 `requirements-github.txt` | 新增 `requirements.txt`（paho-mqtt、PyYAML、psutil、jsonschema），并在 README 中引用 |

---

## 6. MQTT 专属：模块化与"即插即用"设计

MQTT 比 DDS 轻（纯 Python + 一个 broker 可执行文件），但**同样要按三层解耦**，否则控制台仍然会因为你本机缺依赖而崩：

```
mqtt/adapter.py         ← 契约层：顶层不得 import paho / broker，任何解释器可 import
mqtt/console_runner.py  ← 编排层：纯 Python，负责拉起 broker 与端点、落盘、打印日志
mqtt/mqtt_*.py          ← 引擎层：允许 paho 等依赖，惰性导入
```

三条硬规则：

1. **`adapter.py` 顶层不得出现 `import paho.mqtt.client`**（现在是通过 `from mqtt_adapter import execute…` 间接把 paho 拖进导入链，一旦本机没装 paho，控制台启动就会失败）。改为在 `run()` 内部惰性导入。
2. `available` 用 5.4 的真实探测；缺依赖时 `available=false` + `notes` 中文提示。
3. `runner_command()` 允许指定解释器（推荐，便于将来用独立 venv）：

```python
def runner_command():
    venv = FOLDER / ".venv" / "Scripts" / "python.exe"
    return [str(venv) if venv.exists() else sys.executable, str(FOLDER / "console_runner.py")]
```

优雅降级矩阵（与 DDS 一致，不得抛异常、不得伪造）：

| 环境状态 | `available` | `catalog()` | `run()` 结果 |
|---|---|---|---|
| 依赖齐全 | `true` | S01–S12 全部条件 | 真实测量 |
| 缺 paho / 缺 broker | `false` + `notes` 修复提示 | S01–S12 全部条件 | `status="not_tested"`，全指标 `null`，`limitations` 写明 |
| 能力缺口（如弱网注入） | 条件级 | 条件仍在目录里 | 条件级 `status="not_tested"` + `limitations` |

---

## 7. 自测验收（**冒烟级**：只需证明"跑通"，不需要全量测试）

> 目的：证明"接口链路通"，不是产出正式数据。正式的全量 S01–S12 测试（每条件 ≥5 次重复）由控制台统一发起，**不在你的交付范围内**。
> 执行时只需跑：第 1 步（契约层可导入）、第 2 步（单元测试）、第 3 步（**spec.json 里 `case_repeats` 写 1** 的冒烟作业）、第 4 步产物核对；第 5 步控制台联调同样是"任选一个条件能出数据、或明确给出 `not_tested`"即可，不必跑完整场景矩阵。

```powershell
# 1) 契约层可导入，且不因缺依赖而崩
python -c "import importlib.util as u; s=u.spec_from_file_location('a','mqtt/adapter.py'); m=u.module_from_spec(s); s.loader.exec_module(m); a=m.create_adapter(); print(a.metadata()); c=a.catalog(); print(len(c)); import collections; print(sorted({r['scenario_name'] for r in c}))"
#   期望：available 为真实布尔值；条件数 >0；场景集合 = {S01..S12}；不抛异常

# 2) 单元测试（当前因缺 paho 全部 ImportError）
python -m unittest discover -s mqtt/tests -v

# 3) 独立跑一次作业（spec.json 按 3.5 节构造；先装依赖并准备 broker）
python mqtt/console_runner.py <job>\spec.json
#   期望：stdout 每行 [HH:MM:SS]…；START/END 按重复次数出现；RESULT … status=…；退出码 0

# 4) 产物字段核对（对照 3.6 / 3.7）
#   output/result.json：status/planned_runs/environment/scenario_summaries/runs/limitations
#   output/runs/<run_id>.json：3.6 全字段；缺测为 null
#   output/artifacts/<run_id>/subscriber-0.result.json：含 latencies_ms
#   output/console.log 非空

# 5) 控制台联调
python songfei/run.py       # 下拉出现 MQTT；S01–S12 齐全；曲线有样本点

# 6) 边界
git status --short          # 只有 mqtt/ 下改动
```

---

## 8. 交付文档要求：更新 `mqtt/README.md`（只要接口清单）

改造完成后**更新 `mqtt/README.md`**。不需要写接口详细文档，**只要列出"有哪些接口 + 每个接口是干什么的"（名称 + 一句话功能）**即可。
允许把所有接口都写在 `README.md` 一个文件里（推荐）；也可拆到 `mqtt/INTERFACES.md`，但 `README.md` 里要给出链接。

### 8.1 `README.md` 需要包含的内容（保持简短）

1. 一句话说明本模块做什么 + `available = true` 的成立条件（paho 是否安装、broker 是否存在）
2. 冒烟验证命令（一条，能证明跑通）
3. **接口清单：名称 + 一句话功能**（主体，见 8.2）
4. S01–S12 能力矩阵（支持 / 部分支持 / `not_tested` + 原因，尤其 S09 弱网）
5. 已知限制 + "不伪造数据"声明

### 8.2 接口清单的写法（每行：名称 + 功能，各一句话）

| 接口 | 功能 |
|---|---|
| `create_adapter()` | 创建并返回本中间件的 Adapter 实例，供控制台注册 |
| `Adapter.name` | 中间件 ID，固定为 `mqtt` |
| `Adapter.metadata()` | 返回中间件元数据与可用性（含 paho / broker 真实探测结果） |
| `Adapter.catalog()` | 返回 S01–S12 全部标准条件，供控制台条件下拉框使用 |
| `build_cases(index, configuration, matrix)` | 把"当前表单参数"或"完整矩阵"展开成引擎可执行条件列表（控制台必需） |
| `base_config()` | 返回引擎全局配置（可选，写入 spec.json 的 config） |
| `Adapter.run(scenario, parameters)` | 执行单轮真实测量，返回统一格式的单轮结果 |
| `calibrate(cfg, output)` | 最大稳定速率校准（候选速率扫描），供 S03/S04 使用 |
| `runner_command()` | 返回启动执行器的命令行（含解释器选择） |
| `console_runner.py <spec.json>` | 子进程执行器入口：按 spec 逐条件逐轮执行并落盘 |
| `output/result.json` | 套件级结果汇总文件，控制台轮询读取 |
| `output/runs/<run_id>.json` | 单轮结果文件（统一字段） |
| `output/artifacts/<run_id>/subscriber-0.result.json` | 延迟样本文件，供前端延迟曲线使用 |
| `mqtt_adapter.py`（原生 CLI） | 原有命令行入口（`--config/--validate/--features/--resume` 等），供人工复现用 |

> 字段名、单位、状态枚举等以仓库根 `README.md` 与需求文档 PDF 为准，**不必在本文重复抄写**。

---

## 9. 禁止事项

1. 不得修改 `songfei/`、`interfaces/`（`interfaces/scenarios.py` 是场景唯一来源）。
2. 不得删场景、不得伪造/估算指标、不得把未测量写成 `0`。
3. 不得保留两份同内容的适配器文件（`adapter.py` 必须是唯一入口）。
4. 不得让 `adapter.py` 顶层依赖 paho / broker（会破坏"即插即用"）。
5. 不得把结果写到别的中间件目录或仓库根。
6. 提交前删除 `__pycache__/`、`outputs/`、`ui_runs/`、日志与备份文件。
