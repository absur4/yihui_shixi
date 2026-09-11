# 文件整理与上传清单

整理日期：2026-09-11。当前主版本由 `standalone/version.py` 定义为 **1.3**。

## 结论

仓库根目录是当前主源码。整理时已删除重复且不同步的内层 `vsoa_py/`，并清除了 `build/`、`results/`、`__pycache__/`、模板运行日志和输出；`.gitignore` 会避免这些内容再次混入上传集合。

根据交付对象选择下列一种上传方式：

1. **上传源码仓库**：上传“源码清单”中的文件，供开发、复现和重新构建。
2. **交付给最终用户**：只上传 `release/vsoa_win64_v1.3.zip` 和对应的 `.sha256`，不要再混入源码或解压目录。

## 源码清单

按下述规则统计，当前源码上传集合共 **72 个文件，约 352.2 KiB**（不含可选的原始需求 PDF）。

### 根目录文件

- 项目说明：`README.md`、`WEBUI_README.md`、`BUILD_AND_ACCEPTANCE.md`、`VALIDATION.md`、`DEPENDENCIES.md`、`UPLOAD_CHECKLIST.md`
- 依赖与忽略规则：`.gitignore`、`requirements.txt`、`requirements-build.txt`、`requirements-dev.txt`
- 批量运行与结果校验：`run_all.py`、`validate_results.py`、`result.schema.json`
- Web UI 启动：`run_webui.bat`、`run_webui.ps1`
- 独立模块构建与入口：`vsoa_module.py`、`build_delivery.py`、`verify_release.py`
- 交付验收：`accept_delivery.py`、`accept_lifecycle.py`、`accept_qualification.py`、`accept_dashboard.py`
- 15 个单项测试：所有根目录下的 `test_*.py`

### 必须上传的目录

- `bench/`：基础性能测试公共实现，只上传 `.py` 文件。
- `standalone/`：v1.3 独立交付模块实现，只上传 `.py` 文件。
- `webui/`：Web 服务、测试目录定义和静态页面。
- `delivery_template/`：交付模板的固定文件；排除 `logs/` 和 `outputs/`。

### 按团队需要上传

- `yihui_shixi/通信中间件统一测试需求文档(1).pdf`：原始需求资料，若团队仓库需要保存需求依据则上传。
- `yihui_shixi/` 下其余占位文件属于四人协作仓库结构，不是 VSOA 程序运行所需文件。

## 最终用户交付清单

| 文件 | 用途 |
| --- | --- |
| `release/vsoa_win64_v1.3.zip` | 已构建、已封包的 Windows x64 独立交付包 |
| `release/vsoa_win64_v1.3.zip.sha256` | ZIP 完整性校验值 |

当前记录的 ZIP SHA-256：

```text
bd65be5a159affd2915c58911c5455ee2be2adaab11485a1112436601c4bb574
```

上传前重新执行 `verify_release.py`；若 ZIP 被重新生成，以上摘要也必须随之更新。

## 不应上传

| 路径或模式 | 原因 |
| --- | --- |
| `.git/` | 本地版本控制元数据，不应出现在手工压缩包中 |
| `.venv/` | 本机虚拟环境，体积大且不可移植 |
| `__pycache__/`、`*.pyc`、`*.pyo` | Python 缓存 |
| `build/`、`dist/`、`*.spec` | 可重新生成的中间构建产物 |
| `results/` | 测试生成数据，当前约 537 MiB，不属于源码 |
| `delivery_template/logs/` | 模板测试日志 |
| `delivery_template/outputs/` | 模板测试输出 |
| `release/vsoa_win64_v1.3/` | ZIP 的解压/暂存目录；交付时上传 ZIP 即可 |
| `vsoa_py/` | 不同步的重复源码副本，容易误用旧版本 |

## 推荐的源码上传结构

```text
vsoa_py/
├── bench/
├── delivery_template/       # 不含 logs/、outputs/
├── standalone/
├── webui/
├── test_*.py
├── accept_*.py
├── *.md
├── requirements*.txt
├── result.schema.json
├── run_all.py
├── run_webui.bat
├── run_webui.ps1
├── vsoa_module.py
├── build_delivery.py
└── verify_release.py
```

## 上传前检查

```powershell
# 检查 Python 文件能否编译
python -m compileall -q -x "(\.venv|build|release|results|vsoa_py)" .

# 检查最终交付包及摘要
.\.venv\Scripts\python.exe verify_release.py

# 查看将被忽略的大目录（需要已安装 Git）
git status --short --ignored
```

- 确认版本均指向 v1.3；若以后升级版本，应同步更新源码常量、文档、目录名、ZIP 名和摘要文件。
- 确认没有口令、令牌、私钥、个人路径或不应公开的测试数据。
- 源码上传和最终用户交付应分开，不要把两种内容混成一个压缩包。
