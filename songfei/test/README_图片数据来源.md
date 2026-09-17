# PPT 图片数据来源与绘制说明

本文说明**汇报 PPT 里那五张图**的数据来自哪个文件、取哪些字段、以及脚本是怎样把它们画出来的。

- 绘图脚本：`songfei/test/plot_diff.py`
- 一键生成：`python songfei/test/plot_diff.py`
- 数据根目录：`songfei/test/`（脚本内的 `DATA_ROOT`，即脚本自身所在目录）

---

## 0. 总览：五张图 → 数据源

| PPT 页 | 图片文件 | 数据粒度 | 数据源 |
|---|---|---|---|
| 第 12 页 | `compare_repeatability.png` | 组级汇总 | 四家 `summary.json` + DDS suite |
| 第 13 页 | `compare_S02_payload_knee.png` | 组级汇总 | 三家 `summary.json` + DDS suite |
| 第 14 页 | `compare_S03_large_message_tail.png` | 组级汇总 | 四家 `summary.json` + DDS suite |
| 第 15 页 | `compare_S05_S07_topology.png` | 组级汇总 | 四家 `summary.json` + DDS suite |
| 第 16 页 | `compare_S08_drift.png` | **逐样本** | 三家原始逐样本产物 + DDS `subscriber_0.receive.bin` |

> 说明：另有一张 `compare_S08_stability.png`（S08 四档横截面）为备用图，**未放进 PPT**。

---

## 1. 两类数据源的结构

### 1.1 三家（VSOA / MQTT / Zenoh）：`<中间件>/summary.json`

路径：

```
songfei/test/vsoa/summary.json
songfei/test/MQTT/summary.json      ← Windows 下大小写不敏感，脚本按 "MQTT" 拼路径
songfei/test/zenoh/summary.json
```

结构：顶层是一个字典，**键为 `<场景>-<组号>`**，值为该组的指标行。例如：

```json
{
  "S01-3": {
    "scenario": "S01",
    "group": 3,
    "label": "1000Hz",
    "factor": "publish_rate_hz",
    "status": "completed",
    "parameters": {
      "payload_size_bytes": 1024,
      "publish_rate_hz": 1000,
      "publisher_count": 1,
      "subscriber_count": 1,
      "duration_seconds": 10.0,
      "warmup_seconds": 1.0,
      "drain_seconds": 2.0,
      "repeats": 1
    },
    "latency_ms": 0.1563,
    "latency_p95_ms": 0.2164,
    "latency_p99_ms": 0.3286,
    "latency_std_ms": 0.0501,
    "jitter_ms": 0.0264,
    "throughput_mbps": 8.193,
    "offered_throughput_mbps": 8.192,
    "packet_loss": 0.0,
    "final_packet_loss": 0.0,
    "cpu_percent": 102.81,
    "memory_mb": 80.69
  }
}
```

**绘图只认 `status == "completed"` 的行**；其余状态一律取 `None`（画成断线，而不是补 0）。

对应脚本函数：

- `load_three(name)` —— 直接读 `summary.json`
- `get_metric(sources, name, scenario, group, field)` —— 按 `f"{scenario}-{group}"` 取字段，并校验 `status`

### 1.2 DDS：`DDS/results/` 下的 suite JSON

路径：`songfei/test/DDS/results/matrix_s01_s08`（**无扩展名的文件**，就是 suite 结果本体）

脚本的扫描规则（`load_dds()`）：

1. 遍历 `DDS/results/*`
2. 若是文件 → 直接当 JSON 读；若是目录 → 读其下的 `result.json`
3. 只接受含 `runs` 数组的文档
4. 对 `runs[]` 里每一条，把 `condition_id` 解析成 `(场景, 组号)`

`condition_id` 形如 `S01_G3_1000Hz`、`S02_G4_64KiB`，解析规则为按 `_` 切分后取第 0 段与第 1 段（`G` 后面的数字）：

```python
def _parse_condition(condition_id):     # "S01_G3_1000Hz" -> ("S01", 3)
    parts = str(condition_id or "").split("_")
    ...
    return parts[0].upper(), int(parts[1][1:])
```

同一条件若出现在多份 suite 中（例如后续补测产出新 suite），**取 `test_start_time` 更晚的那次**，便于新数据自动覆盖旧数据。

