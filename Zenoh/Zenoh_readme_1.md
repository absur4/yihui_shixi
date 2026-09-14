# Zenoh 接入改造要求

> 读者：负责 `Zenoh/` 的实现者与其 AI。
> **场景与指标要求以《四种通信中间件统一测试需求文档》1.0（仓库根 PDF）为准；接口与字段要求以仓库根 `README.md` 为准。**
> 目标：让 Zenoh 成为一个能被 `songfei/` 控制台"即插即用"的模块——控制台不改一行代码，仅凭你目录内的契约文件就能列出 Zenoh、展开 S01–S12 条件、启动测量、展示结果。
> 参考实现（已通过控制台验收）：`VSOA/`、`DDS/`、`mqtt/` 三家的 `adapter.py` + `console_runner.py`。

---

## 0. 先区分：环境问题 vs 交付问题（关键）

| # | 缺的东西 | 性质 | 谁来装/谁来做 |
|---|---|---|---|
| 1 | `eclipse-zenoh`（`paho` 之于 MQTT 的位置）、`psutil`、`jsonschema` | **环境依赖**（pip 可装） | 由运行环境安装。按你 README：`pip install -r Zenoh/requirements.txt`（该文件目前也缺失，见 §4） |
| 2 | **`zenoh_bench`** | **你自己的引擎包，不是 pip 包**。你 README 写的是"适配器内部复用**仓库根目录**的 `zenoh_bench` 真实测量引擎"，但仓库里**没有这个包**（根目录、`Zenoh/` 下都没有，`importlib.util.find_spec('zenoh_bench')` 为 False） | **必须交付代码**，或明确声明它的安装来源 |
| 3 | 根 `pyproject.toml`（你 README 引用其依赖声明） | **交付缺失** | 仓库根没有该文件：要么补上，要么删掉引用 |

> 结论：**即使把 `eclipse-zenoh` 等依赖全部装好，`Zenoh/adapter.py` 仍然会在导入期失败**——因为顶层就 `from zenoh_bench.config import ...`。
> 所以「装环境」只能解决第 1 项，第 2/3 项是代码侧交付必须补的，此外还有 §5 列出的接口与逻辑问题。

**实测证据**（当前状态）：

```
$ python -c "import importlib.util as u; print(u.find_spec('zenoh_bench'))"
None
$ python -c "spec_from_file_location('z','Zenoh/adapter.py') ... exec_module"
ModuleNotFoundError: No module named 'zenoh_bench'

控制台 /api/init → zenoh: available=False, cases=0（无法列出任何条件）
控制台 /api/start zenoh → {"error": "Zenoh 适配器加载失败，无法启动测试"}
```

---

## 1. 控制台会这样调用你（"模块化"的全部含义）

```
控制台启动
  └─ import Zenoh/adapter.py（用主解释器，要求 import 成功）
        ├─ metadata()                          → /api/init 的 middleware 项（含真实 available）
        ├─ catalog()                           → /api/init 的 catalogs["zenoh"]（S01–S12 全部条件）
        ├─ build_cases(index, config, matrix)   → 把"当前表单参数"或"完整矩阵"展开成引擎可执行条件
        ├─ base_config()（可选）                → 写入 spec.json 的 config
        └─ runner_command()                     → 启动执行器的命令行（可指定专用解释器）

用户点"开始测试"
  └─ 控制台写 spec.json → 子进程：python Zenoh/console_runner.py <job_dir>/spec.json
        ├─ 逐条件逐轮执行，每轮写 output/runs/<run_id>.json 并原子刷新 output/result.json
        └─ stdout 每行 [HH:MM:SS] …，结束打印 RESULT <path> status=…（退出码 0/130/其他）
```

控制台**只认字段名**，不认你的库；你内部用 `zenoh_bench` 还是别的都可以，但对外必须是这套形状。

---

## 2. 完成标准（全部勾上才算交付）

