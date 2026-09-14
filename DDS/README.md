# Fast DDS Python 统一通信中间件测试适配器

版本：1.0.1  
目标规范：《四种通信中间件统一测试需求文档》1.0  
目标环境：Windows x64、Fast DDS 3.6.2、Fast-DDS-python 2.6.1、Python 3.11

本项目用 Python 实现测试控制、发布者、订阅者、资源采样、原始记录、指标计算、汇总和校验。它覆盖需求文档中的统一参数、1P/1S、1P/4S、4P/1S、4P/4S、1 KiB～1 MiB 消息矩阵、线性插值 P95/P99、总体标准差、相邻延迟差抖动、payload 吞吐、CPU、RSS、丢包、恢复、完整性和统一 JSON 结果。

> 1.0.1 修复：读取 Fast DDS-Gen 为有界 IDL 字符串生成的
> `fixed_string` 时改用 `payload_str()`，避免订阅端把有效样本误判为完整性错误。

## 1. 必须先说明的边界

业务和测试源代码均为 Python；但 Fast DDS 本身是原生 C++ 库，官方 `fastdds` Python 包是其 SWIG 包装，用户自定义 DDS 类型也必须由 Fast DDS-Gen 从 IDL 生成并编译原生类型支持。因此，“Python 调用 Fast DDS”可以实现，“完全不含任何原生 DLL/编译步骤的纯 Python Fast DDS”不能实现。本项目不要求你手写任何 C++：`setup_windows.bat` 会自动下载兼容绑定、调用 Fast DDS-Gen 并完成编译。

官方依据：

