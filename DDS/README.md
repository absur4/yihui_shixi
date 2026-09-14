# DDS 模块

本模块为控制台提供 Fast DDS 的统一适配器、S01-S12 条件目录和结果落盘；`available=true` 仅在运行时探测到 Fast DDS 绑定、IDL 类型支持、DDS/.venv 与 `FASTDDSHOME` 时成立。

冒烟验证（只跑一个条件、一次重复即可验证链路）：

```powershell
python tools/make_smoke_spec.py --job results/smoke --index 0 --repeats 1
python console_runner.py results/smoke/spec.json
```

## 接口清单

- `create_adapter()`：创建并返回 DDS `Adapter` 实例供控制台注册。
- `Adapter.name`：返回固定的中间件 ID `dds`。
- `Adapter.metadata()`：返回名称、版本、供应商、传输/QoS 选项及真实环境可用性。
- `Adapter.catalog()`：返回 S01-S12 的全部标准条件，包括能力状态和参数字段。
- `build_cases(index, configuration, matrix)`：将当前表单参数或完整矩阵展开为引擎可执行条件列表。
- `base_config()`：返回引擎全局配置，供控制台写入 `spec.json` 的 `config`。
- `Adapter.run(scenario, parameters)`：执行单轮场景并返回统一的 `ScenarioResult`，环境未就绪时返回 `not_tested`。
- `probe_environment()`：探测 Fast DDS 运行环境并返回 `(ok, reason)`，探测异常不会冒泡。
- `runner_command()`：返回启动 `console_runner.py` 的解释器和命令行。
- `console_runner.py <spec.json>`：按 spec 逐条件逐轮执行，输出 `START`、`END`、`RESULT` 日志并写入结果文件。
- `execute_spec(spec)`：执行一个控制台 spec 并返回套件级结果文档。
- `case_repeats(case)`：按 `case_repeats`、`repeats`、默认值解析重复次数。
- `output/result.json`：保存套件级状态、计划轮次、场景汇总、运行列表和限制说明。
- `output/runs/<run_id>.json`：保存单轮统一字段结果，未测指标保持 JSON `null`。
- `output/artifacts/<run_id>/subscriber-0.result.json`：保存真实接收记录的延迟样本，供前端曲线读取。
- `launch.py`：调用原生 Fast DDS CLI，供人工复现和环境调试。

## 能力矩阵

目录始终保留 S01-S12 全部条件。当前无原生运行环境时，条件会以 `not_tested` 状态落盘，不删除条件、不伪造指标；S09 的非 baseline 网络注入、S10 的热启动/启动超时、S11 的网络中断依赖外部注入或后续能力，默认标记为 `not_tested`。

## 已知限制

Fast DDS 依赖 C++、SWIG、IDL 生成器、MSVC/CMake 和专用 Python 绑定，不能仅通过 pip 安装完成。`udp` 映射显式 UDPv4，`shm` 映射显式共享内存描述符；RELIABLE 仅表示 DDS 协议层重传。资源指标范围为 participant 进程（发布者和订阅者），不包含 Broker/Router。

模块不会为缺失测量填充 0 或随机值；所有不可用指标写为 `null`，并在 `limitations` 中说明原因。环境准备请参考 [`SETUP.md`](SETUP.md)。