DDS run 的关键字段直接位于条目顶层（不像三家那样嵌在 `parameters` 里）：

```
payload_size_bytes / publish_rate_hz / publisher_count / subscriber_count
latency_ms / latency_p95_ms / latency_p99_ms / latency_std_ms / jitter_ms
throughput_mbps / cpu_percent / memory_mb / status
```

注意状态值：**DDS 用 `"passed"`，三家用 `"completed"`**，`get_metric()` 里两者都接受。

### 1.3 DDS 白名单（`DDS_TRUSTED`）

DDS 侧只有部分条件进入对比。白名单写在脚本里，逐条依据见代码注释（来源为 `DDS/结果评价.txt` 的判定）：

| 场景 | 纳入的组 |
|---|---|
| S01 | 1、2、3 |
| S02 | 1、2、3、4 |
| S04 | 1、2 |
| S05 | 1、2 |
| S06 | 1、2、3、4 |
| S07 | 1、2 |
| S08 | 1、3 |

**不在白名单 ⇒ `get_metric()` 返回 `None` ⇒ 图上断线**（不是缺失，是判定不可用）。
S03 整场不入图，原因另有：其速率参数（unpaced）与三家的固定 20 Hz 不同。

---

## 2. 逐图说明

### 图 1 · `compare_repeatability.png`（PPT 第 12 页）

**画的是什么**：同一条件（1P/1S、1 KiB、1000 Hz）在 6 个场景里各测一次，横向比较其延迟均值与 P99。

**数据源**：三家 `summary.json`，取这 6 个键：

```python
REPEAT_KEYS = [("S01", 3), ("S04", 2), ("S05", 1), ("S06", 1), ("S07", 1), ("S08", 2)]
```

**取用字段**：`latency_ms`（左图）、`latency_p99_ms`（右图）

**绘制**（`figure_repeatability()`）：

1. `x = 0..5`，x 轴刻度为 `S01-3 / S04-2 / …`，**顺序＝上表顺序**（这 6 个条件的参数相同，顺序不影响结论）
2. 每家一条折线 + 6 个散点；再用 `fill_between` 画出该家的 min–max 区间（图中半透明带）
3. 右图 y 轴取对数（`set_yscale("log")`），因为 P99 的量级差可达数倍
4. 图例里附该家的"极差%"，由 `spread_percent()` 计算：
   `(max − min) / mean × 100`
5. 底部结论横幅：逐场景检查"在场各家的排序"是否与脚本里的固定顺序（`vsoa → mqtt → zenoh`）一致，输出 `一致数/总场景数`
6. 本图**不含 DDS**（`plotted = [n for n in names if n != "dds"]`），标题中已标明覆盖三家

### 图 2 · `compare_S02_payload_knee.png`（PPT 第 13 页）

**画的是什么**：S02 消息大小阶梯（1 / 4 / 16 / 32 / 48 / 64 KiB @100 Hz），重点看 48→64 KiB 之间的分片阈值。

**数据源与取组顺序**（关键：组号顺序 ≠ payload 顺序）

```python
S02_GROUPS = [1, 2, 3, 5, 6, 4]      # → 1 / 4 / 16 / 32 / 48 / 64 KiB
S02_TICKS  = ["1", "4", "16", "32", "48", "64"]
```

组 5（32 KiB）与组 6（48 KiB）是后补的条件，组号大于组 4（64 KiB），因此**必须显式排序**，否则曲线会来回折。

- 三家：`summary.json` 的 `S02-1 / S02-2 / S02-3 / S02-5 / S02-6 / S02-4`
- DDS：suite 的 `S02_G1_1KiB / G2_4KiB / G3_16KiB / G4_64KiB` → 对应组 1/2/3/4；**组 5、6 无数据 → 断线**

**取用字段**：延迟子图 `latency_ms` + `latency_p95_ms`；另三格为 `latency_p99_ms`、`cpu_percent`、`memory_mb`

**绘制**（`figure_s02_knee()`）：

1. 2×2 子图；x 轴为 6 个**等距**刻度（底部标注真实 KiB 值）
2. 在 `KNEE_X = 4.5` 处画竖直点线 —— 下标 4/5 之间正好是 48 与 64 KiB，即 **VSOA 的 `fragment_size_bytes = 60000` 阈值**所在区间
3. P99 子图取对数轴
4. 在 48→64 处标注跳幅：均值标 `+x%`，P99 标 `×n`
5. 底部结论横幅由 `get_metric(..., "S02", 6/4, "latency_ms")` 现算，不写死数字

