# MQTT 双机跨设备测试包

版本：2.0.0。Python 3.11 或更高版本，Windows 为主要使用环境。

这个文件夹是独立的新版本，用于同一局域网内两台电脑的真实 MQTT 通信及 CD1–CD4 测试。原 `github_mqtt_v2/mqtt` 不需要替换。把本文件夹完整复制到两台电脑，例如两边都放在 `D:\mqtt_cross_device_v2`。

数据链路：发送电脑上的 Publisher → 同机 Mosquitto Broker → 局域网 → 接收电脑上的 Subscriber。反向测试时，Broker 和 Publisher 切换到另一台电脑。两台电脑各运行一个 Agent，控制器只需在任意一台电脑上运行。

## 1. 两台电脑各准备一次

安装 Python 3.11/3.12 及 Mosquitto，推荐 Mosquitto 2.x。需要完整 Mosquitto 安装目录及其 DLL，不能只复制一个 exe。已有可运行的 `broker` 文件夹可以整体继续使用，通过参数提供其中的 `mosquitto.exe` 路径；没有验证全部旧版本兼容性。本包不捆绑 Python、Broker 或虚拟环境。

在 PowerShell 中进入本文件夹：

```powershell
cd D:\mqtt_cross_device_v2
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

以下命令直接使用虚拟环境解释器，不需要激活脚本或更改 PowerShell 执行策略。

只在一台电脑生成一个随机令牌：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd token
```

在两台 Agent 的 PowerShell 窗口以及控制器窗口中，都设置同一个生成值：

```powershell
$env:MQTT_CD_TOKEN = "这里填同一个生成的令牌"
```

令牌不写入配置、归档或结果。Agent 的 HTTP 控制通道和主试验 MQTT 通道不加密，适用于受信任的测试局域网；不要把 8765、1883 端口映射到公网。防火墙仅向本地子网开放 Agent TCP 8765 和测试 Broker TCP 1883，或实际配置的端口。

## 2. 分别启动两台 Agent

电脑 A：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd agent --node-id A --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

电脑 B：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd agent --node-id B --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

`--broker-executable` 改为各台机器实际路径。两台电脑都准备 Broker，才能做 A→B、B→A 角色互换。不要额外启动占用测试端口的 Broker；程序会启动专用实例，并在测试结束后关闭。

Agent 默认监听所有本机网卡的 8765 端口，数据留在各自的 `agent-data`。需要限制网卡可加 `--host 本机局域网IP --broker-bind 本机局域网IP`。Agent 保持运行；按 Ctrl+C 关闭时会清理其创建的进程。

两台电脑和控制器必须使用同一份代码，连接检查会核对源码 SHA-256。测试期间不要修改源码或升级依赖；更新后重启两端 Agent。

## 3. 在控制器电脑填写 IP

复制示例配置：

```powershell
Copy-Item config.example.json config.json
notepad config.json
```

把 A、B 的 `url` 和 `mqtt_host` 改成两台电脑的实际局域网 IP。例如：

```json
"nodes": {
  "A": {"url": "http://192.168.1.10:8765", "mqtt_host": "192.168.1.10"},
  "B": {"url": "http://192.168.1.20:8765", "mqtt_host": "192.168.1.20"}
}
```

这是配置片段，修改现有对象即可，不要删除其他配置。`url` 是 Agent 控制地址；`mqtt_host` 是对方可以连接的 Broker 地址。物理电脑 A、B 的身份不随测试方向改变。

填写 AP、信道、位置等环境记录。正式试验前按方案完成网络基线、空闲环境检查，并把记录路径填入 `environment.network_baseline_artifacts`。本包不会自动更改网卡、Wi-Fi、系统电源模式或防火墙。

控制器另开一个已设置相同令牌的 PowerShell，检查连接：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd check --config config.json
```

`dependencies` 中 Paho 和 psutil 应可用，`managed_broker` 应为 true。如果 `password_tool=false`，程序会使用与 Mosquitto 官方密码格式兼容的 PBKDF2-SHA512 回退，不影响正常运行。

## 4. 先跑一个短测试

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd run --config config.json --scenarios SMOKE --direction both
```

每个方向各跑 1 轮、100 Hz、1 KiB、正式 2 秒，验证连接、数据和归档。程序显示 RESULT 和最终 HTML 报告路径。此项是功能检查，不是容量基准。

