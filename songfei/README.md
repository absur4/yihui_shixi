# Songfei 统一通信性能实验台（CONSOLE 2.0）

以 `example.html` 为原型的统一可视化控制台，当前已**完整接入 VSOA**：S01–S12 全部标准条件
（37 个条件 × 各自重复轮次）均由 `VSOA/vsoa_py/standalone` 真实测量引擎执行，15 项指标
（延迟、P95/P99、吞吐、抖动、丢包与恢复、CPU、内存、启动、发现、重复/乱序/损坏等）
全部来自真实测量，不生成任何模拟数据。

启动：

```powershell
python songfei/run.py
```

浏览器打开 `http://127.0.0.1:8787/`：

- 左侧按 `example.html` 的参数体系配置：payload、速率、发布者/订阅者数量、消息数/时长、
  传输模式（TCP/UDP）、QoS、重复轮次、随机种子、网络条件（延迟/抖动/丢包，经真实 UDP 代理注入）、
  预热/排空/超时。
- **开始回环测试**：用当前表单参数运行所选条件（任意参数可改，后端按引擎规则校验并给出中文错误提示）。
- **运行此场景完整矩阵**：忽略表单修改，按标准参数运行该场景全部条件。
- 右侧实时展示：指标卡（延迟/P95/P99/吞吐/丢失）、延迟样本曲线（取自 subscriber-0 原始样本）、
  统一指标表、运行日志；每轮结果可单独切换查看，"查看完整结果"显示引擎原始 JSON。
- 实验产物保存在 `songfei/results/vsoa/<job_id>/`（`job.json`、`spec.json`、`console.log`、
  `output/result.json`、`output/runs/`、`output/artifacts/`），重启服务后仍可在历史记录中回看，
  并可打开完整报告页 `/files/vsoa/<job_id>/report.html`。

## 架构

```
songfei/app.py          控制台 HTTP 服务（标准库实现，实现 example.html 的 API 契约）
songfei/vsoa_runner.py  引擎子进程执行器（调用 standalone.engine.run_suite，可整体终止）
songfei/static/index.html  前端（复制自 example.html，默认中间件改为 vsoa，会话 token 由服务注入）
songfei/results/        实验产物（按中间件/作业分目录）
```

API 契约（供其他中间件后续接入参考）：

- `GET /api/init` → `{middleware:[...], catalogs:{...}, names:{...}, history:[...]}`
- `GET /api/state` → `{job, logs}`
- `GET /api/history?middleware=`、`GET /api/results?middleware=&job=[&run=]`
- `POST /api/start`（`{middleware, template_index, configuration, matrix}`）
- `POST /api/stop`、`POST /api/shutdown`（请求头需 `X-MQTT-Token`）
- `GET /files/<middleware>/<job>/report.html`

接入新中间件时：实现与 VSOA 相同的“条件目录 + 作业执行器”结构即可复用整套前端。
