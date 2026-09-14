# MQTT 统一适配器

此目录对应仓库规范中的 `mqtt/`，实现 `interfaces.MiddlewareAdapter` 和 `create_adapter()`，供 `songfei/app.py` 注册。`Adapter.name`、`middleware_id` 和目录归档名统一为 `mqtt`。

## 文件

- `adapter.py`：仓库适配器入口，提供 `metadata()`、`catalog()`、`run()`。
- `console_runner.py`：独立子进程入口，运行 `python mqtt/console_runner.py <job_dir>/spec.json`。
- `requirements.txt`：MQTT 测量依赖。
- `config.yaml`、`config.template.yaml`、`config.formal.yaml`：S01–S12 标准条件和模板。
- `result.schema.json`、`feature_profile.schema.json`：结果与功能特性 Schema。
- `mqtt_*.py`：测量、封装、统计、报告、校验和 UI 支持模块。
- `tests/`：公式、payload、拓扑交付和配置校验测试。

## 运行

在仓库根目录执行：

```powershell
python -m unittest discover -s mqtt/tests -v
python mqtt/console_runner.py mqtt/results/job-001/spec.json
```

`spec.json` 中的 `config`、`cases`、`output`、`logs` 对应规范字段；结果写入作业目录，不写入 `songfei/` 或其他中间件目录。每轮包含统一公共字段、实际配置、统计公式、环境、限制和真实原始样本路径。

## 能力矩阵

| 场景 | 状态 | 说明 |
|---|---|---|
| S01–S08 | supported | 本机 MQTT TCP；真实 Broker、发布者、订阅者测量 |
| S09 baseline | supported | 无损基线；非零网络条件需要外部真实注入器 |
| S09 非零弱网 | not_tested | 不把应用丢弃或离线恢复伪装成网络层丢包 |
| S10 | supported / negative cases | 冷热启动、端点发现、失败和超时均保留真实状态 |
| S11 | supported / partial | 进程/Broker/TCP relay 故障；网络层丢包不由此声称支持 |
| S12 | supported | 长度、CRC、缺失、重复、乱序、无法解析 |

支持传输：`tcp`；支持 QoS：`qos0`、`qos1`、`qos2`。多机协同、远端 Broker 资源统计、TLS 和真实网络层丢包注入未实现，并在 metadata/result 的 `notes`/`limitations` 中明确。

GitHub 不建议提交大体积 `mqtt.exe`、Broker DLL、`outputs/`、`ui_runs/`、日志、缓存和备份。提交源码、配置、Schema、测试、说明和一个小型脱敏结果示例即可；完整结果放 Release 或外部制品。