- [ ] `Zenoh/adapter.py` **在主解释器、且没有安装任何依赖（含 eclipse-zenoh / zenoh_bench）时也能 import 成功**——这是"即插即用"的硬门槛：引擎必须**惰性导入**，探测失败只影响 `available` 与结果状态，不影响导入。
- [ ] 提供五个模块级钩子：`metadata()`、`catalog()`、`build_cases(index, configuration, matrix)`、`runner_command()`（+ 可选 `base_config()`），并保留 `create_adapter()` / `Adapter`。
- [ ] `catalog()` 在没有引擎时**仍能返回 S01–S12 全部条件**（不能依赖引擎里的 `PAYLOAD_SIZES` 之类的常量）。
- [ ] 控制台里出现 `Zenoh`：可用时"可用 + N 个条件"，不可用时"入口缺失 + 中文原因"，**任何情况都不抛异常**。
- [ ] **冒烟跑通**：任选 1 个条件跑 1 轮（`case_repeats=1`），产出 `output/result.json` + `output/runs/<run_id>.json` + 样本即可；**不需要跑 S01–S12 全量矩阵，也不需要跑满 5 轮**。环境/引擎未就绪时全部轮次 `not_tested`、指标 `null`、写明原因，**同样算跑通**。
- [ ] 更新 `Zenoh/README.md`：**接口清单（名称 + 一句话功能）**、能力矩阵、冒烟命令、已知限制（沿用现有格式即可，补齐缺失项）。
- [ ] `git status` 只显示 `Zenoh/` 下的改动（不得改 `songfei/`、`interfaces/`）。

---

## 3. 接口要求（根 README §4 / §5 / §6 / §8）

### 3.1 必须补的三个模块级钩子（现缺）

当前 `adapter.py` 只有 `Adapter.metadata/catalog/run`（类方法）+ 模块级 `create_adapter()`；
控制台是用**模块级函数**调用的，所以会在 `start()` 处 `AttributeError`。请补：

```python
MIDDLEWARE_ID = "zenoh"

def metadata() -> dict: ...                    # 见 3.2（可直接复用 Adapter.metadata 的实现）
def catalog() -> list[dict]: ...               # 见 3.3
def build_cases(template_index, configuration, matrix) -> list[dict]: ...   # 必需，语义见下表
def base_config() -> dict: ...                 # 可选：写入 spec.json 的 config
def runner_command() -> list[str]:             # 必需
    return [sys.executable, str(Path(__file__).resolve().parent / "console_runner.py")]

def create_adapter() -> Adapter:
    return Adapter()
```

**`build_cases()` 语义**：

| 参数 | 说明 |
|---|---|
| `template_index` | int，指向 `catalog()` 返回列表的下标 |
| `configuration` | dict，前端表单字段（`repeats` = 轮数、`network_loss_rate` 为 0–1 小数、`transport_mode` 小写） |
| `matrix` | bool。`True` = **忽略 configuration**，返回该场景全部标准条件；`False` = 用 configuration 覆盖所选条件，返回 1 个条件 |

- 返回的每个 case 必须带 **`case_repeats`**（本轮重复次数）以及引擎所需全部字段（含 `case`、`scenario_name`、`transport_mode`、`qos_profile`、`network_profile` 等）。
- 校验失败请 `raise ValueError("中文消息")`，控制台会原样显示给用户。

### 3.2 `metadata()` 字段（根 README §4，八项必填）

`id` / `name`（都必须是 `"zenoh"`）、`label`（`"Zenoh"`）、`version`（用 `importlib.metadata.version("eclipse-zenoh")`，取不到写 `null` 并说明）、`available`（**真实探测**）、`transport_options`、`qos_options`、`notes`（含缺依赖时的**中文修复提示**）。

### 3.3 `catalog()` 字段（根 README §5，21 项必填）

`scenario_name`(S01–S12) / `case` / `condition_id` / `title` / `payload_size_bytes` / `publish_rate_hz` / `publisher_count` / `subscriber_count` / `message_count` / `duration_seconds` / `repeats` / `random_seed` / `warmup_seconds` / `drain_seconds` / `timeout_seconds` / `network_delay_ms` / `network_jitter_ms` / `network_loss_rate` / `network_profile` / `transport_mode` / `qos_profile`。

你现有实现（`INPUT_FIELDS` + `_flat_configuration`）已经覆盖这 21 项 ✅，**唯一问题是 `catalog()` 依赖引擎的 `PAYLOAD_SIZES`**，导致引擎缺失时连目录都拿不到。改法：

```python
STANDARD_PAYLOAD_SIZES = (1024, 4096, 16384, 32768, 49152, 65536, 262144, 1048576)  # PDF §2 推荐矩阵

def _payload_sizes():
    """引擎可用时用引擎的档位，不可用时用 PDF 推荐矩阵，保证目录始终可读。"""
    try:
        return tuple(PAYLOAD_SIZES)
    except NameError:
        return STANDARD_PAYLOAD_SIZES
```

（同理 `METRICS_VERSION` 也要有常量兜底，例如 `METRICS_VERSION = locals().get("METRICS_VERSION", "1.0")` 或显式 `"1.0"`。）

