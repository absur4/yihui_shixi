# 独立模块的构建与复验

当前交付：`release/vsoa_win64_v1.3.zip`；完整交付说明在包内 README。

## 依赖与构建

构建机需要 Windows x64 + Python。**用户运行交付 exe 不需要这些构建依赖。**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe build_delivery.py
```

构建脚本使用 PyInstaller onefile/console 模式，包含解释器、VSOA、psutil、PyYAML 及所需运行库；内置服务通过同一个 exe 的内部工作模式启动。它复制模板、许可证、源码哈希并生成初始 ZIP。生成初始 ZIP 不代表验收已通过。

## 实际 exe 验收

```powershell
.\.venv\Scripts\python.exe accept_delivery.py
.\.venv\Scripts\python.exe accept_lifecycle.py
.\.venv\Scripts\python.exe -m standalone.validate_delivery release\vsoa_win64_v1.3\outputs\result.json --all-scenarios
.\release\vsoa_win64_v1.3\vsoa.exe --suite qualification --output outputs\qualification
.\.venv\Scripts\python.exe accept_qualification.py
```

`accept_delivery.py` 在中文空格目录中运行 exe，隔离 Python 环境变量与 PATH；检查默认 25 轮、多个发布者、极端丢包、配置错误、端口冲突、批处理，并把成功的默认场景结果复制进交付目录。标准套件另有 31 个案例、155 轮，其中 5×300 秒真实长稳，不要与其他负载测试并行运行。

`accept_lifecycle.py` 创建隐藏控制台，发送 Ctrl+C 验证 cancelled 结果；另强制结束控制进程以验证 Windows Job 子进程清理。两项都只操作本次创建的进程。

如长稳资源采样出现一秒以上间断，`accept_qualification.py` 拒绝验收。可在套件停止后运行 `accept_qualification.py --prepare-retry`，完整保留被拒绝原始记录，再以同一 EXE/配置执行完整套件命令加 `--resume`，仅补跑缺失轮次。不要修改数据来绕过校验。

离线页面可选自动化验收需要额外安装开发工具 Playwright，并已有 Windows Edge：

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe accept_dashboard.py release\vsoa_win64_v1.3\outputs\result.json
```

Playwright 仅为开发验收工具，不打进 exe，也不是交付运行依赖。四槽位检查使用仅在内存中的明确 UI 测试 fixture，不代表其他中间件实现。

## 最终封包

验收后更新交付目录的验收说明及报告，再执行：

```powershell
.\.venv\Scripts\python.exe build_delivery.py --finalize-only
.\.venv\Scripts\python.exe verify_release.py
```

这一步不重建 exe，只为现有已验收目录重新生成每文件 SHA-256、ZIP CRC 校验和 ZIP SHA-256。请勿在生成最终校验后继续修改包中文件。

包内的 `verify_checksum.ps1` 用于检查解压后的交付完整性。运行测试会改变 outputs/logs，因此先校验、再运行。

如果系统禁止执行 PowerShell 脚本，不必改变系统策略；直接用 `Get-FileHash` 校验 ZIP 与其 `.sha256` 即可。开发验收也会在 Python 中逐文件核对 ZIP 内的全部清单项。