- [Fast DDS 3.6.2 Python 示例](https://fast-dds.docs.eprosima.com/en/latest/fastdds/getting_started/simple_python_app/simple_python_app.html)
- [Fast-DDS-python 官方仓库](https://github.com/eProsima/Fast-DDS-python)
- [Fast DDS / Python 绑定版本对应表](https://github.com/eProsima/Fast-DDS-python/blob/master/RELEASE_SUPPORT.md)

单向延迟使用 `time.perf_counter_ns()`，所以发布者和订阅者必须在同一台主机上；跨主机测试需要统一时钟同步方案，不能直接使用本适配器的单向延迟值。

## 2. 目录说明

```text
fastdds_python_benchmark_v1.0/
├─ fastdds_bench/                 Python 实现
│  ├─ controller.py              多进程编排、启动/发现/排空、结果落盘
│  ├─ endpoint.py                Fast DDS Publisher / Subscriber
│  ├─ metrics.py                 统一指标算法
│  ├─ rawio.py                   固定长度二进制逐消息记录
│  ├─ resource_monitor.py        仅采样中间件必要进程
│  └─ ...
├─ idl/BenchmarkMessage.idl      统一测试消息定义
├─ schemas/unified_result.schema.json
├─ tests/                        不依赖真实 DDS 通信的算法测试
├─ tools/setup_windows.py        绑定与 IDL 类型支持自动构建
├─ tools/verify_example.py       示例结果严格验收
├─ config.example.yaml           200 条消息的小例子
├─ config.yaml                   S01～S12 正式条件矩阵
├─ setup_windows.bat
├─ run_one_example.bat
├─ validate_one_example.bat
└─ run.bat
```

## 3. Windows 一次性准备

### 3.1 必需软件

请使用 64 位版本，并保证以下命令可在终端中找到：

| 软件 | 要求 |
|---|---|
| Python | 3.10、3.11 或 3.12；推荐 3.11 x64 |
| Fast DDS | 3.6.2，包含 Fast CDR、DLL 和 CMake package 文件 |
| Fast DDS-Gen | 与 Fast DDS 3.6.x 匹配；`fastddsgen.bat` 可用 |
| Visual Studio | 带“使用 C++ 的桌面开发”工作负载和 x64 MSVC |
| CMake | 可用 `cmake --version` 检查 |
| Git | 用于取得官方 Fast-DDS-python v2.6.1 |
| SWIG | 4.1.x；官方 2.6.1 绑定要求 `< 4.2` |
| Java | Fast DDS-Gen 所需；`java -version` 可用 |

建议从“x64 Native Tools Command Prompt for Visual Studio”执行后续命令。

假定 Fast DDS 安装在 `D:\fastdds`，目录中至少应能找到 `bin`、`lib`，并能在其自身或独立 Fast DDS-Gen 目录找到 `fastddsgen.bat`。

### 3.2 设置环境变量

```bat
cd /d D:\你的目录\fastdds_python_benchmark_v1.0
set FASTDDSHOME=D:\fastdds
```

如果 Fast DDS-Gen 不在 `%FASTDDSHOME%\bin` 或 `PATH` 中，再设置：

```bat
set FASTDDSGEN=D:\Fast-DDS-Gen\scripts\fastddsgen.bat
```

如需跨终端永久生效，可用 `setx`，但执行后要重新打开终端：

```bat
setx FASTDDSHOME D:\fastdds
setx FASTDDSGEN D:\Fast-DDS-Gen\scripts\fastddsgen.bat
```

### 3.3 自动安装和构建

```bat
setup_windows.bat
```

该脚本会依次：

1. 建立 `.venv`；
2. 安装 PyYAML、psutil、jsonschema 和 pywin32；
3. 获取官方 Fast-DDS-python `v2.6.1`；
4. 针对 `%FASTDDSHOME%` 编译并安装绑定到项目私有 `runtime`；
5. 从 `BenchmarkMessage.idl` 生成并编译 Python 类型模块；
6. 执行配置和运行时探针。

重新构建可执行：

```bat
.venv\Scripts\python.exe tools\setup_windows.py --force
```

构建完成后，检查环境：

```bat
check_environment.bat
```

输出中的 `config_ok` 和 `runtime_probe.ok` 都应为 `true`。

## 4. 小例子：真实 Fast DDS 通信验证

例子使用显式 UDPv4、可靠 QoS、1 个发布者、1 个订阅者、1024 字节 payload、100 Hz、200 条正式消息，预热 1 秒、排空 1 秒。

```bat
run_one_example.bat
validate_one_example.bat
```

第一条命令会运行真实 Fast DDS Publisher/Subscriber；第二条命令会同时检查：

- JSON Schema；
- 成功发送数、预期交付数和最终有效接收数均为 200；
- 最终丢包率为 0；
- 重复、乱序、损坏、未知消息、时钟异常和元数据不一致均为 0；
- 延迟、吞吐、CPU、内存有有效值；
- 发布和接收原始文件的 SHA-256 与结果文件一致。

通过时会看到：

```text
EXAMPLE VERIFICATION PASS
result: PASS
sent: 200
received_final: 200
final_packet_loss: 0.0
```

延迟和吞吐的具体数值取决于机器，不应照抄固定“标准答案”。结果位于 `outputs\one_example.json`，逐消息原始记录和端点日志位于 `outputs\raw\...`。

## 5. 正式测试

### 5.1 运行全部已启用条件

```bat
run.bat
```

结果为 `outputs\results.json`。程序每完成一轮就原子更新 JSON，因此中途失败时已完成轮次仍可追溯。当前配置包含 30 个条件，每个正式条件默认 5 次：其中 26 个无需外部故障设施的条件已启用，共 130 轮。S09 的 3 个弱网条件和 S11 的 1 个故障条件保留在配置中但默认禁用；接入四种中间件共用的网络/故障注入器后才应启用，完整矩阵即 150 轮。

当前矩阵较长，且包括五次 5 分钟稳定性测试，通常至少需要约 1.5 小时；大消息和机器性能会进一步影响时间。为保护结果，正式命令默认拒绝覆盖已有 JSON；再次运行时请先归档 `results.json` 或指定新的 `--output`。只有明确传入 `--overwrite` 才会替换结果索引，原始目录仍使用唯一 run ID。

### 5.2 只跑指定条件

```bat
.venv\Scripts\python.exe launch.py run ^
  --config config.yaml ^
  --condition S01_1k_100 ^
  --output outputs\S01_1k_100.json
```

快速冒烟测试可临时覆盖重复次数：

```bat
.venv\Scripts\python.exe launch.py run ^
  --config config.yaml ^
  --condition S01_1k_100 ^
  --repeats 1 ^
  --output outputs\smoke.json
```

正式结果不要使用 `--repeats 1`；需求规定每个条件至少 5 次有效重复，推荐 10 次。

### 5.3 S01 已按目标设置

`config.yaml` 中：

- `S01_1k_100`：100 Hz、10,000 条、100 秒；
- `S01_1k_1000`：1000 Hz、10,000 条、10 秒。

多发布者条件中的 `publish_rate_hz` 和 `message_count` 都是聚合值。例如 4P、1000 Hz、10,000 条表示四个发布者合计 1000 Hz、合计 10,000 条，不会意外放大四倍。

### 5.4 校验、重汇总和导出原始记录

```bat
.venv\Scripts\python.exe launch.py validate --input outputs\results.json

.venv\Scripts\python.exe launch.py resummarize ^
  --input outputs\results.json ^
  --output outputs\results_resummarized.json

.venv\Scripts\python.exe launch.py export-raw ^
  --input outputs\raw\某个suite\某个run\subscriber_0.receive.bin ^
  --output outputs\subscriber_0.receive.csv
```

`resummarize` 只使用单轮 `runs` 重建 `summary`，用于验证汇总可重算。

## 6. S09、S11 为什么不伪造

Python Fast DDS API不能在传输层真实注入指定的链路延迟、抖动、丢包和带宽，也不能公平模拟四种不同中间件的网络故障。若简单在订阅回调里随机丢消息，网络负载、重传、CPU 和延迟都不会受到真实影响，结果不能横向比较。

因此：

- S09 必须使用同一个外部网络仿真器作用于四种中间件的真实流量。实际注入后，将相应 `network_profiles.*.external_impairment_confirmed` 改为 `true`，再把条件 `enabled` 改为 `true`；未确认时控制器会拒绝运行。
- S11 应由四种适配器共用的故障编排器，在相同时间点停止/恢复进程、Broker、Router 或网络。当前 Fast DDS 包会拒绝把仅写在 YAML、却未实际执行的 `fault_plan` 计作有效结果。

这两个保护是为了防止生成看起来完整、实际上不可比较的伪数据。统一注入接口和生命周期见 `UNIFIED_CONTRACT.md`。

## 7. 指标口径摘要

- 消息唯一键：`(publisher_id, sequence_number)`；每条正式消息都带单调时钟发送时间、payload 长度和 CRC32。
- `payload_size_bytes` 只指应用 payload，不含 DDS/CDR 和测试元数据。
- 延迟只统计长度、校验和、元数据均正确的唯一消息。
- P95/P99 使用 `h=(n-1)p` 线性插值；标准差使用总体标准差。
- 抖动按每条 Publisher→Subscriber 链路、序列号排序后计算相邻延迟绝对差的均值。
- 排空/恢复阶段到达的消息不进入原始延迟和吞吐样本，但进入恢复数与最终丢包率。
- 吞吐只计算 payload bit；广播的总交付量按所有订阅者有效交付总数计算。
- CPU 为发布者和订阅者进程 CPU 时间之和除以资源窗口，一个逻辑核为 100%；控制进程不计入。
- 内存为各必要端点进程 RSS 之和的采样峰值，单位为十进制 MB。
- `packet_loss` 以控制器的单调时钟发送完成边界为准；`final_packet_loss` 是排空后的最终损失。订阅端快照同时保留用于审计。
- `latency_drift_ms` 是最后 10% 有效延迟均值减最前 10%；`memory_growth_mb` 是最后与最初有效 RSS 样本之差，用于 S08 观察。
- 没有有效样本、未测量或不适用时写 JSON `null`，不写 0 或 NaN。

完整生命周期、公式、原始记录和跨中间件适配约束见 `UNIFIED_CONTRACT.md`。

## 8. UDP 与 SHM 的可比性

Fast DDS 在同机默认配置下可能使用共享内存。为保证 `transport_mode: UDPv4` 真的是 UDP，本项目生成显式 UDPv4 transport descriptor，并设置 `useBuiltinTransports=false`；同时关闭 Data Sharing，防止绕过目标传输。`SHM` 条件同样显式选择共享内存 transport。不要把 `DEFAULT` 与显式 `UDPv4`/`SHM` 混为同组平均。

## 9. 离线算法自测

安装完成后可运行：

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

这些测试覆盖配置矩阵、精确 payload、大消息边界、传输 XML、二进制记录、截断检测、线性分位数、总体标准差、按链路抖动、恢复阶段剔除、丢包和汇总分组。真实 DDS 通信由第 4 节例子验证。

## 10. 常见问题

### `No module named fastdds` 或 DLL load failed

确认 Python、Fast DDS、MSVC 都是 x64；确认 `%FASTDDSHOME%\bin` 中 DLL 可用；重新打开 x64 Native Tools 终端后执行：

```bat
.venv\Scripts\python.exe tools\setup_windows.py --force
```

### `SWIG < 4.2` 检查失败

安装 SWIG 4.1.x，把它放到 PATH 前面，或设置：

```bat
set SWIG_EXECUTABLE=D:\swigwin-4.1.1\swig.exe
```

### 找不到 Fast DDS-Gen

设置 `FASTDDSGEN` 指向真实的 `fastddsgen.bat`，并确认 `java -version` 成功。

### CMake 找不到 `fastdds` 或 `fastcdr`

`FASTDDSHOME` 必须指向安装前缀，而不是源码目录。该前缀中应存在 Fast DDS/Fast CDR 的 CMake package 文件和 `bin`/`lib`。如果两者安装在不同前缀，先把它们安装到同一前缀，再运行本项目。

### 发现超时

检查 Windows 防火墙、DDS domain 冲突和 UDP multicast/unicast 权限；关闭遗留测试进程后重试。每轮使用唯一 Topic，domain ID 在 0～232 内轮换。

### 大消息丢包或吞吐异常

保留原始记录和端点日志；确认使用的 transport、QoS、排空时间和系统 UDP 缓冲一致。不要只增加一方的参数后与其他中间件直接比较。
