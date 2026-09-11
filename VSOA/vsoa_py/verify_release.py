"""Verify archive CRC, exact inventory hashes and extracted Python-independent startup."""

import hashlib
import json
import os
import subprocess
import uuid
import zipfile
from pathlib import Path

from standalone.io_utils import write_json
from standalone.version import PACKAGE_NAME


def verify():
    archive = Path("release") / f"{PACKAGE_NAME}.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert digest == archive.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    target = (Path("results") / f"release-verification-{uuid.uuid4().hex[:8]}").resolve()
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        assert len(bundle.namelist()) == len(set(bundle.namelist()))
        inventory = bundle.read(f"{PACKAGE_NAME}/checksum.txt").decode("utf-8")
        expected_files = set()
        for line in inventory.splitlines():
            checksum, name = line.split("  ", 1)
            member = f"{PACKAGE_NAME}/{name}"
            expected_files.add(member)
            assert hashlib.sha256(bundle.read(member)).hexdigest() == checksum, member
        actual_files = {item.filename for item in bundle.infolist() if not item.is_dir()}
        assert actual_files == expected_files | {f"{PACKAGE_NAME}/checksum.txt"}
        for name in bundle.namelist():
            resolved = (target / name).resolve()
            assert resolved.is_relative_to(target), name
        bundle.extractall(target)
    package = target / PACKAGE_NAME
    manifest = json.loads((package / "build_manifest.json").read_text(encoding="utf-8"))
    for name, checksum in manifest["source_sha256"].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == checksum, f"Source differs from tested EXE: {name}"
    environment = dict(os.environ)
    environment["PATH"] = os.environ["SystemRoot"] + ";" + str(Path(os.environ["SystemRoot"]) / "System32")
    environment["PYTHONHOME"] = str(target / "no-python")
    environment["PYTHONPATH"] = str(target / "no-packages")
    environment.pop("VIRTUAL_ENV", None)
    for args in (["--version"], ["--validate-config", "--suite", "qualification"]):
        completed = subprocess.run([str(package / "vsoa.exe"), *args], env=environment, cwd=target,
                                   capture_output=True, timeout=30, check=True)
        (target / ("version.txt" if args[0] == "--version" else "validated-config.json")).write_bytes(completed.stdout)
    report = {"passed": True, "archive": str(archive.resolve()), "sha256": digest,
              "bytes": archive.stat().st_size, "inventory_files": len(expected_files),
              "crc_verified": True, "all_hashes_verified": True, "source_matches_executable_manifest": True,
              "extracted_python_independent_startup": True, "extraction_directory": str(target)}
    write_json(Path("results/release-verification.json"), report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    verify()