### 3.4 `console_runner.py` 协议（根 README §8）

- 读 spec：`config` / `cases` / `plan` / `output` / `logs`（你现在读了 `cases`、`plan` 兜底、`output`、`logs` ✅）。
- **重复次数**：优先 `case["case_repeats"]`，其次 `case["repeats"]`，默认 1（见 §4 第 4 条）。
- 日志 `[HH:MM:SS]` ✅、`RESULT … status=` ✅、退出码 0/130/1 ✅。
- **套件结果字段要补齐**（见 §4 第 2 条）。
- **不要整体覆盖 `job.json`**（见 §4 第 3 条）。

---

## 4. 代码侧必改清单（按优先级，附现状与改法）

### 4.1 【阻断】引擎包未交付
- 现状：`adapter.py:20-22` 顶层 `from zenoh_bench.config import BenchConfig, PAYLOAD_SIZES` 等；仓库无 `zenoh_bench`。
- 改法（二选一）：
  - **A（推荐）**：把引擎包随代码交付——在仓库根放 `zenoh_bench/`（含 `__init__.py`、`config.py`、`runner.py`、`scenarios.py`，与你 README 描述一致），并补根 `pyproject.toml`；
  - **B**：如果它是独立分发的包，请在 `README.md` + `requirements.txt` 里写清**安装来源**（`pip install <包名==版本>` 或 git URL），并确保 `metadata().available` 在未安装时为 `false`。

### 4.2 【阻断】顶层导入必须惰性化
- 现状：顶层导入引擎 → 引擎缺失时整份适配器不可用（控制台显示"加载失败"，连条件目录都没有）。
- 改法：

```python
try:
    from zenoh_bench.config import BenchConfig, PAYLOAD_SIZES
    from zenoh_bench.runner import METRICS_VERSION, ZenohBench
    from zenoh_bench.scenarios import SCENARIOS
    ENGINE_ERROR = None
except Exception as error:            # 只记录，不抛出
    ENGINE_ERROR = f"{type(error).__name__}: {error}"
    BenchConfig = ZenohBench = None
    PAYLOAD_SIZES = (1024, 4096, 16384, 32768, 49152, 65536, 262144, 1048576)
    METRICS_VERSION = "1.0"
    SCENARIOS = {}
```

`metadata()` 里把 `ENGINE_ERROR` 写进 `notes`（例如"未安装/未交付 `zenoh_bench` 引擎：<原因>"），`run()` 在 `ENGINE_ERROR` 存在时返回 `status="not_tested"`、指标全 `null`（**不伪造**）。

### 4.3 【阻断】补 `build_cases()` / `base_config()` / `runner_command()`
见 §3.1。缺少它们控制台无法启动 Zenoh。

### 4.4 【逻辑】重复次数语义
- 现状：`console_runner.py:100,104` 用 `max(5, int(case.get("repeats", default_repeats)))` → **强制至少 5 轮**，且只认 `repeats` 不认 `case_repeats`。
- 影响：与控制台契约（`case_repeats` 决定轮数）不符；"冒烟 1 轮"跑不了；正式重复次数应由控制台传（5 或 10）。
- 改法：

```python
repeats = int(case.get("case_repeats") or case.get("repeats") or 1)
```

### 4.5 【逻辑】`job.json` 不能整体覆盖
- 现状：`console_runner.py:95,139` 直接把 `job.json` 写成 `{"middleware_id","job_id","status","started_at"/"run_count","finished_at"}`。
- 后果：控制台在作业开始前写入了 `{id, middleware_id, started, ended, total, matrix, case}`；被你覆盖后 **`id` 字段消失**，而控制台历史记录按 `"id" in data` 过滤 → **该作业不会出现在历史下拉框里**（`started` 也变成 `started_at`，时间显示异常）。
- 改法：**读-改-写**（保留控制台字段），例如：

```python
job_path = root / "job.json"
job = json.loads(job_path.read_text(encoding="utf-8")) if job_path.exists() else {}
job.update(middleware_id="zenoh", status=suite["status"],
           run_count=len(all_runs), ended=suite["finished_at"])
job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
```

（对照：`mqtt/console_runner.py` 就是这样 merge 的；`DDS/console_runner.py` 则完全不碰 `job.json`，两者都可以。）

### 4.6 【字段】套件结果缺少控制台使用的键
- 现状：`output/result.json` = `{schema_version, metrics_definition_version, middleware_id, middleware_version, status, started_at, finished_at, runs, summary}`。
- 控制台读取（根 README §8/§6）：`planned_runs`、`test_start_time`、`test_end_time`、`environment`、`limitations`、`scenario_summaries`。
- 后果：报告页的"条件汇总"表为空、计划轮次与起止时间显示"—"（指标卡与曲线不受影响）。
- 改法：套件里补上

