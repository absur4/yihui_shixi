# Songfei VSOA 多机协同使用说明

## 1. 运行结构

多机协同由一个 Songfei 控制端和至少两个 VSOA Agent 节点组成：

| 角色 | 用途 | 是否运行网页 |
| --- | --- | --- |
| Songfei 控制端 | 提供网页、检测节点、下发实验并汇总结果 | 是 |
| VSOA Agent 1 | 运行 Position、发布者或订阅者进程 | 否 |
| VSOA Agent 2 及更多节点 | 运行发布者或订阅者进程 | 否 |

控制机可以同时作为一个 Agent，但网页中必须填写至少两个不同的 Agent 地址。正式测试建议每台物理机器只启动一个 Agent。

所有机器必须使用相同版本的本项目、Python 和 `vsoa` Python 包。多机进程会直接导入并调用 `vsoa`，不会使用模拟通信。

## 2. 网络与端口

开始前确认：

- 各机器处于互相可达的局域网，尽量使用有线网络。
- 各机器已通过 NTP 或 PTP 校时。单向延迟依赖时钟同步，时钟误差过大会直接影响结果。
- 控制端可以访问所有 Agent 的 TCP `8790` 端口。
- Agent 之间允许 TCP 和 UDP `30050–30058`。默认基础端口是 `30050`，最多 8 个发布者，Position 使用发布者端口之后的一个端口。
- Windows 防火墙或其他安全软件没有拦截所使用的 Python 解释器。
- Agent 的 `--advertise-host` 必须填写其他机器能够访问的真实局域网 IPv4 地址，不能填写 `127.0.0.1`。

可以在每台机器执行 `ipconfig`，从正在使用的网卡中找到 IPv4 地址。例如：

```text
Agent 1: 192.168.1.10
Agent 2: 192.168.1.11
```

## 3. 在所有机器安装环境

把同一份项目复制到控制机和每台 Agent。以下命令均从项目根目录执行：

```powershell
python -m pip install -r VSOA\vsoa_py\requirements.txt
python -c "import vsoa; print(vsoa.__version__)"
```

记录每台机器输出的 VSOA 版本。Songfei 在检测节点时会比较版本，只要有一台不一致就不允许开始实验。

## 4. 生成共享令牌

在任意一台机器生成一个随机令牌：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

复制输出值，并在所有 Agent 上使用完全相同的令牌。不要把真实令牌提交到代码仓库或发送到不可信网络。

Agent API 当前使用 HTTP，令牌本身不加密。因此应仅在可信、隔离的测试网络中使用；跨不可信网络时，应在 Agent 前配置 HTTPS 反向代理。

## 5. 启动 Agent 节点

在 Agent 1 上打开 PowerShell，进入项目根目录并执行：

```powershell
$env:VSOA_AGENT_TOKEN = "替换成刚才生成的共享令牌"
cd VSOA\vsoa_py
python -m standalone.agent `
  --host 0.0.0.0 `
  --port 8790 `
  --advertise-host 192.168.1.10 `
  --root D:\vsoa-agent-runs
```

在 Agent 2 上执行相同命令，只把 `--advertise-host` 改为该机器的真实地址：

```powershell
$env:VSOA_AGENT_TOKEN = "替换成相同的共享令牌"
cd VSOA\vsoa_py
python -m standalone.agent `
  --host 0.0.0.0 `
  --port 8790 `
  --advertise-host 192.168.1.11 `
  --root D:\vsoa-agent-runs
```

看到下面这类输出表示 Agent 已启动：

```text
VSOA agent listening on http://0.0.0.0:8790; advertise=192.168.1.10
```

运行实验期间不要关闭这两个终端。`--root` 指定的是 Agent 临时运行目录，路径必须可写。

## 6. 手工检查 Agent

在控制机 PowerShell 中设置相同令牌，然后分别检查两个节点：

```powershell
$env:VSOA_AGENT_TOKEN = "替换成相同的共享令牌"

Invoke-RestMethod `
  -Uri http://192.168.1.10:8790/v1/health `
  -Headers @{"X-VSOA-Agent-Token"=$env:VSOA_AGENT_TOKEN}

Invoke-RestMethod `
  -Uri http://192.168.1.11:8790/v1/health `
  -Headers @{"X-VSOA-Agent-Token"=$env:VSOA_AGENT_TOKEN}