数据传输正常后，可以快速验证一发四收：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd run --config config.json --scenarios CD3 --direction A_to_B --repeats 1 --duration 5
```

这个命令运行 P=1/2/4 的短探索条件，明确标记 exploratory，不用于正式容量边界。

## 5. 按正式场景测试

| 场景 | 默认负载 | 正式时长和轮次 |
|---|---|---|
| CD1 | 1 KiB，100/500/1000/2000/5000/10000 Hz | 每档 20 秒、5 轮。 |
| CD2 | 200 Hz，1/16/64/256/1024 KiB | 每档 20 秒、5 轮。 |
| CD3 | 1 KiB、1000 Hz，1/2/4 个独立订阅进程 | 每档 20 秒、5 轮。 |
| CD4-C | 三种中间件双方向共同可承受的绝对速率 | 300 秒、5 轮，需完整外部容量参考。 |
| CD4-L | 本方向 MQTT 确认通过速率的 90% | 300 秒、5 轮。 |
| CD4-H | 本方向确认失败速率的 120%；未找到失败档则使用已测最高档并标注 | 300 秒、5 轮。 |

生成计划，不启动测试：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd plan --scenarios CD1,CD2,CD3 --direction both --output plan.json
```

执行三个扫描场景：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd run --config config.json --scenarios CD1,CD2,CD3 --direction both --output results\scans-01
```

在原扫描结果基础上确认边界、最多三次二分和确认 CD2 平台：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd run --config config.json --scenarios CD4 --direction both --prior-results results\scans-01\result.json --refine --output results\followup-01
```

自动容量判定需要合格的时钟与窗口证据。默认校时模型尚未经验证时，上述命令会保留不可测原因并跳过无法推导的 CD4，不会猜一个极限。每次运行使用新输出目录，避免覆盖已有试验。

`--scenarios all --direction both --refine` 可以一次执行扫描、边界确认和可推导的 CD4；完整矩阵耗时较长，不建议首次连接时使用。

正式轮次冷却固定为 20 秒。配置其他 `cooldown_s` 会标为探索测试。随机种子默认为 20260916，`--seed` 会同时传入扫描、边界确认、平台确认及 CD4。环境、时钟配置、冷却时间或随机种子变化时，不能继续使用旧比较指纹下的容量参考。

后续结果保留 `reference_runs`，继续引用后续结果时也会带入更早的证据。边界和平台确认中断后，只补缺少的槽位，已执行的失败槽位不会被自动重跑为成功。

## 6. 时钟质量与如何先运行 CD4

配置默认 `clock.model_validated=false`。程序已经实现两机四时间戳交换、每 5 秒校准、分段映射、不确定度传播和窗口边界检查；但运行程序本身不等于验证了两台真实机器的漂移误差上界。

因此默认仍输出真实收发数、接收端本地固定窗口吞吐、重复/乱序/损坏和两端资源；单程延迟及严格容量判定显示不可测/不确定。这是防止跨机计时产生虚假精度。

完成方案要求的独立校准验证后，记录实际的 `drift_bound_ppm`、可选的 `timestamp_error_ns`、`validation_residual_ns`，将证据说明或文件标识写入 `model_validation_reference`，才能设 `model_validated=true`。不要仅为了显示通过而改为 true。改变这些条件后属于新比较指纹，需要重测，不能静默套用旧容量结果。

暂时没有校时验证或容量参考，也能手动指定 CD4 的探索速率：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd run --config config.json --scenarios CD4 --direction A_to_B --manual-cd4 500,2000 --output results\cd4-explore-01
```

这会测试低负载 500 Hz、高负载 2000 Hz，各 300 秒、5 轮，并注明手动探索，不宣称它们是已确认的稳定点和饱和点。首次验证分桶可以用短版本：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd run --config config.json --scenarios CD4 --direction A_to_B --manual-cd4 100,500 --duration 20 --warmup 1 --drain 1 --repeats 1
```

`--common-rate 100` 可增加手动 CD4-C，但仍是探索性共同速率。正式 CD4-C 需要 VSOA、MQTT、Zenoh 两个方向的六个已验证容量参考，结构见 `capacity-reference.example.json`；不能用 MQTT 的结果替代其他中间件数据。

## 7. 输出内容与口径

