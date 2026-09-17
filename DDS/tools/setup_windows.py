from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = PROJECT_ROOT / ".build"
SOURCE_ROOT = BUILD_ROOT / "src"
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
BINDING_SOURCE = SOURCE_ROOT / "Fast-DDS-python"
BINDING_TAG = "v2.6.1"
BINDING_REPOSITORY = "https://github.com/eProsima/Fast-DDS-python.git"


class SetupError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    completed = subprocess.run(command, cwd=cwd, env=env, check=False)
    if completed.returncode != 0:
        raise SetupError(
            f"Command failed with exit code {completed.returncode}: "
            f"{subprocess.list2cmdline(command)}"
        )


def capture(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise SetupError(
            f"Command failed: {subprocess.list2cmdline(command)}\n{completed.stdout}"
        )
    return completed.stdout.strip()


def find_executable(name: str, env_name: str | None = None) -> Path:
    if env_name and os.environ.get(env_name):
        path = Path(os.environ[env_name]).expanduser().resolve()
        if path.exists():
            return path
        raise SetupError(f"{env_name} points to a missing file: {path}")
    # 先查项目私有 venv：requirements-dev.txt 里的 swig 包会把 swig.exe 放在 Scripts 下，
    # wheel 里还带一份 data/bin/swig.exe，这两处都不在 PATH 上。
    # 早先只查 PATH，导致"已装好 swig 却报 not on PATH"。
    for candidate in (
        PROJECT_ROOT / ".venv" / "Scripts" / name,
        PROJECT_ROOT / ".venv" / "bin" / name,
        PROJECT_ROOT / ".venv" / "Lib" / "site-packages" / "swig" / "data" / "bin" / name,
        Path(sys.executable).parent / name,
    ):
        if candidate.exists():
            return candidate.resolve()
    found = shutil.which(name)
    if not found:
        raise SetupError(
            f"Required tool is not on PATH: {name}"
            f"（已检查项目 .venv；也可用 {env_name or 'PATH'} 指定绝对路径）"
        )
    return Path(found).resolve()


def find_fastddsgen(fastdds_home: Path) -> Path:
    candidates: list[Path] = []
    if os.environ.get("FASTDDSGEN"):
        candidates.append(Path(os.environ["FASTDDSGEN"]))
    if os.environ.get("FASTDDSGENHOME"):
        home = Path(os.environ["FASTDDSGENHOME"])
        candidates.extend(
            [home / "scripts" / "fastddsgen.bat", home / "scripts" / "fastddsgen"]
        )
    candidates.extend(
        [
            fastdds_home / "bin" / "fastddsgen.bat",
            fastdds_home / "bin" / "fastddsgen.exe",
            fastdds_home / "bin" / "fastddsgen",
        ]
    )
    for name in ("fastddsgen.bat", "fastddsgen.exe", "fastddsgen"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path.exists():
            return path
    raise SetupError(
        "fastddsgen was not found. Put fastddsgen.bat on PATH or set FASTDDSGEN."
    )


def command_for_script(path: Path, args: list[str]) -> list[str]:
    if os.name == "nt" and path.suffix.lower() in {".bat", ".cmd"}:
        command_processor = os.environ.get("COMSPEC", "cmd.exe")
        return [command_processor, "/d", "/c", str(path), *args]
    return [str(path), *args]


def ensure_windows_python() -> None:
    if os.name != "nt":
        raise SetupError("This setup helper is for Windows. Run it on the target Windows PC.")
    if platform.architecture()[0] != "64bit":
        raise SetupError("A 64-bit Python interpreter is required")
    if sys.version_info[:2] not in {(3, 10), (3, 11), (3, 12)}:
        raise SetupError(
            "Use 64-bit Python 3.10, 3.11, or 3.12. Python 3.11 is recommended."
        )


def check_swig(swig: Path) -> str:
    output = capture([str(swig), "-version"])
    match = re.search(r"SWIG Version\s+(\d+)\.(\d+)(?:\.(\d+))?", output)
    if not match:
        raise SetupError(f"Could not parse SWIG version:\n{output}")
    version = tuple(int(value or 0) for value in match.groups())
    if version >= (4, 2, 0):
        raise SetupError(
            f"{BINDING_TAG} requires SWIG < 4.2; found {version}. "
            "Install SWIG 4.1.x and put it first on PATH."
        )
    return ".".join(str(value) for value in version)


def ensure_java_environment(environment: dict[str, str]) -> str | None:
    """fastddsgen 需要 java 在 PATH 或 JAVA_HOME 上。

    JAVA_HOME 未设置且 PATH 上没有 java 时，在常见 JDK 安装位置自动定位，
    并把 JAVA_HOME 与该 JDK 的 bin 写入给定环境（后续子进程即可直接使用）。
    返回最终使用的 JDK 路径；无法定位时返回 None。
    """
    current = environment.get("JAVA_HOME")
    if current and (Path(current) / "bin" / "java.exe").exists():
        return current
    if shutil.which("java") or shutil.which("java.exe"):
        return None
    for base, pattern in (
        (r"C:\Program Files\Eclipse Adoptium", "jdk*"),
        (r"C:\Program Files\Java", "jdk*"),
        (r"C:\Program Files\Microsoft", "jdk*"),
        (r"C:\Program Files\Zulu", "zulu*"),
        (r"C:\Program Files (x86)\Eclipse Adoptium", "jdk*"),
    ):
        root = Path(base)
        if not root.is_dir():
            continue
        for directory in sorted(root.glob(pattern)):
            if (directory / "bin" / "java.exe").exists():
                environment["JAVA_HOME"] = str(directory)
                environment["PATH"] = (
                    str(directory / "bin") + os.pathsep + environment.get("PATH", "")
                )
                return str(directory)
    return None


def ensure_openssl_environment(environment: dict[str, str]) -> str | None:
    """fastdds-config.cmake 里 find_dependency(OpenSSL) 需要头文件与导入库。

    Fast DDS 安装包只带 libcrypto/libssl 的 .lib（没有 openssl 头文件），
    所以 OPENSSL_ROOT_DIR 未设置时要自动定位一个"头文件+库"齐全的根目录
    （conda 环境下通常是 <prefix>\\Library）。返回使用的根目录；找不到返回 None。
    """
    current = environment.get("OPENSSL_ROOT_DIR")
    if current and (Path(current) / "include" / "openssl" / "ssl.h").exists():
        return current
    candidates: list[Path] = []
    for prefix in (
        environment.get("CONDA_PREFIX"),
        getattr(sys, "base_prefix", None),
        getattr(sys, "prefix", None),
        str(Path(sys.executable).parent),
    ):
        if prefix:
            candidates.extend([Path(prefix) / "Library", Path(prefix)])
    candidates.extend(
        [
            Path(r"C:\Program Files\OpenSSL-Win64"),
            Path(r"C:\Program Files\OpenSSL"),
            Path(r"C:\OpenSSL-Win64"),
        ]
    )
    for candidate in candidates:
        if not (candidate / "include" / "openssl" / "ssl.h").exists():
            continue
        has_lib = any(
            (candidate / rel).exists()
            for rel in ("lib/libcrypto.lib", "lib/VC/x64/MD/libcrypto.lib", "lib/VC/x64/MDd/libcrypto.lib")
        )
        if has_lib:
            environment["OPENSSL_ROOT_DIR"] = str(candidate)
            return str(candidate)
    return None


def clone_binding_source(git: Path) -> None:
    """
    Prepare Fast-DDS-python source.

    Supports:
    1. Git checkout from GitHub
    2. Manually downloaded ZIP source without .git metadata
    """

    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)

    # No local source: try Git clone
    if not BINDING_SOURCE.exists():
        run(
            [
                str(git),
                "clone",
                "--depth",
                "1",
                "--branch",
                BINDING_TAG,
                BINDING_REPOSITORY,
                str(BINDING_SOURCE),
            ]
        )
        return

    # ZIP source: no git metadata, skip verification
    if not (BINDING_SOURCE / ".git").is_dir():
        print(
            f"WARNING: ZIP source detected, skipping git verification: {BINDING_SOURCE}"
        )
        return

    # Normal git checkout: verify tag
    actual_tag = capture(
        [
            str(git),
            "-C",
            str(BINDING_SOURCE),
            "describe",
            "--tags",
            "--exact-match",
        ]
    )

    if actual_tag != BINDING_TAG:
        raise SetupError(
            f"Fast-DDS-python checkout is {actual_tag!r}, expected {BINDING_TAG!r}."
        )


def find_cmake_package(prefix: Path, package: str) -> Path:
    names = {
        f"{package.lower()}-config.cmake",
        f"{package.lower()}Config.cmake".lower(),
    }
    candidates = [
        path
        for path in prefix.rglob("*.cmake")
        if path.name.lower() in names
    ]
    if not candidates:
        raise SetupError(
            f"Could not find the {package} CMake package under FASTDDSHOME={prefix}. "
            "FASTDDSHOME must be the install prefix, not the source directory."
        )
    return sorted(candidates)[0]


def detect_fastdds_version(prefix: Path) -> str | None:
    """尽力从安装前缀里探测 Fast DDS 版本；探测不到返回 None。

    结果只作为 runtime/build_info.json 的提示信息，adapter 会优先使用它，
    探测不到时回落到 config.yaml 里声明的目标版本。
    """
    pattern = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
    for config in sorted(prefix.rglob("fastdds-config.cmake")):
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # eProsima 的 Windows 安装包写的是 set(fastdds_VERSION 3.6.1.0)，
        # 而不是 PACKAGE_VERSION，所以两个都要认。
        match = re.search(
            r"(?:PACKAGE_VERSION|fastdds_VERSION)[\s\"']+(\d+\.\d+(?:\.\d+)?)", text
        )
        if match:
            return match.group(1)
    directories = sorted((prefix / "lib" / "cmake").glob("fastdds*")) + sorted(
        (prefix / "share").glob("fastdds*/cmake/fastdds*")
    )
    for directory in directories:
        match = pattern.search(directory.name)
        if match:
            return match.group(1)
    match = pattern.search(prefix.name)
    return match.group(1) if match else None


def cmake_configure_and_install(
    cmake: Path,
    source: Path,
    build: Path,
    prefix_path: str,
    swig: Path,
    environment: dict[str, str],
) -> None:
    build.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(cmake),
            "-S",
            str(source),
            "-B",
            str(build),
            "-A",
            "x64",
            "-DCMAKE_CONFIGURATION_TYPES=Release",
            f"-DCMAKE_INSTALL_PREFIX={RUNTIME_ROOT}",
            f"-DCMAKE_PREFIX_PATH={prefix_path}",
            f"-DPython3_EXECUTABLE={sys.executable}",
            f"-DPython3_ROOT_DIR={Path(sys.executable).parent}",
            f"-DSWIG_EXECUTABLE={swig}",
            # 生成调试符号（PDB）：崩溃转储才能用 cdb 解析出 _wrap_XXX 这类
            # SWIG 生成的函数名；否则栈里只有 _fastdds_python+<偏移>，
            # 定位不到究竟是哪个 API 的字符串转换把 NULL 交给了 std::string。
            "-DCMAKE_MSVC_DEBUG_INFORMATION_FORMAT=ProgramDatabase",
            "-DCMAKE_CXX_FLAGS_RELEASE=/O2 /Ob2 /DNDEBUG /Zi",
            "-DCMAKE_SHARED_LINKER_FLAGS_RELEASE=/DEBUG",
            "-DCMAKE_EXE_LINKER_FLAGS_RELEASE=/DEBUG",
            # Fast DDS 的 Windows 安装包同时提供静态库与导入库，find_package 默认会选静态。
            # 静态链接会让 fastdds_python 与 BenchmarkMessage 各持一份 Fast DDS 全局单例
            # （DomainParticipantFactory、TypeObject 注册表、EDP/SEDP 状态等）；运行期在
            # 发现阶段跨实例传递对象就会 0xC0000005。已由 cdb 符号栈证实：
            #   _fastdds_python!...EDP::pairingReader -> ... -> ucrtbase!strlen(NULL)
            # 因此必须统一走共享库，全进程只加载一份 fastdds-3.6.dll / fastcdr-2.3.dll。
            "-DBUILD_SHARED_LIBS=ON",
            "-DBUILD_TESTING=OFF",
        ],
        env=environment,
    )
    run(
        [
            str(cmake),
            "--build",
            str(build),
            "--config",
            "Release",
            "--target",
            "install",
        ],
        env=environment,
    )