```python
suite.update(
    planned_runs=sum(repeats_of(c) for c in cases),
    test_start_time=started_at, test_end_time=finished_at,
    environment=(all_runs[-1].get("environment") if all_runs else {}),
    limitations=limitations,
    scenario_summaries=<按条件分组汇总，可复用你现有的 _aggregate>  # 键名与 VSOA/DDS/MQTT 一致
)
```

（`summary` 可以保留，但请额外提供 `scenario_summaries`。）

### 4.7 【字段】把样本写成四家一致的文件
- 现状：单轮结果里有 `samples: [{sequence, latency, publisher_id, subscriber_id}]` ✅，但**没有** `artifacts/<run_id>/subscriber-0.result.json`，也没有 `samples_path`。
- 说明：控制台已加兜底——`artifacts` 文件缺失时会改读单轮结果的 `samples` 字段，所以曲线能显示；但为了与 VSOA/DDS/MQTT 完全对齐（也便于人工核查），建议补：

```python
(artifacts_dir / run_id).mkdir(parents=True, exist_ok=True)
(artifacts_dir / run_id / "subscriber-0.result.json").write_text(json.dumps({
    "latencies_ms": [s["latency"] for s in valid_samples],
    "latency_sample_count": len(valid_samples),
    "samples": valid_samples,
}, ensure_ascii=False, indent=2), encoding="utf-8")
result["samples_path"] = f"artifacts/{run_id}/subscriber-0.result.json"
result["artifacts_directory"] = f"artifacts/{run_id}"
```

### 4.8 【命名】`module_name` 必须小写
- 现状：`adapter.py:136` 传 `"module_name": "Zenoh"`，单轮结果 `adapter.py:210` 也是 `"module_name": "Zenoh"`。
- 要求（根 README §2）：接口 ID 一律小写 `zenoh`，不得用厂商名/大写；显示名放 `label`，厂商与版本放 `metadata().version` / `environment`。
- 改法：两处都改为 `"zenoh"`（结果里已另有 `middleware_id: "zenoh"` ✅，保持两者一致）。

### 4.9 【字段】`link_metrics` 命名与内容
- 现状：`adapter.py:271` 直接放引擎的 `delivery_matrix`（字段名取决于缺失的引擎，很可能是 `publisher_id`/`subscriber_id`）。
- 要求（根 README §6）：字段名必须是 `publisher` / `subscriber`（从 0 开始），并建议附每链路延迟分布，便于控制台画 P95 基准线：

```python
result["link_metrics"] = [{
    "publisher": row["publisher_id"], "subscriber": row["subscriber_id"],
    "messages_sent": row["messages_sent"], "messages_received": row["unique_deliveries"],
    "packet_loss": row.get("packet_loss"), "final_packet_loss": row.get("final_packet_loss"),
    "latency_ms": row.get("latency_distribution") or {"count": 0, "mean": None},
} for row in raw.get("delivery_matrix", [])]
```

### 4.10 【字段】`statistics` 结构
- 现状：`{counts, metrics, measurement_level, ...}`。
- 要求（四家统一）：按指标给分布，控制台会读 `statistics.latency_ms.count`：

```python
"statistics": {
    "latency_ms": describe(latencies), "jitter_ms": describe(jitters),
    "cpu_percent": describe(cpu_samples), "memory_mb": describe(rss_samples),
    "discovery_time_ms": describe(discovery_samples),
}
```

（顶层 `latency_sample_count` 你已提供 ✅，所以这是"对齐"而非阻断项。）

### 4.11 【交付】`requirements.txt` 与 README 引用
- 新增 `Zenoh/requirements.txt`：`eclipse-zenoh>=1.10,<2`、`psutil>=7,<8`、`jsonschema>=4,<5`（另加引擎包的安装来源，若按 §4.1-B 处理）。
- 根 `pyproject.toml` 不存在：删除 README 里的"与根目录 `pyproject.toml` 一致"表述，或补上该文件。

---

## 5. 场景要求（PDF §3 / §4 / §6）

PDF 要求 12 个场景；其中 S09/S10/S11 是当前的主要缺口：