### 图 3 · `compare_S03_large_message_tail.png`（PPT 第 14 页）

**画的是什么**：S03 五档（64 / 128 / 256 / 512 KiB @20 Hz + 1 MiB @5 Hz）的吞吐与尾部代价。

**数据源**：三家 `summary.json` 的 `S03-1 … S03-5`

**取用字段**：`throughput_mbps`、`latency_ms`、`latency_p95_ms`、`latency_p99_ms`

**绘制**（`figure_s03_large()`）：

1. 2×2 子图：吞吐 / 延迟（均值+P95）/ P99（对数）/ **P99 ÷ P95**（尾部代价）
2. 吞吐子图叠加"目标吞吐"虚线，由 `target_mbps()` 计算：

   ```
   目标吞吐 = payload × 8 × 速率 × publisher_count × subscriber_count / 1e6
   ```

   参数来自 `params_of()`：优先取三家 `summary.json` 里的 `parameters`，取不到再退回 DDS run 的顶层字段
3. P99÷P95 子图加 `y = 1` 参考线
4. 底部结论横幅取 1 MiB 档（组 5）的 P99/P95

### 图 4 · `compare_S05_S07_topology.png`（PPT 第 15 页）

**画的是什么**：三种拓扑 × 三个指标。

**列（场景）与行（指标）**

| | 第 1 列 | 第 2 列 | 第 3 列 |
|---|---|---|---|
| 场景 | **S05 扇出 1P→N×S** | **S06 汇聚 N×P→1S** | S07 并发 N×P→N×S |
| 组 1/2/3/4 | 1S / 2S / 3S / 4S | 1P / 2P / 3P / 4P | 1×1 / 2×2 / 3×3 / 4×4 |

| 行 | 字段 |
|---|---|
| 行 1 | `latency_ms` |
| 行 2 | `memory_mb` |
| 行 3 | `cpu_percent` |

**取组方式**：`keys = [(scenario, g) for g in (1, 2, 3, 4)]`

**绘制**（`figure_topology()`）：

1. 3×3 子图
2. 每格 x 轴为 4 个端数刻度
3. 行 1 的 x 轴标签同时写出该场景各档的**应发吞吐**（`target_mbps()` 算得），因为吞吐本身不再单独占版面
4. DDS 现覆盖全部档位（S05 的 1S–4S、S06 的 1P–4P、S07 的 1×1–4×4）

### 图 6 · `compare_S08_drift.png`（PPT 第 16 页）

**画的是什么**：S08 组2（1 KiB @1000 Hz，100 s）的延迟**随时间**的变化，按 10 s 分桶。

**这是唯一读「逐样本」的图**，数据源不是 `summary.json`：

| 中间件 | 逐样本位置 | 字段 |
|---|---|---|
| VSOA | `vsoa/S08/group2.json` → `artifacts_directory` → 该目录下 `subscriber-0.latencies_ms.f64` | 二进制 `float64`（`np.fromfile(dtype="<f8")`） |
| MQTT | `MQTT/S08/group2.json` → `samples_path` → 该文件的 `latencies_ms` | JSON 数组 |
| Zenoh | `zenoh/S08/group2.json` → `samples[]` | 每条取 `.latency` |

读取逻辑**直接复用** `plot_compare.py` 的 `LOADERS`：

```python
import plot_compare as pc
info = pc.LOADERS[name]("S08", 2)     # → {"latencies", "res_times", "cpu", "mem", ...}
```

> VSOA 特别说明：其 `subscriber-0.result.json` 里的 `latencies_ms` 受 `sample_capacity = 10000` 限制（S08-2 实收 99954 条，JSON 里只有 1 万条），完整序列在同目录的 `.f64` 文件里。脚本优先读 `.f64`；只用 JSON 会把这 1 万条摊到整条时间轴上，曲线失真。

**分桶**：同样复用 `plot_compare.binned()`：

```python
stats = pc.binned(latencies, window=100.0, bin_seconds=10.0)
# → {"centers", "mean", "p95", "max", "count"}
```

分桶假设：**逐样本按测量窗口均匀铺开**（各家样本不带统一墙钟时间戳），第 i 个样本的时间为
`window × (i + 0.5) / N`。

