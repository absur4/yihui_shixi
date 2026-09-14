# Songfei 统一通信性能实验台（CONSOLE 2.0）

以 `example.html` 为原型的统一可视化控制台。控制台本身**不包含任何中间件逻辑**，
它按固定目录顺序扫描各中间件模块的 `adapter.py` 并自动注册：

```
vsoa → dds → mqtt → zenoh        （目录名大小写均可，必须有 adapter.py 才会被注册）
```

启动：

```powershell
python songfei/run.py
```

浏览器打开 `http://127.0.0.1:8787/`。终端会打印已加载的中间件与条件数量，例如：

```
统一通信性能实验台: http://127.0.0.1:8787/
  已加载 vsoa: VSOA · 37 个标准条件
  已加载 mqtt: MQTT · 不可用（入口缺失）
```

## 控制台依赖的适配器契约（五个钩子）

| 钩子 | 必需 | 作用 |
|---|---|---|
| `metadata()` | 是 | 中间件元数据与**真实**可用性（`available` 为 false 时控制台显示"入口缺失"并展示 `notes`） |
| `catalog()` | 是 | S01–S12 全部标准条件（UI 输入字段，见根 README §5） |
| `build_cases(template_index, configuration, matrix)` | 是 | 把"当前表单参数"或"完整矩阵"展开成引擎可执行条件（`matrix=True` 时忽略表单，返回该场景全部标准条件） |
| `base_config()` | 否 | 引擎全局配置，写入 `spec.json` 的 `config` |
| `runner_command()` | 是 | 启动执行器的命令行（可指定专用解释器，如中间件私有 venv） |

同时兼容根 README §4 的写法（`create_adapter()` / `Adapter` 类，含 `run()`）。
加载失败时注册为 `available=false`，控制台仍能正常启动并提示原因。

执行器协议：控制台把 `spec.json` 写到作业目录，再用 `runner_command()` 启动子进程：

```
spec.json = {middleware, config, cases, plan, output, logs}
产物       = output/result.json、output/runs/<run_id>.json、output/artifacts/<run_id>/
日志       = stdout 每行 [HH:MM:SS] …，结束打印 RESULT <path> status=<status>
退出码     = 0 全部完成 / 130 取消 / 其他为失败
```

## 目录

```
songfei/
├─ app.py                 控制台 HTTP 服务（标准库实现；通用适配器加载）
├─ run.py                 启动入口
├─ static/index.html      前端页面（复制自 example.html，会话 token 由服务注入）
└─ results/<中间件>/<job_id>/   实验产物归档（job.json / spec.json / console.log / output/**）
```

当前接入状态：

| 中间件 | 位置 | 状态 |
|---|---|---|
| VSOA | `VSOA/adapter.py` + `VSOA/console_runner.py` | 已完成（参考实现） |
| DDS | `DDS/adapter.py` + `DDS/console_runner.py` | 待实现，要求见 `DDS/DDS_readme_1.md` |
| MQTT | `mqtt/adapter.py` + `mqtt/console_runner.py` | 待实现，要求见 `mqtt/mqtt_readme_1.md` |

新增中间件只改自己目录，不需要改 `songfei/` 或 `interfaces/`。

## API（前端 ↔ 控制台）

- `GET /api/init` → `{middleware:[...], catalogs:{...}, names:{...}, history:[...]}`
- `GET /api/state` → `{job, logs}`
- `GET /api/history?middleware=`、`GET /api/results?middleware=&job=[&run=]`
- `POST /api/start`（`{middleware, template_index, configuration, matrix}`）
- `POST /api/stop`、`POST /api/shutdown`（请求头需 `X-MQTT-Token`）
- `GET /files/<middleware>/<job>/report.html`
