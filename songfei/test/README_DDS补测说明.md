# DDS 补测清单（用于与 VSOA / MQTT / Zenoh 并表出图）

**目的**：把 DDS 的实测数据补齐到**恰好 26 个条件**，使汇报 PPT 的五张图里 DDS 的每个数据点都有实测值 —— 不多一个、不少一个。

**约束**：只跑本文列出的 26 个条件。配置文件 `songfei/test/DDS/matrix_s01_s08.yaml` 里另有 9 个条件**这五张图用不到**，可以跳过（省约 1/3 时间）。

**为什么需要补**：`DDS/results/matrix_s01_s08` 那批结果是用**旧版配置**跑的，与三家矩阵存在三处不一致：

| 不一致 | 旧结果 | 需要 |
|---|---|---|
| S03 发送速率 | `unpaced`（不限速） | **20 Hz**（1 MiB 档为 5 Hz） |
| S04 组4 / S08 组4 速率 | `unpaced` | 10000 Hz / 5 Hz |
| S08 时长 | 300 s | **100 s** |

另有两处**从未跑过的档位**：S02 的 32 KiB、48 KiB。

---

## 一、需要跑的 26 个条件

### 图 1（第 12 页）· 六场景重复性 —— 需 6 个

同一条件（**1P/1S、1024 B、1000 Hz**）在 6 个场景里各一次：

| condition_id | 场景 | duration | 说明 |
|---|---|---|---|
| `S01_G3_1000Hz` | S01 组3 | 10 s | |
| `S04_G2_1000Hz` | S04 组2 | 8 s | |
| `S05_G1_1S` | S05 组1 | 10 s | |
| `S06_G1_1P` | S06 组1 | 10 s | |
| `S07_G1_1P1S` | S07 组1 | 8 s | |
| `S08_G2_1KiB_1000Hz` | S08 组2 | **100 s** | 旧结果是 300 s，必须按 100 s 重跑 |

### 图 2（第 13 页）· 消息大小阶梯与分片拐点 —— 需 6 个

固定 **1P/1S、100 Hz、duration 10 s、message_count 1000**：

| condition_id | payload | 状态 |
|---|---|---|
| `S02_G1_1KiB` | 1 KiB | 已有 |
| `S02_G2_4KiB` | 4 KiB | 已有 |
| `S02_G3_16KiB` | 16 KiB | 已有 |
| **`S02_G5_32KiB`** | **32 KiB** | **从未跑过** |
| **`S02_G6_48KiB`** | **48 KiB** | **从未跑过**（跨 VSOA 分片阈值 60000 B 的关键点） |
| `S02_G4_64KiB` | 64 KiB | 已有 |

### 图 3（第 14 页）· 大消息尾部代价 —— 需 5 个

固定 **1P/1S**：

| condition_id | payload | 速率 | duration | 状态 |
|---|---|---|---|---|
| `S03_G1_64KiB_20Hz` | 64 KiB | **20 Hz** | 10 s | 旧结果是不限速，须重跑 |
| `S03_G2_128KiB_20Hz` | 128 KiB | **20 Hz** | 10 s | 同上 |
| `S03_G3_256KiB_20Hz` | 256 KiB | **20 Hz** | 10 s | 同上 |
| `S03_G4_512KiB_20Hz` | 512 KiB | **20 Hz** | 10 s | 同上 |
| `S03_G5_1MiB_5Hz` | 1 MiB | **5 Hz** | **40 s** | 从未跑过；40 s 是为使样本数（200）与其它档对齐 |

> 1 MiB 不要用 20 Hz：20 Hz × 1 MiB = 168 Mbps，远超本机可达带宽，只会测出"发不出去"。

### 图 4（第 15 页）· 拓扑成本结构 —— 需 12 个

固定 **1024 B、1000 Hz**：

