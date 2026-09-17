# DDS 追加要求（第二轮）· 两件事

上一轮交付的 `dds_26_results.json` 已核对通过：`suite_id = 2026-09-16T16_40_31_733Z_p22724`，26 条全部 `passed`，参数与三家矩阵逐条一致，已经画进图 1 / 图 2 / 图 3 / 图 4。

**还差两件，都不需要重跑全部 26 个条件。**

---

## 事项一 · 补交一个 run 目录（图 6 用）

### 需要什么

**`S08_G2_1KiB_1000Hz` 那一次运行的原始目录**，因为图 6（长稳漂移）要按时间分桶，需要**逐样本时间戳**，汇总值推不出来。

### 精确路径

在跑出这份数据的机器上：

```
<运行目录>\outputs\raw\2026-09-16T16_40_31_733Z_p22724\
    2026-09-16T16_46_54_642Z_2026-09-16T16_40_31_733Z_p22724_S08_G2_1KiB_1000Hz_r1\
```

（`<运行目录>` 就是当时执行 `launch.py run` 时所在的那个目录）

### 请交付该目录下的

```
subscriber_0.receive.bin      ← 必需（逐样本：sequence_number / send_timestamp_ns / receive_timestamp_ns）
subscriber_0.result.json      ← 建议一起（含 raw_record_count，便于核对条数）
```

整个 run 目录一起打包最省事，其它文件我也能用上。

### 如果那个目录已经不在了

重跑一次这一档，输出到新目录，然后把**整个新输出目录**给我（它下面会自动生成 `raw/<suite_id>/<run_dir>/`）：

```powershell
.venv\Scripts\python.exe launch.py run --config matrix_s01_s08.yaml ^
  --condition S08_G2_1KiB_1000Hz ^
  --output results\s08_g2_with_raw
```

交付 `results\s08_g2_with_raw\` 整个目录（含 `result.json` 与 `raw\`）。

> 提示：仓库里 `DDS/.build/rep2_1/artifacts/` 下有旧的 `.bin`，但那是 9-15 号**旧配置**（300 s、条件名不同）跑的，**不能混用**，请不要拿它来顶替。

---

## 事项二 · 重跑 `S05_G3_3S`（图 4 用）

### 为什么

该档位的实测值与同场景相邻档位差约 **500–700 倍**：

| S05 档位 | 拓扑 | 延迟均值 | P95 | 丢包 |
|---|---|---|---|---|
| `S05_G1_1S` | 1P → 1S | 0.284 ms | 0.383 ms | 0 |
| `S05_G2_2S` | 1P → 2S | 0.336 ms | 0.463 ms | 0 |
| **`S05_G3_3S`** | **1P → 3S** | **206.3 ms** ⚠️ | **702.2 ms** ⚠️ | 0 |
| `S05_G4_4S` | 1P → 4S | 0.433 ms | 0.601 ms | 0 |

### 一个重要线索：坏档位在两次运行之间"换了位置"

| | 上一批（旧配置） | 本批（9-16） |
|---|---|---|
| `S05_G3_3S` | 10.5 ms（异常） | **206.3 ms（异常）** |
| `S05_G4_4S` | **388 ms（异常）** | 0.433 ms（正常） |

本批**负载更重的 4S 完全正常，反而是 3S 异常**。这说明它更像是**偶发干扰**（后台进程、调度抖动、队列瞬时积压），而不是 3 个订阅者带来的确定性瓶颈。

**因此第一步请先用原配置原样重跑一次**，很可能就正常了。

### 第一步：原样重跑（不改任何参数）

```powershell
.venv\Scripts\python.exe launch.py run --config matrix_s01_s08.yaml ^
  --condition S05_G3_3S ^
  --output results\s05_g3_retry
```

**判定合格的标准**：`latency_ms` 落在 **0.3–0.5 ms** 量级（与 G2 / G4 同级），且 `final_packet_loss = 0`、`status = passed`。

### 第二步：若仍异常，再按顺序调 QoS（每次只改一项）

当前 `matrix_s01_s08.yaml` 的 reliable 档是：`history_depth: 1024`、`publish_mode: synchronous`、`disable_data_sharing: true`、`max_blocking_time_ms: 1000`、`wait_for_acknowledgments: true`。

按下面顺序尝试，**每次只改一项**并记录改了什么、结果如何：

1. `publish_mode: asynchronous`（发布端非阻塞）
2. 提高 `history_depth`（例如 4096）
3. 启用 flow controller
4. 增大接收端队列

### ⚠ 一致性要求（重要）

**如果为了通过这一档而改了 QoS，那么 `S05` 这个场景的四个档位必须用同一套 QoS 全部重跑**：

```powershell
.venv\Scripts\python.exe launch.py run --config matrix_s01_s08.yaml ^
  --condition S05_G1_1S --condition S05_G2_2S --condition S05_G3_3S --condition S05_G4_4S ^
  --output results\s05_all_retry
```

理由：图 4 的 S05 那一列是同一条曲线，四个点必须来自同一配置，否则曲线形状没有意义。

同时请在交付说明里写明：**改了哪一项、改成什么、四个档位是否都用了这套配置**。

---

## 交付清单

| # | 交付物 | 用途 |
|---|---|---|
| 1 | `S08_G2_1KiB_1000Hz` 的 run 目录（含 `subscriber_0.receive.bin`）**或** 重跑产出的 `results\s08_g2_with_raw\` 整个目录 | 图 6 |
| 2 | `S05_G3_3S` 重跑产出的 suite 目录（`result.json` + `raw\`） | 图 4 |
| 3 | 若走了第二步：一句说明"改了哪项 QoS、S05 四档是否统一" | 报告口径 |

**用新目录，不要覆盖** `results\matrix_s01_s08_v2`。我这边读取时会自动取 `test_start_time` 更晚的那条，新的覆盖旧的，不需要你处理合并。

---

## 验收自检

- [ ] `S05_G3_3S` 的 `latency_ms` 回到 0.3–0.5 ms 量级，`final_packet_loss = 0`
- [ ] 若改了 QoS：S05 的 **四个** 档位都在同一份新 suite 里，且参数仍是 1024 B / 1000 Hz / duration 10 s
- [ ] `S08_G2` 的 run 目录里 `subscriber_0.receive.bin` 存在，且 `subscriber_0.result.json` 的 `raw_record_count` 约为 10 万（100 s × 1000 Hz）
- [ ] 交付的新 suite 里**只有**这次重跑的条件（不要顺手把 26 个全跑一遍）

---

## 我收到后会做什么

1. `S05_G3` 通过 → 从 `DDS_ANOMALOUS` 里移除，图 4 的 S05 列补满（当前在 3S 处有一个缺口）
2. `S08_G2` 的 `.bin` 到位 → 给 `plot_diff.py` 加 DDS 逐样本读取，图 6 从三家变四家
3. 重出五张图并与三家逐档核对覆盖是否完全对齐