```text
results/<suite>/
  experiment_manifest.json
  schedule.json
  result.json
  report.html
  runs/<run_id>/
    spec.json
    clocks.json
    controller.json
    result.json
    report.html
    nodes/A/
    nodes/B/
```

HTML 报告可直接用浏览器打开，不需要启动网页服务器。每轮 `nodes` 保留发送/接收 CSV、资源 JSONL、节点信息、状态和日志。两台 Agent 本地也保留原始归档。

`experiment_manifest.json` 保存实验身份、源码哈希、两端版本、配置和引用轮次。源提交日志与发布端封存计数、接收日志与接收端封存计数分别核对；日志缺失或截断不会被当成完整的零接收结果。

- `send_achievement_ratio`：实际提交数 / 计划数。
- `delivery_achievement_ratio`：窗口内唯一交付 / 已提交消息对应的预期副本数。
- `end_to_end_achievement_ratio`：窗口内唯一交付 / 计划副本数；等于前两者的乘积，不取最小值。
- `throughput_mbps`：接收端正式固定窗口内的有效净荷吞吐，多订阅者累计量与每订阅者量分开。
- `final_missing_ratio`：固定收尾截止前仍未交付的比例，不等于网络包丢失率。
- `drain_delivery_ratio`：仅收尾期才补齐的比例。
- 延迟包括平均、P50/P95/P99、总体标准差和连续序号抖动；不满足样本量或时钟要求则 null 并说明原因。
- 资源按物理 A/B 及 Publisher/Subscriber/Broker 分列；CPU 一逻辑核=100%，内存采用十进制 MB。
- CD4 区分本地到达桶和按发送批次的桶；跨机源批次、连续维持和同步资源指标需要合格时基。

`execution_status=completed` 仅表示程序完成，不代表容量通过。请同时查看 `capacity_verdict`、`metric_quality`、`clock_quality`、逐订阅者及缺失/失败记录。

重算某一轮：

```powershell
.\.venv\Scripts\python.exe -m mqtt_cd analyze results\某次试验\runs\某个run_id
```

默认写到该轮的 `reanalysis`，保留原始 CSV 和原始结果不变。

## 8. 停止、故障及常见问题

- 控制器按 Ctrl+C：请求两端停止、保留可获得的部分结果并清理 Broker/端点。
- 控制器进程崩溃或网络断开：Agent 在 30 秒控制租约过期后清理本轮子进程。
- Windows Agent 被强制终止：Job Object 约束其创建的进程，避免测试 Broker 和端点遗留。
- 端口占用：更改 `config.json` 的 `broker_port` 或停止自己已有的测试 Broker；Agent 端口变化也要同步更新 url。
- 连接超时：先检查两机 IP、Agent 是否运行、令牌是否相同、AP 客户端隔离和本地子网防火墙规则，再看节点日志。
- 两个 Agent 报告同一主机名：控制器默认拒绝，防止把本机模拟误认为双机。真正不同机器需要配置唯一主机名；`--allow-local-check` 只供本机开发，结果明确排除正式容量参考。
- 大消息/高频失速：是需记录的实验现象；程序有有限队列和内存截止，不会为了补齐计划而无限排队。

## 9. 与方案及原项目的边界

本包实现 MQTT 双机执行引擎、控制器、CD 场景与离线报告，不修改原 S01–S12，也没有修改 GitHub 主项目 `songfei` 或声称其网页已经完成双机接入。未来网页可调用本包控制器，不能继续把原单机适配器当成双机入口。

本包只负责 MQTT，不会执行 VSOA/Zenoh 或自动验证三方服务语义。环境预检、网络基线、校时误差证据、外部容量参考和三方交错运行由完整试验负责人提供。自动收集的 HTTP 计数仅为控制 JSON 正文流量，不是包含 TCP、HTTP 头及校准全部网络成本的接口字节计数。

软件测试与本机真实 Broker 冒烟的具体结果见 `VERIFICATION.md`。未运行完整正式矩阵，也未声称已在两台物理电脑上完成性能验证。

完整测试定义见 `docs/BENCHMARK_PLAN.md`。提交 GitHub 时可以把整个本文件夹作为一个独立目录提交；不要提交 `.venv`、`agent-data`、`results`、含真实环境地址的 `config.json` 或任何令牌。
