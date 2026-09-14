# MQTT 接入模块

供 songfei 统一控制台执行 MQTT 真实测量；仅当当前解释器具备依赖且配置的 Mosquitto 文件存在时 `available=true`。安装依赖：`python -m pip install -r mqtt/requirements.txt`；另行安装 Mosquitto，在 `config.yaml` 的 `broker_executable` 配置可执行文件路径（相对路径以 mqtt 目录为准）。配置文件使用 JSON 兼容 YAML，目录展开不需要第三方库。

从仓库根目录冒烟验证（S01 一个条件、一轮）：`python mqtt/smoke.py`。子进程协议入口：`python mqtt/console_runner.py <job>/spec.json`。单元测试：`python -m unittest discover -s mqtt/tests -v`。

接口清单：

| 接口 | 功能 |
|---|---|
| `create_adapter()` | 创建供控制台注册的适配器实例。 |
| `Adapter.name` / `MIDDLEWARE_ID` | 返回固定中间件 ID mqtt。 |
| `metadata()` / `Adapter.metadata()` | 返回版本、真实依赖可用性及中文修复提示。 |
| `catalog()` / `Adapter.catalog()` | 展开 S01–S12 的 39 个标准条件并读取仓库的统一场景标题。 |
| `build_cases()` / `Adapter.build_cases()` | 按表单参数构建一个条件或忽略表单展开所选场景的完整矩阵。 |
| `base_config()` / `Adapter.base_config()` | 返回写入 spec 的全局配置。 |
| `runner_command()` / `Adapter.runner_command()` | 返回当前解释器与执行器路径。 |
| `Adapter.run()` | 执行单轮并返回仓库定义的 ScenarioResult。 |
| `result_dict()` | 从 ScenarioResult.metrics 读取完整公共单轮记录供归档。 |
| `calibrate()` | 扫描候选速率并记录首个失败候选前的最高通过速率。 |
| `console_runner.py` | 按 spec 逐条件逐轮执行并原子刷新结果和作业状态。 |
| `contain_children()` | 使用 Windows Job Object 确保执行器被强制终止时清理其子进程。 |
| `smoke.py` | 创建并运行一个条件一轮的冒烟作业。 |
| `output/result.json` | 提供控制台轮询使用的套件汇总。 |
| `output/runs/<run_id>.json` | 保存公共格式单轮结果。 |
| `output/artifacts/<run_id>/subscriber-0.result.json` | 提供从真实 CSV 导出的延迟曲线样本。 |
| `mqtt_adapter.py` | 提供 --config/--validate/--features/--resume 原生命令行入口。 |

能力矩阵（传输 TCP，QoS 0/1/2，速率为每发布者速率）：

| 场景 | 能力 |
|---|---|
| S01 | 支持点对点延迟。 |
| S02 | 支持 1 KiB–1 MiB 八档扫描。 |
| S03 | 支持大消息及候选速率校准。 |
| S04 | 支持固定速率及候选最大稳定速率。 |
| S05 | 支持 1P/4S 与逐链路统计。 |
| S06 | 支持 4P/1S 与逐链路统计。 |
| S07 | 支持 4P/4S 与 16 条链路统计。 |
| S08 | 支持 300 秒长期测试与趋势。 |
| S09 | 部分支持：基线可测，非零丢包/延迟/抖动因缺真实包级注入器返回 not_tested。 |
| S10 | 支持冷/热启动、多端点发现和预期失败用例。 |
| S11 | 支持端点/Broker 重启及 TCP relay 断网、断连恢复。 |
| S12 | 支持长度、校验和、缺失、重复和乱序统计。 |

限制：仅本机回环，不能推断物理网络性能；最大稳定速率仅代表候选网格和当前实现；TCP relay 故障不是包级弱网。缺依赖或缺能力时指标为 null、样本为空并说明原因，不伪造数据。目录脱离主仓库时标题显示场景 ID，复制到仓库 mqtt/ 后自动使用 interfaces/scenarios.py；不维护第二份标题表。

本次验证：通过 16 项单元测试（含 Windows 强制停止清理子进程）及不加载第三方包的导入检查；完成一个条件一轮的缺依赖降级冒烟，套件 completed、单轮 not_tested，归档完整。未运行真实 Broker 性能测试、全量矩阵或 songfei 页面联调。正式测试由控制台执行。