| 场景 | PDF 要求 | 你现在 | 必须改成 |
|---|---|---|---|
| S01 | 1P/1S、1 KiB、100/1000 Hz | ✅ 2 条件 | 保持 |
| S02 | 固定拓扑/速率/QoS，遍历 1K–1M 共 8 档 | ✅ 逻辑有（依赖引擎 `PAYLOAD_SIZES`） | 按 §3.3 加兜底，保证 8 档与另外三家一致 |
| S03 | 64K/256K/1M 用**最大稳定速率** | ✅ 3 条件（`publish_rate_hz=0`） | 结果里建议补 `rate_calibration`（候选扫描结论） |
| S04 | 100/1000/5000 Hz + 最大稳定速率 | ✅ 4 条件 | 同上 |
| S05/S06/S07 | 1P4S / 4P1S / 4P4S | ✅ 各 1 条件 | 保持 |
| S08 | ≥5 分钟，观察延迟漂移与内存增长 | ✅ 300 s | 建议补"延迟漂移 / RSS 增长"两个趋势字段，便于与 DDS/MQTT 对齐 |
| **S09** | **1% / 5% / 10% 丢包 + 延迟/抖动** | ❌ **4 档（含 baseline）全部无条件标 `unsupported`**（`adapter.py:177-179`） | **baseline（0 损伤）必须可执行**：它不需要注入器，属于真实可测条件，当前白白浪费；三档非零损伤在无真实注入器时继续 `not_tested` 并写明原因 |
| **S10** | 冷启动、热启动、多端点发现、超时 | ⚠️ 4 项条件存在，但只有 `cold_start` 真实执行，其余 `not_tested` | 至少再实现"**热启动**"（session 已存在时测量 ready 与首样本）与"**启动超时**"（连接失败/超时路径）；多端点发现可保留 `not_tested` 并说明 |
| **S11** | 进程 / Broker / Router / 网络故障后的恢复 | ❌ 唯一条件 `process_restart` 直接标 `unsupported` | **至少让"进程重启"可执行**（本机可做：kill 掉 subscriber/publisher 进程 → 重新拉起 → 记录 `recovery_time_ms` 与恢复后丢失）；Router/网络故障可继续 `not_tested` |
| S12 | 长度、校验和、缺失、重复、乱序 | ✅ 1 条件 | 保持 |
| 通用 | 每条件 ≥5 次有效重复（正式）；消息含序列号/时间戳/长度/校验和 | 引擎已具备（README 已说明） | 重复次数由控制台传（§4.4） |

> 原则不变：做不到的就显式 `not_tested`/`unsupported` 并写原因，**绝不伪造**。但"能做的却标 unsupported"（S09 baseline、S11 进程重启）会被视为覆盖不达标。

---

## 6. 自测验收（冒烟级：只需证明"跑通"）

```powershell
# 1) 契约层在"零依赖"解释器里可导入（关键！）
python -c "import importlib.util as u; s=u.spec_from_file_location('z','Zenoh/adapter.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(m.metadata()['available'], len(m.catalog()), sorted({r['scenario_name'] for r in m.catalog()})); print(m.runner_command())"
#   期望：available 为真实布尔值；条件数 ≥20；场景集合 = {S01..S12}；不抛异常

# 2) 五个钩子齐备
python -c "import importlib.util as u; s=u.spec_from_file_location('z','Zenoh/adapter.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print([h for h in ('metadata','catalog','build_cases','base_config','runner_command','create_adapter') if hasattr(m,h)])"

# 3) 冒烟跑一个条件一轮（case_repeats 写 1）
python Zenoh/console_runner.py <job_dir>/spec.json
#   期望：[HH:MM:SS] 日志；START/END；RESULT … status=…；退出码 0；output/result.json + runs/ + artifacts/

# 4) 控制台联调
python songfei/run.py
#   期望：下拉出现 Zenoh（可用或"入口缺失 + 原因"）；S01–S12 齐全；任选一个条件能出数据或明确的 not_tested

# 5) 边界
git status --short          # 只有 Zenoh/ 下改动
```

---

## 7. 禁止事项

1. 不得修改 `songfei/`、`interfaces/`（`interfaces/scenarios.py` 是场景唯一来源）。
2. 不得删场景、不得伪造/估算指标、不得把未测量写成 `0`。
3. 不得让 `adapter.py` 顶层依赖 `zenoh_bench`/`eclipse-zenoh`（会直接破坏"即插即用"）。
4. 不得用 `"Zenoh"` 这类大写/厂商名作为接口 ID。
5. 不得整体覆盖控制台写入的 `job.json`（会让作业从历史记录里消失）。
6. 不得把结果写到别的中间件目录或仓库根。