| condition_id | 拓扑 | duration |
|---|---|---|
| `S05_G1_1S` | 1P → 1S | 10 s |
| `S05_G2_2S` | 1P → 2S | 10 s |
| `S05_G3_3S` | 1P → 3S | 10 s |
| `S05_G4_4S` | 1P → 4S | 10 s |
| `S06_G1_1P` | 1P → 1S | 10 s |
| `S06_G2_2P` | 2P → 1S | 10 s |
| `S06_G3_3P` | 3P → 1S | 10 s |
| `S06_G4_4P` | 4P → 1S | 10 s |
| `S07_G1_1P1S` | 1P/1S | 8 s |
| `S07_G2_2P2S` | 2P/2S | 8 s |
| `S07_G3_3P3S` | 3P/3S | 8 s |
| `S07_G4_4P4S` | 4P/4S | 8 s |

> `S05_G1_1S`、`S06_G1_1P`、`S07_G1_1P1S` 与图 1 是同一条件，**只跑一次即可**（此处重复列出只为说明用途）。

### 图 6（第 16 页）· 长稳漂移 —— 需 1 个条件 + 逐样本

| condition_id | 参数 | duration | 额外要求 |
|---|---|---|---|
| `S08_G2_1KiB_1000Hz` | 1P/1S、1024 B、1000 Hz | **100 s** | **必须保留逐样本二进制**（见第四节） |

### 汇总

| 场景 | 需要的组 | 个数 |
|---|---|---|
| S01 | G3 | 1 |
| S02 | G1 G2 G3 G4 G5 G6 | 6 |
| S03 | G1 G2 G3 G4 G5 | 5 |
| S04 | G2 | 1 |
| S05 | G1 G2 G3 G4 | 4 |
| S06 | G1 G2 G3 G4 | 4 |
| S07 | G1 G2 G3 G4 | 4 |
| S08 | G2 | 1 |
| **合计** | | **26** |

**可以跳过的 9 个**（这五张图用不到）：
`S01_G1_100Hz`、`S01_G2_500Hz`、`S01_G4_2000Hz`、`S04_G1_100Hz`、`S04_G3_5000Hz`、`S04_G4_10000Hz`、`S08_G1_1KiB_100Hz`、`S08_G3_64KiB_100Hz`、`S08_G4_1MiB_5Hz`

预计耗时：约 **15 分钟**（`repeats=1`）。

---

## 二、统一参数（配置文件已写好，无需改动）

`songfei/test/DDS/matrix_s01_s08.yaml` 顶部已定义，对全部条件统一生效：

| 项 | 值 |
|---|---|
| `transport_mode` | `udp`（UDPv4，显式指定，不与共享内存混测） |
| `qos_profile` | `reliable` |
| reliability / durability | `reliable` / `volatile` |
| history | `keep_last`，`history_depth: 1024` |
| publish_mode | `synchronous`，`max_blocking_time_ms: 1000` |
| `disable_data_sharing` | `true` |
| `wait_for_acknowledgments` | `true` |
| `network_profile` | `normal`（**不注入网络损伤**） |
| `warmup_seconds` | 1.0 |
| `drain_seconds` | 2.0（与三家一致；1.0 s 会误记尾部在途消息为丢包） |
| `timeout_seconds` | 120 |
| `repeats` | 1 |

> 这三处数值必须与三家保持一致：`warmup=1.0`、`drain=2.0`、`repeats=1`。四家并表时才可比。

---

## 三、三个高风险档位（重点）

以下档位在旧结果里出现过异常，**本次必须产出可用数据**，否则图中会出现缺口：

| 档位 | 旧结果的问题 | 处理建议 |
|---|---|---|
| `S08_G2_1KiB_1000Hz` | 延迟 624 ms、丢包 6.4% | 若仍异常，按 DDS 侧建议调整：提高 `history_depth`、启用 flow controller、`publish_mode: asynchronous`、增大接收队列 |
| `S05_G3_3S` / `S05_G4_4S` | 10.5 ms / 388 ms | 同上（writer cache / reader 匹配压力） |
| `S07_G3_3P3S` / `S07_G4_4P4S` | 29.1 ms / 4.9 ms | 同上 |

**一致性要求**：如果为通过某个档位而调整了 QoS，**该场景内的所有档位必须用同一套 QoS 重跑**，并在交付说明里记录改了什么。不要只改高负载那一档 —— 否则同一张图里不同点来自不同配置，无法比较。

**判定"可用"的标准**：延迟在同场景低档位的同一量级内（不出现数量级跃升），且 `final_packet_loss` ≈ 0。