**绘制**（`figure_s08_drift()`）：

1. 版式**上二下一**：上排「每桶均值 + 每桶 P95」并排，左下「每桶最大值」，右下角**只放漂移汇总文字**（`axis("off")`），不放第四张图 —— 三格排成 2×2 时留一个空洞，整块图看起来像少画了一格
2. 画布 **15×9.5 in → 比例 1.579**，与 PPT 第 16 页的图片框（8.26×5.23 in）完全一致；改版式时不能动这个比例，否则替换进 PPT 会被拉伸
3. 边距直接写在 `add_gridspec(left/right/top/bottom)` 上，**不走 `tight_layout`** —— 右下角那格是纯文字，`tight_layout` 会把它一起参与排版，把三个面板挤变形
4. 漂移值 = **后 1/3 桶均值的平均 ÷ 前 1/3 桶均值的平均 − 1**，用与画图同一份样本计算，避免口径分叉
5. 本图画 VSOA / MQTT / Zenoh / DDS 四家（DDS 逐样本来自其原始二进制，`plot_compare` 的 loader 不认这种格式）
6. **原本的第 4 格 CPU 面板已删除**：逐采样资源只有 VSOA 与 MQTT 落盘，四家里只有两家能画，信息量不足且容易被误读成"另两家没测"

---

## 3. 全图共用约定

| 项 | 约定 |
|---|---|
| 颜色 | `vsoa=#1f77b4` `mqtt=#d62728` `zenoh=#2ca02c` `dds=#9467bd` |
| 中文 | `matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]` |
| 取数原则 | 只取 `status` 为 completed / passed 的组；缺失一律 `None` → **断线，不补 0、不插值** |
| 底部脚注 | 每张图都标注测量参数（`repeats` / `drain_seconds`）与数据来源声明 |
| 输出 | `songfei/test/compare_*.png`，`dpi=150` |

**参数取值辅助函数**（图 2/3/4 都用）：

```python
params_of(sources, scenario, group)   # 优先 summary 的 parameters，退回 DDS run 顶层字段
target_mbps(sources, keys)            # payload × 8 × rate × P × S / 1e6
```

---

## 4. 如何重新生成

```powershell
cd D:\AI_project\yihui_shixi

python songfei/test/plot_diff.py                      # 六张图全出（含备用图 5）
python songfei/test/plot_diff.py --only repeatability # 只出某一张
python songfei/test/plot_diff.py --only knee
python songfei/test/plot_diff.py --only large
python songfei/test/plot_diff.py --only topology
python songfei/test/plot_diff.py --only stability     # 备用图，未进 PPT
python songfei/test/plot_diff.py --only drift

python songfei/test/plot_diff.py --middlewares vsoa,mqtt,zenoh   # 不画 DDS
```

`--only` 可选值：`repeatability / knee / large / topology / stability / drift`
默认输出目录为脚本所在目录（`songfei/test/`），可用 `--out-dir` 覆盖。

---

## 5. 上游数据是怎么产生的（便于追溯）

图里的组级数据不是手写的，来自矩阵测试脚本 `songfei/test/run_matrix.py`：

```powershell
# 三家全量（每场景 4–6 组）
python songfei/test/run_matrix.py --middleware all --scenario S01-S08 --groups 1-6

# 只补测新增组（S02 组5/6 = 32/48 KiB；S03 组5 = 1 MiB@5Hz）
python songfei/test/run_matrix.py --middleware all --scenario S02-S03 --groups 5,6
```

- 每组结果落盘为 `<中间件>/<场景>/group<N>.json`，同时汇总进 `<中间件>/summary.json`
- 每组带参数指纹（`_matrix.fingerprint`），参数变化会触发重跑，未变化的组重跑时自动跳过
- DDS 侧由 `songfei/test/DDS/matrix_s01_s08.yaml` 定义同名条件，产出 suite JSON 到 `DDS/results/`

因此完整的数据链是：

```
run_matrix.py（三家） ┐
                      ├─→ <中间件>/summary.json ─┐
DDS launch.py         ┘   DDS/results/<suite>    ├─→ plot_diff.py ─→ compare_*.png
                                                 │
<中间件>/<场景>/group<N>.json → artifacts/*.f64 ─┘（仅图 6 走这条逐样本路径）
```
