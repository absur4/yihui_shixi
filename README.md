# 四种通信中间件性能对比试验

本项目在统一场景、统一参数和统一指标口径下，对 **VSOA、Fast DDS、MQTT、Zenoh** 四种通信中间件进行性能测试与横向对比。项目提供统一适配器、场景定义、测试执行器和浏览器可视化控制台，既可在本机回环环境中运行单项测试，也可保存矩阵测试结果用于绘图、分析和汇报。

> 本仓库中的现有对比数据为同一台计算机上的本地回环测试结果，适合比较四种实现的相对性能，不等同于真实物理网络或跨设备性能。

## 1. 项目框架

### 1.1 运行链路

```text
浏览器可视化界面
    │  HTTP API
    ▼
songfei/app.py（统一控制台与作业管理）
    │
    ├── VSOA/adapter.py  ── VSOA/console_runner.py
    ├── DDS/adapter.py   ── DDS/console_runner.py
    ├── mqtt/adapter.py  ── mqtt/console_runner.py
    └── Zenoh/adapter.py ── Zenoh/console_runner.py
            │
            ▼
      各中间件真实测试引擎
            │
            ▼
统一 JSON 结果、原始样本、日志和可视化报告
```

控制台按 `vsoa -> dds -> mqtt -> zenoh` 的顺序扫描各目录中的 `adapter.py`。适配器负责把统一测试参数转换为中间件自身的运行方式，`console_runner.py` 负责启动测试、收集指标并写入统一格式的结果文件。

### 1.2 目录结构

```text
.
├── README.md                    # 项目总览、启动方式和数据位置
├── interfaces/
│   ├── __init__.py              # 统一适配器与结果类型
│   └── scenarios.py             # S01-S12 场景定义的唯一来源
├── VSOA/                        # VSOA 适配器、执行器和测试引擎
├── DDS/                         # Fast DDS 适配器、执行器和测试引擎
├── mqtt/                        # MQTT 适配器、Mosquitto 与测试引擎
├── Zenoh/                       # Zenoh 适配器、执行器和测试引擎
└── songfei/
    ├── run.py                   # 可视化控制台启动入口
    ├── app.py                   # HTTP API、作业管理和适配器加载
    ├── static/index.html        # 浏览器实际加载的前端页面
    ├── results/                 # 从可视化界面运行后生成的作业数据
    └── test/                    # 已整理的本地回环对比数据、绘图脚本和汇报材料
```

各中间件的依赖、能力和独立运行方式见：

- [DDS/README.md](DDS/README.md)
- [mqtt/README.md](mqtt/README.md)
- [Zenoh/README.md](Zenoh/README.md)

VSOA 当前没有单独的模块 README，其统一入口为 `VSOA/adapter.py` 和 `VSOA/console_runner.py`，核心实现位于 `VSOA/vsoa_py/standalone/`，依赖清单见 `VSOA/vsoa_py/requirements.txt`。

## 2. 开启可视化

### 2.1 启动

在仓库根目录执行：

```powershell
python songfei/run.py
```

服务启动后会自动打开浏览器；如未自动打开，请手动访问：

```text
http://127.0.0.1:8787/
```

控制台会显示已经加载的四种中间件及其可用状态。选择中间件、测试场景和参数后即可启动测试；执行日志、当前状态、指标和曲线会在页面中更新。按 `Ctrl+C` 可停止控制台。

### 2.2 环境说明

- 控制台 HTTP 服务使用 Python 标准库，无需单独安装 Web 框架。
- 各测试引擎仍需安装自身依赖；具体要求以对应中间件目录中的 README 和 `requirements.txt` 为准。
- MQTT 测试使用仓库中的 Mosquitto 组件或 `mqtt/config.yaml` 指定的 Broker。
- 某个中间件环境未就绪时，控制台仍可启动，但该中间件会显示不可用，或将对应测试记录为 `not_tested`。
- `8787` 端口被占用时，可换一个端口启动，例如：`python -c "from songfei.app import serve; serve(port=8788)"`。

## 3. 本地回环数据保存位置

项目中有两类结果目录，使用时不要混淆。

### 3.1 可视化控制台新生成的作业

从页面启动的测试统一保存在：

```text
songfei/results/<middleware_id>/<job_id>/
```

四种中间件对应目录为：

```text
songfei/results/vsoa/
songfei/results/dds/
songfei/results/mqtt/
songfei/results/zenoh/
```

单个作业的典型结构如下：

```text
<job_id>/
├── job.json                     # 作业状态和页面配置
├── spec.json                    # 传给执行器的完整测试计划
├── console.log                 # 控制台日志
└── output/
    ├── result.json              # 整个作业的汇总结果
    ├── runs/<run_id>.json       # 每一轮的统一指标
    ├── artifacts/<run_id>/      # 延迟样本等原始测量产物
    └── logs/<run_id>/           # 发布端、订阅端和服务进程日志
```

### 3.2 当前四种中间件的本地回环对比数据

当前用于性能对比、绘图和汇报的本地回环数据集中保存在 `songfei/test/`：

| 中间件 | 数据目录 | 主要内容 |
|---|---|---|
| VSOA | `songfei/test/vsoa/` | `S01-S08/group*.json`、`runs/`、`artifacts/`、`history/`、`logs/`、`summary.json`、`changes.json` |
| Fast DDS | `songfei/test/DDS/results/` | 无扩展名的 JSON 汇总文件 `matrix_s01_s08`、`raw/` 原始逐轮结果、控制台日志 |
| MQTT | `songfei/test/MQTT/` | `S01-S08/group*.json`、`runs/`、`artifacts/`（含进程日志）、`summary.json`、`changes.json` |
| Zenoh | `songfei/test/zenoh/` | `S01-S08/group*.json`、`artifacts/engine-run/`、`summary.json`、`changes.json` |

