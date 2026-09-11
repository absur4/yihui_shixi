# 依赖清单

本文按用途区分项目依赖。版本号与当前已验证环境一致，便于复现。

## 环境要求

- 源码运行：Python 3.10 或更高版本。
- 独立交付包构建：Windows x64、Python 3.10 或更高版本。
- 已生成的 `release/vsoa_win64_v1.3.zip`：目标用户无需安装 Python 或下列 Python 包。
- 仪表盘自动验收：Windows Edge；Playwright 默认复用本机 Edge，不要求把浏览器打进交付包。

## 运行时依赖

由 `requirements.txt` 管理：

| 包 | 固定版本 | 用途 |
| --- | --- | --- |
| `vsoa` | 1.0.4 | VSOA 通信、服务发现与数据收发 |
| `psutil` | 7.2.2 | 进程管理、CPU 和内存采样 |
| `PyYAML` | 6.0.3 | 读取独立模块的 YAML 配置 |

安装命令：

```powershell
python -m pip install -r requirements.txt
```

## 构建依赖

由 `requirements-build.txt` 管理。它会先安装全部运行时依赖，再安装：

| 包 | 固定版本 | 用途 |
| --- | --- | --- |
| `pyinstaller` | 6.22.2 | 将独立模块构建为 Windows x64 单文件程序 |

安装命令：

```powershell
python -m pip install -r requirements-build.txt
```

PyInstaller 会自动解析并安装 `altgraph`、`packaging`、`pefile`、`pyinstaller-hooks-contrib`、`pywin32-ctypes` 和 `setuptools` 等传递依赖，不应手工重复固定到项目清单中。

## 开发与可选验收依赖

由 `requirements-dev.txt` 管理。它包含全部构建依赖，并增加：

| 包 | 固定版本 | 用途 |
| --- | --- | --- |
| `playwright` | 1.62.0 | 自动检查离线仪表盘；仅 `accept_dashboard.py` 使用 |

安装命令：

```powershell
python -m pip install -r requirements-dev.txt
```

如机器上没有可供测试的 Edge，可另外执行 `python -m playwright install chromium`。这会下载浏览器运行时，不属于项目上传内容。

## 标准库与前端

- 其余 Python import 均来自标准库或本项目的 `bench`、`standalone` 包，无需通过 pip 安装。
- Web UI 使用原生 HTML/CSS/JavaScript 和 Python 标准库 HTTP 服务，无 Node.js、npm 或前端构建依赖。

## 清单关系

```text
requirements.txt
└── requirements-build.txt（追加 PyInstaller）
    └── requirements-dev.txt（追加 Playwright）
```

以上箭头表达“后者通过 `-r` 包含前者”。
