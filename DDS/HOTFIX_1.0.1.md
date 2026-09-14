# Fast DDS Python Benchmark 1.0.1 热修复

此版本修复 `validate_one_example.bat` 报错：

```text
EXAMPLE VERIFICATION FAILED: Run failed: ['Data-integrity validation reported errors']
```

## 原因

`BenchmarkMessage.idl` 中的 payload 是有界字符串。Fast DDS-Gen 将其生成
为 `fastcdr::fixed_string`，Python 端应通过生成的 `payload_str()` 读取文本。
1.0.0 错误地使用了 `payload()`，导致订阅回调对 SWIG 代理对象调用
`encode()`，有效样本被误判成完整性错误。

## 在现有安装上应用

1. 关闭正在运行的示例进程。
2. 将本热修复包解压到原项目根目录：
   `C:\Users\28794\Downloads\fastdds_python_benchmark_v1.0`
3. 出现同名文件提示时选择“全部替换”。不要删除 `.venv`、`runtime` 或
   `.build`，也不需要重新运行 `setup_windows.bat`。
4. 在原项目根目录执行：

```bat
set FASTDDSHOME=D:\fastdds
run_one_example.bat
validate_one_example.bat
```

成功时最后会显示：

```text
EXAMPLE VERIFICATION PASS
```