```

两个请求都应返回 `ok: true`，并显示正确的 `advertise_host`、机器名和 VSOA 版本。

## 7. 启动 Songfei 网页

在控制机项目根目录执行：

```powershell
python songfei\run.py
```

浏览器打开：

```text
http://127.0.0.1:8787/
```

如果网页之前已经打开过，按 `Ctrl+F5` 强制刷新一次。

## 8. 从网页开始多机测试

按下面的顺序操作：

1. 点击左上方的“多机协同”。
2. 在“Agent 地址”中每行填写一个地址，至少两行，例如：

   ```text
   http://192.168.1.10:8790
   http://192.168.1.11:8790
   ```

3. 在“Agent 令牌”中填写所有节点共同使用的令牌。
4. 点击“检测 Agent 节点”。
5. 确认每个节点前面都显示 `✓`，并核对机器名、广播地址、VSOA 版本和检测延迟。
6. 第一次验证建议选择 S01、1 个发布者、1 个订阅者、较少消息数、1 次重复，并确保延迟、抖动和丢包均为 0。
7. 点击“开始多机测试”。
8. 在右侧运行日志中观察启动、校时、Position 发现、发布、订阅和结果汇总过程。

修改 Agent 地址或令牌后，必须重新检测节点。点击开始时，后端也会再次检测，防止检测后节点已经离线或版本发生变化。

## 9. 默认节点分配

未提供自定义分配时，系统自动使用以下规则：

- Position 运行在第一个 Agent。
- 发布者从第一个 Agent 开始轮询分配。
- 订阅者从第二个 Agent 开始轮询分配。
- 每个订阅者连接所有发布者，形成完整的发布订阅关系。

例如使用两个 Agent、2 个发布者、2 个订阅者时，发布者会依次分配到 Agent 1 和 Agent 2，订阅者会依次分配到 Agent 2 和 Agent 1。

## 10. 当前限制

- 多机模式不接受网页中配置的人工丢包、延迟、抖动或断网参数。需要弱网测试时，应在真实交换机、路由器或网络整形设备上配置。
- 远程进程重启类故障恢复条件暂不支持。选择不支持的条件时，网页会明确拒绝，不会退回本机执行，也不会生成模拟结果。
- 延迟是校准后的跨机单向延迟。当报告中的时钟同步不确定度接近或大于测得延迟时，应先改善 NTP/PTP，不应直接使用该延迟小数值。
- TCP 和 UDP、单播、广播、扇入、mesh、发现、稳定性及正确性等普通条件使用真实跨机 VSOA 链路。

## 11. 结果位置

控制机上的结果保存在：

```text
songfei\results\vsoa\<job_id>\
```

主要文件包括：

- `job.json`：作业状态和 Agent 地址，不包含令牌。
- `spec.json`：本次测试计划和参数，不包含令牌。
- `console.log`：运行日志。
- `output\result.json`：汇总结果。
- `output\runs\`：每轮测试结果。
- `output\artifacts\`：各角色的原始测量产物。

令牌只通过当前控制进程的环境变量传给执行器，不会写入上述结果文件。正常完成、手工停止或关闭 Songfei 服务时，控制端都会通知 Agent 回收属于该作业的远程进程。

## 12. 常见问题

### 点击“检测 Agent 节点”后提示连接失败

检查 Agent 终端是否仍在运行、地址是否写成真实 IPv4、控制机能否访问 TCP 8790，以及防火墙是否放行。

```powershell
Test-NetConnection 192.168.1.10 -Port 8790
Test-NetConnection 192.168.1.11 -Port 8790
```

### 提示 `unauthorized`

网页填写的令牌与 Agent 环境变量不一致。重新设置相同令牌并重启对应 Agent。

### 提示 VSOA 版本不一致

在控制机和所有 Agent 上执行：

```powershell
python -c "import vsoa; print(vsoa.__version__)"
```

使用相同 Python 环境重新安装同一版本后，再启动 Agent 和 Songfei。

### 提示 `Position lookup failed`

确认 `--advertise-host` 不是 `127.0.0.1`，Agent 之间可以互相访问，并放行 TCP/UDP `30050–30058`。

### 提示订阅者连接失败

通常是发布者端口被防火墙拦截、节点间路由不可达，或 `advertise_host` 填写了错误网卡地址。先用两台机器、1P/1S 和 S01 做最小测试。

### 延迟结果异常大或大量接近 0

先检查所有机器时间同步状态，尽量使用 PTP 或同一局域网 NTP 服务器；同时避免 Wi-Fi、省电模式和高后台负载。再查看结果中的 `clock_synchronization.maximum_uncertainty_ms`。

### 如何停止实验

点击网页中的“停止实验”。控制端会结束本地执行器，并通知所有 Agent 清理本作业启动的远程进程。若控制机异常退出，可在各 Agent 终端按 `Ctrl+C`，Agent 会关闭它管理的进程。