def patch_fastdds_windows_loader(fastdds_home: Path) -> None:
    """
    Fix the Windows loader emitted by Fast-DDS-python 2.6.1.

    With some prebuilt Fast DDS Windows installations, the generated Python
    wrapper contains:

        win32api.LoadLibrary('libfastdds-3.6.lib')

    A .lib file is a link-time library and cannot be loaded with LoadLibrary.
    Replace it with the real release DLL found under FASTDDSHOME/bin.
    """
    if os.name != "nt":
        return

    bin_dir = fastdds_home / "bin"
    dll_candidates = sorted(
        path
        for path in bin_dir.glob("fastdds-*.dll")
        if not path.name.lower().startswith("fastddsd-")
    )
    if not dll_candidates:
        raise SetupError(
            f"Fast DDS release DLL was not found under {bin_dir}. "
            "Expected a file such as fastdds-3.6.dll."
        )

    fastdds_dll = dll_candidates[0].name
    loader_pattern = re.compile(
        r"win32api\.LoadLibrary\(\s*['\"]"
        r"(?:lib)?fastdds-[^'\"]+\.lib"
        r"['\"]\s*\)"
    )
    replacement = f"win32api.LoadLibrary('{fastdds_dll}')"

    candidates = [
        BUILD_ROOT / "fastdds_python" / "src" / "swig" / "fastdds.py",
        RUNTIME_ROOT / "Lib" / "site-packages" / "fastdds" / "__init__.py",
    ]

    patched_any = False
    for python_file in candidates:
        if not python_file.is_file():
            continue

        original = python_file.read_text(encoding="utf-8")
        updated, count = loader_pattern.subn(replacement, original)

        if count:
            python_file.write_text(updated, encoding="utf-8", newline="\n")
            print(
                f"Patched Fast DDS Windows loader: {python_file} "
                f"-> {fastdds_dll}"
            )
            patched_any = True
        elif fastdds_dll in original:
            print(f"Fast DDS Windows loader already correct: {python_file}")

    runtime_loader = (
        RUNTIME_ROOT / "Lib" / "site-packages" / "fastdds" / "__init__.py"
    )
    if not runtime_loader.is_file():
        raise SetupError(
            f"Installed Fast DDS Python loader was not found: {runtime_loader}"
        )

    runtime_text = runtime_loader.read_text(encoding="utf-8")
    if fastdds_dll not in runtime_text:
        raise SetupError(
            "Could not repair the installed Fast DDS Python Windows loader. "
            f"Expected it to load {fastdds_dll}."
        )

    # Make the native DLL directories explicit for subprocesses used by setup.
    dll_dirs = [
        fastdds_home / "bin",
        RUNTIME_ROOT / "bin",
    ]
    for dll_dir in dll_dirs:
        if dll_dir.is_dir():
            current_path = os.environ.get("PATH", "")
            if str(dll_dir).lower() not in current_path.lower():
                os.environ["PATH"] = str(dll_dir) + os.pathsep + current_path

    if patched_any:
        print(f"Fast DDS runtime loader fix completed using {fastdds_dll}.")


