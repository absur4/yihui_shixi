"""Ecosystem: reproducible installed-distribution evidence, no invented maturity score."""

import importlib.metadata
from pathlib import Path

import vsoa

from bench.common import entry


def run(options):
    package = importlib.metadata.distribution("vsoa")
    metadata = package.metadata
    required = ["Client", "Server", "Position", "Payload", "Timer", "WorkQueue"]
    checks = {name: hasattr(vsoa, name) for name in required}
    contents = metadata.get_payload()
    return "partial" if all(checks.values()) else "fail", {
        "evidence_kind": "installed_distribution_snapshot",
        "version": package.version, "python_requirement": metadata.get("Requires-Python"),
        "license": metadata.get("License-Expression"),
        "project_urls": metadata.get_all("Project-URL", []),
        "classifiers": metadata.get_all("Classifier", []),
        "runtime_dependencies": metadata.get_all("Requires-Dist", []),
        "importable_api": checks, "bundled_readme_characters": len(contents),
        "source_file_count": len(list(Path(vsoa.__file__).parent.glob("*.py"))),
        "github_activity": "not_measured", "issue_response_time": "not_measured",
        "downloads": "not_measured", "production_adoption": "not_verified",
        "other_language_bindings": "documented_not_tested", "maturity_score": None}, [
        "Package metadata is evidence of packaging/documentation, not ecosystem popularity",
        "No online release/issue/download snapshot obtained; installed version is not claimed to be latest",
        "No subjective maturity score is synthesized from unavailable data"]


if __name__ == "__main__":
    raise SystemExit(entry("ecosystem", "feature", __doc__, run))