---

## 四、图 6 额外要求：逐样本二进制

图 6 画的是"延迟随时间变化"，需要**每条消息的时间戳**，不是聚合值。DDS 的落盘方式已具备：

- 每次运行会生成 `subscriber_0.receive.bin`（原始接收记录，含 `sequence_number` / `send_timestamp_ns` / `receive_timestamp_ns` / `flags`）
- 对应的 `subscriber_0.result.json` 里有 `raw_file` 与 `raw_record_count`

**唯一要求：把 `raw/<suite_id>/` 整个目录一起交付**（`.bin` 不要删、不要只拷 `result.json`）。

若希望额外提供可读格式，可导出 CSV：

```powershell
.venv\Scripts\python.exe launch.py export-raw ^
  --input results\matrix_s01_s08_v2\raw\<suite_id>\<run_dir>\subscriber_0.receive.bin ^
  --output S08_G2_latency_samples.csv
```

（CSV 与 `.bin` 二选一即可，`.bin` 我也能直接读。）

---



---

## 六、交付物

```
results\matrix_s01_s08_v2\
├─ result.json                  ← suite 汇总（含全部 26 条 run 指标）
└─ raw\
   └─ <suite_id>\
      └─ <run_dir>\             ← 每个条件一个目录，含：
         ├─ subscriber_0.receive.bin      ← 逐样本（图 6 必需，务必保留）
         ├─ subscriber_0.result.json
         ├─ publisher_0.send.bin
         ├─ condition.json
         └─ .log
```




---

## 七、验收自检（交付前请核对）

对照第一节的表，逐项确认：

- [ ] **26 个条件全部 `status = passed`**，无失败、无跳过
- [ ] `S08_G2_1KiB_1000Hz` 的 `duration` 是 **100 s**（不是 300 s）
- [ ] `S03_G1`~`S03_G4` 的 `publish_rate_hz` 是 **20**（不是 0）
- [ ] `S03_G5_1MiB_5Hz` 的 `publish_rate_hz` 是 **5**、`duration_seconds` 是 **40**
- [ ] S02 存在 **6 个** 条件（1 / 4 / 16 / 32 / 48 / 64 KiB）
- [ ] S05 / S06 / S07 各 **4 个** 条件
- [ ] 所有条件的 `warmup_seconds = 1.0`、`drain_seconds = 2.0`、`repeats = 1`
- [ ] `raw\<suite_id>\` 目录完整，`subscriber_0.receive.bin` 未缺失
- [ ] 用 `--condition` 过滤后，`result.json` 里 **恰好只有 26 条 run**（不多不少）

---

## 八、我这边收到后的对接动作（无需你处理）

1. 把新 suite 放到 `songfei/test/DDS/results/` 下 —— 绘图脚本会**自动合并**，同一条件取 `test_start_time` 更晚的那次，旧的旧配置结果自动被覆盖
2. 解除图 1 的"仅三家"限制，把 DDS 重新纳入
3. 按第三节结果更新 `DDS_TRUSTED` 白名单（确认可用后放开 S05 G3/G4、S07 G3/G4）
4. 为图 6 新增读 `subscriber_0.receive.bin` 的逐样本 loader（按 `flags` 过滤有效记录，延迟 = `(receive_timestamp_ns − send_timestamp_ns) / 1e6`）
5. 重出六张图并核对：每张图里 DDS 的曲线覆盖与三家**完全相同的档位**

---

## 附：五张图与所需条件的对应关系

| PPT 页 | 图 | DDS 需要的条件数 | 对应条件 |
|---|---|---|---|
| 第 12 页 | `compare_repeatability.png` | 6 | S01G3, S04G2, S05G1, S06G1, S07G1, S08G2 |
| 第 13 页 | `compare_S02_payload_knee.png` | 6 | S02 G1–G6 |
| 第 14 页 | `compare_S03_large_message_tail.png` | 5 | S03 G1–G5 |
| 第 15 页 | `compare_S05_S07_topology.png` | 12 | S05 G1–G4, S06 G1–G4, S07 G1–G4 |
| 第 16 页 | `compare_S08_drift.png` | 1（+逐样本） | S08 G2 |