def verify_generated_type_files(generated: Path) -> None:
    """Verify that fastddsgen produced the expected nested idl/ outputs."""
    idl_dir = generated / "idl"
    required_files = [
        idl_dir / "BenchmarkMessage.hpp",
        idl_dir / "BenchmarkMessage.i",
        idl_dir / "BenchmarkMessageTypeObjectSupport.cxx",
    ]
    missing = [path for path in required_files if not path.is_file()]
    if missing:
        details = "\n".join(f"  - {path}" for path in missing)
        raise SetupError(
            "fastddsgen did not generate the expected BenchmarkMessage files:\n"
            f"{details}\n"
            "Do not use -flat-output-dir for this project."
        )


def patch_generated_type_cmake(generated: Path) -> None:
    """
    Patch the CMake project emitted by Fast DDS-Gen.

    Fast DDS-Gen places BenchmarkMessage.hpp under generated_type/idl/, while
    the generated SWIG C++ wrapper includes it as "BenchmarkMessage.hpp".
    Add the idl directory explicitly to the generated targets.
    """
    cmake_file = generated / "CMakeLists.txt"
    if not cmake_file.is_file():
        raise SetupError(f"Generated CMakeLists.txt was not found: {cmake_file}")

    marker = "# BEGIN local BenchmarkMessage Python wrapper fix"
    current = cmake_file.read_text(encoding="utf-8")
    if marker in current:
        print(f"CMake include-path patch already present: {cmake_file}")
        return

    patch = """

# BEGIN local BenchmarkMessage Python wrapper fix
# Fast DDS-Gen places BenchmarkMessage.hpp under ./idl, while the generated
# SWIG C++ wrapper includes it as "BenchmarkMessage.hpp".
if(TARGET BenchmarkMessage)
    target_include_directories(BenchmarkMessage PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/idl"
    )
endif()

if(TARGET BenchmarkMessageWrapper)
    target_include_directories(BenchmarkMessageWrapper PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/idl"
    )
endif()
# END local BenchmarkMessage Python wrapper fix
"""

    with cmake_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(patch)

    print(f"Patched generated CMake include paths: {cmake_file}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build Fast DDS Python 2.6.1 and the benchmark IDL wrapper"
    )
    parser.add_argument("--force", action="store_true", help="rerun all configure/build steps")
    args = parser.parse_args(argv)

    ensure_windows_python()
    fastdds_home_value = os.environ.get("FASTDDSHOME")
    if not fastdds_home_value:
        raise SetupError("FASTDDSHOME is not set (expected example: D:\\fastdds)")
    fastdds_home = Path(fastdds_home_value).expanduser().resolve()
    if not fastdds_home.is_dir():
        raise SetupError(f"FASTDDSHOME does not exist: {fastdds_home}")
    fastdds_cmake = find_cmake_package(fastdds_home, "fastdds")
    fastcdr_cmake = find_cmake_package(fastdds_home, "fastcdr")

    cmake = find_executable("cmake.exe" if os.name == "nt" else "cmake")
    git = find_executable("git.exe" if os.name == "nt" else "git")
    swig = find_executable("swig.exe" if os.name == "nt" else "swig", "SWIG_EXECUTABLE")
    swig_version = check_swig(swig)
    java_home = ensure_java_environment(os.environ)
    if java_home:
        print(f"Java: {java_home}（自动定位，已写入 JAVA_HOME 供 fastddsgen 使用）")
    openssl_root = ensure_openssl_environment(os.environ)
    if openssl_root:
        print(f"OpenSSL: {openssl_root}（自动定位，已写入 OPENSSL_ROOT_DIR 供 fastdds CMake 依赖使用）")
    fastddsgen = find_fastddsgen(fastdds_home)
    fastddsgen_version = capture(command_for_script(fastddsgen, ["-version"]))

    build_environment = os.environ.copy()

    # MSVC automatically consumes CL and _CL_ as extra compiler arguments.
    # Remove them so a stale value such as literal "%CL%" cannot break the
    # Fast DDS-Gen preprocessing step.
    build_environment.pop("CL", None)
    build_environment.pop("_CL_", None)

    path_parts = [fastdds_home / "bin", RUNTIME_ROOT / "bin"]
    build_environment["PATH"] = os.pathsep.join(
        [*(str(path) for path in path_parts if path.is_dir()), build_environment.get("PATH", "")]
    )

    clone_binding_source(git)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)

    fastdds_package = RUNTIME_ROOT / "Lib" / "site-packages" / "fastdds"
    if args.force or not fastdds_package.exists():
        cmake_configure_and_install(
            cmake,
            BINDING_SOURCE / "fastdds_python",
            BUILD_ROOT / "fastdds_python",
            str(fastdds_home),
            swig,
            build_environment,
        )
    else:
        print(f"Fast DDS Python binding already present: {fastdds_package}")

    # Fast-DDS-python 2.6.1 may generate a Windows loader that incorrectly
    # tries to LoadLibrary() a .lib file. Repair it before any import check.
    patch_fastdds_windows_loader(fastdds_home)

    # Refresh PATH for child processes after the loader fix.
    build_environment["PATH"] = os.pathsep.join(
        [
            str(fastdds_home / "bin"),
            str(RUNTIME_ROOT / "bin"),
            build_environment.get("PATH", ""),
        ]
    )

    generated = BUILD_ROOT / "generated_type"
    benchmark_build = BUILD_ROOT / "benchmark_message"

    # Always regenerate the benchmark IDL wrapper from a clean tree.
    # This removes stale files left by previous experiments such as
    # -flat-output-dir and stale CMake caches.
    if generated.exists():
        shutil.rmtree(generated)
    if benchmark_build.exists():
        shutil.rmtree(benchmark_build)
    generated.mkdir(parents=True, exist_ok=True)

    run(
        command_for_script(
            fastddsgen,
            [
                "-python",
                "-replace",
                "-d",
                str(generated),
                str(PROJECT_ROOT / "idl" / "BenchmarkMessage.idl"),
            ],
        ),
        env=build_environment,
    )

    verify_generated_type_files(generated)
    patch_generated_type_cmake(generated)

    cmake_configure_and_install(
        cmake,
        generated,
        benchmark_build,
        f"{fastdds_home};{RUNTIME_ROOT}",
        swig,
        build_environment,
    )

    build_info = {
        "fastdds_home": str(fastdds_home),
        "fastdds_version": detect_fastdds_version(fastdds_home),
        "fastdds_cmake": str(fastdds_cmake),
        "fastcdr_cmake": str(fastcdr_cmake),
        "fastdds_python_tag": BINDING_TAG,
        "python": sys.version,
        "python_executable": sys.executable,
        "cmake": capture([str(cmake), "--version"]).splitlines()[0],
        "swig_version": swig_version,
        "fastddsgen": str(fastddsgen),
        "fastddsgen_version": fastddsgen_version,
    }
    (RUNTIME_ROOT / "build_info.json").write_text(
        json.dumps(build_info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    run(
        [
            sys.executable,
            str(PROJECT_ROOT / "launch.py"),
            "check",
            "--config",
            str(PROJECT_ROOT / "config.example.yaml"),
        ],
        cwd=PROJECT_ROOT,
        env=build_environment,
    )
    print("Setup completed successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SetupError as exc:
        print(f"SETUP ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
