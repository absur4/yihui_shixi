# DDS 原生环境准备（SETUP）

> 契约层 `DDS/adapter.py` 与编排层 `DDS/console_runner.py` **不需要**本文件里的任何东西就能
> 被导入和运行（环境未就绪时返回 `status="not_tested"`）。本文件只说明**怎样打开真实测量**。

## 1. 为什么 DDS 不是 "pip 装完就能跑"

Fast DDS 是原生 C++ 库，官方 `fastdds` Python 包是它的 SWIG 包装；用户自定义 DDS 类型还必须由
Fast DDS-Gen 从 IDL 生成并编译原生类型支持。因此本模块需要一个**私有虚拟环境**和一个
**编译产物目录**：

```text
DDS/.venv/      Python 3.11 虚拟环境（编排层与引擎都在这里跑）
DDS/runtime/    编译出来的 fastdds 绑定 + BenchmarkMessage 类型支持 + build_info.json
DDS/idl/BenchmarkMessage.idl    统一测试消息定义（精确 ASCII payload、CRC32、序列号、时间戳）
```

`adapter.py` 会在主解释器里被控制台导入（只有标准库），`runner_command()` 会把执行器指向
`DDS/.venv/Scripts/python.exe`，两者互不冲突——这就是"三层解耦"。

## 2. 必需软件（Windows x64）

| 软件 | 要求 |
|---|---|
| Python | 3.11 x64（推荐）；3.10 / 3.12 也可用于建 `.venv` |
| Fast DDS | 3.6.2，含 Fast CDR、DLL 与 CMake package 文件 |
| Fast DDS-Gen | 与 Fast DDS 3.6.x 匹配；`fastddsgen.bat` 可用 |
| Visual Studio | "使用 C++ 的桌面开发" 工作负载 + x64 MSVC |
| CMake | `cmake --version` 可用 |
| Git | 用于拉取官方 Fast-DDS-python `v2.6.1` |
| SWIG | 4.1.x（官方 2.6.1 绑定要求 `< 4.2`） |
| Java | Fast DDS-Gen 所需；`java -version` 可用 |

建议从 "x64 Native Tools Command Prompt for Visual Studio" 执行后续命令。

## 3. 环境变量

```bat
set FASTDDSHOME=D:\fastdds
rem Fast DDS-Gen 不在 %FASTDDSHOME%\bin 或 PATH 中时再设置：
set FASTDDSGEN=D:\Fast-DDS-Gen\scripts\fastddsgen.bat
```

需要永久生效就用 `setx`，之后**重新打开终端**：

```bat
setx FASTDDSHOME D:\fastdds
setx FASTDDSGEN D:\Fast-DDS-Gen\scripts\fastddsgen.bat
```

## 4. 一键构建

```bat
cd /d <仓库>\DDS
setup_windows.bat
```

该脚本依次：

1. 建立 `.venv`（`py -3.11 -m venv .venv`）；
2. 安装 `requirements.txt`（PyYAML / psutil / jsonschema / pywin32）；
3. 拉取并编译官方 Fast-DDS-python `v2.6.1` 到 `DDS/runtime`；
4. 用 Fast DDS-Gen 从 `idl/BenchmarkMessage.idl` 生成并编译 Python 类型支持；
5. 修复 Windows 上 `LoadLibrary` 指向 `.lib` 的问题，并把探测结果写入
   `runtime/build_info.json`（含 `fastdds_version`，`adapter.metadata().version` 优先读它）；
6. 执行 `launch.py check --config config.example.yaml` 作为构建自检。

重建：

```bat
.venv\Scripts\python.exe tools\setup_windows.py --force
```

## 5. 检查环境

```bat
check_environment.bat
```

期望输出里 `config_ok` 与 `runtime_probe.ok` 都是 `true`，并包含
`total_condition_count` / `enabled_condition_count` / `not_tested_condition_count`。

再确认契约层的判断与之一致：

```powershell
python -c "import importlib.util as u,sys;sys.path.insert(0,'DDS');s=u.spec_from_file_location('a','DDS/adapter.py');m=u.module_from_spec(s);s.loader.exec_module(m);print(m.metadata()['available'], m.metadata()['environment_detail'])"
```

`available` 为 `true` 的成立条件（三者必须同时满足）：

1. 能找到 `fastdds` Python 绑定（`DDS/runtime/**/site-packages/fastdds` 或当前解释器）；
2. 能找到 IDL 生成的类型支持模块 `BenchmarkMessage`；
3. 存在私有虚拟环境 `DDS/.venv`，且设置了 `FASTDDSHOME`。

`cmake` / `swig` / `java` / Fast DDS-Gen 只在**重建绑定**时需要，缺失只会写入 `metadata().notes`
的提示，不会让 `available` 变成 `false`。

## 6. 打开真实测量后的第一次运行

```bat
run_one_example.bat
validate_one_example.bat
```

产物在 `results\one_example\`：

```text
result.json                              套件级结果
runs\<run_id>.json                       单轮统一字段结果
artifacts\<run_id>\publisher_0.send.bin          原始发送记录（含 SHA-256）
artifacts\<run_id>\subscriber_0.receive.bin      原始接收记录（含 SHA-256）
artifacts\<run_id>\subscriber-0.result.json      延迟曲线样本 latencies_ms
artifacts\<run_id>\logs\                         端点日志
```

`validate_one_example.bat` 会同时检查 JSON Schema、200/200 收发、0 丢失、0 重复/乱序/损坏、
延迟与吞吐有有效值、原始文件 SHA-256，以及延迟样本文件非空。

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| `No module named fastdds` / `DLL load failed` | 确认 Python / Fast DDS / MSVC 都是 x64；`%FASTDDSHOME%\bin` 有 DLL；重开 x64 终端后重跑 `setup_windows.bat` |
| `SWIG < 4.2` 检查失败 | 安装 SWIG 4.1.x 并放到 PATH 前面，或设置 `SWIG_EXECUTABLE` |
| 找不到 Fast DDS-Gen | 设置 `FASTDDSGEN` 指向真实的 `fastddsgen.bat`，确认 `java -version` 成功 |
| CMake 找不到 `fastdds` / `fastcdr` | `FASTDDSHOME` 必须指向 install prefix（含 CMake package 与 `bin`/`lib`），不是源码目录 |
| 控制台显示"入口缺失" | 看 `metadata().notes` 里的中文原因，按上面三条件逐项补齐 |
| 发现超时 | 检查 Windows 防火墙、DDS domain 冲突、UDP multicast/unicast 权限；关闭遗留测试进程后重试 |
| 大消息丢包或吞吐异常 | 保留原始记录与端点日志；确认 transport、QoS、drain 时间、UDP 缓冲一致，不要只改一侧参数 |

## 8. 不要提交的东西

`.venv/`、`runtime/`、`.build/`、`output/`、`results/`、`outputs/`、`__pycache__/` 都已在
`DDS/.gitignore` 里，提交前请确认 `git status` 只显示 `DDS/` 下的源码改动。