其中：

- `group<N>.json` 是某场景第 N 组参数所选定的单轮完整结果，不是多轮统计汇总。
- 各中间件的原始归档结构不同：存在时，`runs/` 保存单轮统一结果，`artifacts/` 保存原始样本及相关进程文件，`logs/` 保存独立日志。
- `summary.json` 是中间件级汇总，`changes.json` 记录同一场景中各参数组相对基准组的变化。
- `songfei/test/*.png` 是基于上述数据生成的对比图，不是原始测量数据。
- `songfei/test/_backup_drain03/` 是旧收尾窗口参数下的备份数据，不应与当前正式数据混用。

## 4. 统一适配器契约

每个中间件目录均提供 `adapter.py`，控制台主要调用以下接口：

| 接口 | 作用 |
|---|---|
| `metadata()` | 返回中间件 ID、版本、可用性、传输方式和 QoS 信息 |
| `catalog()` | 返回 S01-S12 标准测试条件 |
| `build_cases()` | 将页面参数或完整矩阵展开为可执行条件 |
| `base_config()` | 返回执行器的基础配置 |
| `runner_command()` | 返回该中间件的子进程启动命令 |
| `run()` | 供脚本直接执行一轮真实测量 |

稳定的中间件 ID 为 `vsoa`、`dds`、`mqtt`、`zenoh`。页面显示名可以变化，但接口 ID、适配器名称和控制台作业目录 `songfei/results/<middleware_id>/` 必须一致；`songfei/test/` 中已有的整理数据保留其原始目录大小写。

## 5. 统一输入字段

四种中间件共用以下主要测试参数：

```text
scenario_name, case, condition_id, title
payload_size_bytes, publish_rate_hz, publisher_count, subscriber_count
message_count, duration_seconds, repeats, random_seed
warmup_seconds, drain_seconds, timeout_seconds
network_delay_ms, network_jitter_ms, network_loss_rate, network_profile
transport_mode, qos_profile
```

其中 payload 使用字节，速率使用 Hz，时长使用秒，延迟与抖动使用 ms，`network_loss_rate` 使用 0-1 比例。

## 6. 统一结果与指标

每轮结果保存在 `output/runs/<run_id>.json`，作业汇总保存在 `output/result.json`。主要对比指标包括：

- 延迟：平均值、P95、P99、标准差和 jitter，单位为 ms。
- 吞吐：实收吞吐和应发吞吐，单位为 Mbit/s。
- 可靠性：发送、接收、唯一交付、丢失、重复、乱序和损坏数量。
- 时序：启动时间、发现时间和故障恢复时间，单位为 ms。
- 资源：CPU 使用率和 RSS 内存，内存单位为 MB。
- 控制台统一状态：`completed`、`error`、`cancelled`、`timeout`、`unsupported` 或 `not_tested`。

无法真实测量的指标必须写为 JSON `null`，不能使用 `0` 或固定常数代替。四种中间件的相同字段必须保持相同含义和单位。`songfei/test/DDS/results/matrix_s01_s08` 是导入的 DDS 历史矩阵文件，内部保留原执行器的 `passed` 状态；进入控制台作业目录后的状态按上述统一枚举转换。

## 7. 测试场景

统一框架定义了 S01-S12。当前 `songfei/test/` 中集中整理的本地回环对比数据主要覆盖 S01-S08。

| 场景 | 内容 | 主要观察指标 |
|---|---|---|
| S01 | 点对点延迟 | 平均延迟、P95、P99 |
| S02 | 消息尺寸扫描 | 延迟、吞吐、丢失率 |
| S03 | 大消息吞吐 | 吞吐、尾延迟、CPU、内存 |
| S04 | 发送速率扫描 | 吞吐、P99、丢失率 |
| S05 | 一对多广播 | 各订阅者交付、延迟和资源 |
| S06 | 多对一汇聚 | 各发布者交付、延迟和资源 |
| S07 | 多对多并发 | 链路指标、吞吐、延迟和丢失 |
| S08 | 长时间稳定性 | 延迟漂移、jitter、CPU 和内存 |
| S09 | 弱网恢复 | 窗口内/最终丢失、延迟和 jitter |
| S10 | 启动与发现 | 启动、发现和首包延迟 |
| S11 | 断连与故障恢复 | 恢复时间和最终丢失 |
| S12 | 数据正确性 | 缺失、重复、乱序和损坏 |

## 8. 子进程执行协议

控制台为每个作业生成 `spec.json`，然后执行：

```text
python <middleware>/console_runner.py <job_dir>/spec.json
```

执行器逐轮写入 `output/runs/`，结束后生成 `output/result.json`。标准输出使用带时间戳的日志，并以 `RESULT <path> status=<status>` 报告最终状态。成功退出码为 `0`，取消通常为 `130`，其他退出码表示失败。

## 9. HTTP 接口

前端通过以下接口管理测试：

- `GET /api/init`：中间件元数据、场景目录和历史作业。
- `GET /api/state`：当前作业状态和实时日志。
- `GET /api/history?middleware=<id>`：指定中间件的历史作业。
- `GET /api/results?middleware=<id>&job=<job_id>`：结果、样本、链路数据和日志。
- `POST /api/start`：启动当前条件或完整矩阵。
- `POST /api/stop`：停止当前测试并清理子进程。
- `GET /files/<middleware>/<job>/report.html`：查看单个作业报告。

核心原则：四种中间件可以采用不同内部实现，但进入可视化与对比流程的场景、字段、单位和状态必须一致。
