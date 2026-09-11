"""Build one Windows x64 delivery and finalize its checksum inventory and ZIP."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from standalone.version import VERSION, PACKAGE_NAME
NAME = PACKAGE_NAME


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finalize(destination):
    inventory = [path for path in sorted(destination.rglob("*")) if path.is_file() and path.name != "checksum.txt"]
    checksum = "\n".join(f"{sha256(path)}  {path.relative_to(destination).as_posix()}" for path in inventory) + "\n"
    (destination / "checksum.txt").write_text(checksum, encoding="utf-8")
    archive = destination.parent / f"{NAME}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                bundle.write(path, f"{NAME}/{path.relative_to(destination).as_posix()}")
            elif not any(path.iterdir()):
                bundle.writestr(f"{NAME}/{path.relative_to(destination).as_posix()}/", b"")
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError("ZIP CRC validation failed")
    (archive.parent / f"{archive.name}.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size, "sha256": sha256(archive),
                      "inventoried_files": len(inventory)}, indent=2))


def build(destination):
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise RuntimeError("Build requires a Windows 64-bit Python environment")
    destination.mkdir(parents=True, exist_ok=True)
    binary_dir = ROOT / "build" / "standalone-bin"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
               "--name", "vsoa", "--distpath", str(binary_dir), "--workpath", str(ROOT / "build" / "pyinstaller"),
               "--specpath", str(ROOT / "build"), "--copy-metadata", "vsoa", "--copy-metadata", "psutil",
               "--copy-metadata", "PyYAML", "--collect-submodules", "vsoa", "--exclude-module", "tkinter",
               "--exclude-module", "pytest", str(ROOT / "vsoa_module.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    shutil.copy2(binary_dir / "vsoa.exe", destination / "vsoa.exe")
    for path in (ROOT / "delivery_template").iterdir():
        if path.is_file():
            shutil.copy2(path, destination / path.name)
    for directory in ("outputs", "logs", "licenses"):
        (destination / directory).mkdir(exist_ok=True)
    for package_name in ("vsoa", "psutil", "PyYAML", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging", "setuptools"):
        package = importlib.metadata.distribution(package_name)
        for source in package.files or []:
            if "license" in str(source).lower() or Path(str(source)).name.upper().startswith("COPYING"):
                actual = package.locate_file(source)
                if actual.is_file() and actual.suffix.lower() not in {".py", ".pyc"}:
                    target = destination / "licenses" / package_name / str(source).replace("/", "_").replace("\\", "_")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(actual, target)
    for filename in ("LICENSE.txt", "LICENSE"):
        source = Path(sys.base_prefix) / filename
        if source.exists():
            shutil.copy2(source, destination / "licenses" / "Python-LICENSE.txt")
            break
    library_notices = Path(sys.base_prefix) / "Doc" / "html" / "license.html"
    if library_notices.exists():
        shutil.copy2(library_notices, destination / "licenses" / "CPython-standard-library-license.html")
    metadata = {"module_name": "vsoa", "module_version": VERSION, "build_time_utc": datetime.now(timezone.utc).isoformat(),
                "platform": platform.platform(), "python": platform.python_version(),
                "packages": {name: importlib.metadata.version(name) for name in ("vsoa", "psutil", "PyYAML", "pyinstaller")},
                "source_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in
                                  [ROOT / "vsoa_module.py", *sorted((ROOT / "standalone").glob("*.py"))]},
                "self_contained": True, "authenticode_signed": False}
    (destination / "build_manifest.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finalize-only", action="store_true")
    args = parser.parse_args()
    destination = ROOT / "release" / NAME
    if not args.finalize_only:
        build(destination)
    if not (destination / "vsoa.exe").is_file():
        raise FileNotFoundError("Build executable before finalizing")
    finalize(destination)
