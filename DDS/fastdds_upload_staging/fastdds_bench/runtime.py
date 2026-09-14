from __future__ import annotations

import os
import sys
from pathlib import Path

from .util import PROJECT_ROOT

_DLL_DIRECTORY_HANDLES: list[object] = []


def configure_runtime(project_root: Path = PROJECT_ROOT) -> dict[str, list[str]]:
    """Make the locally built bindings and DLLs visible to this process."""
    project_root = Path(project_root).resolve()
    runtime = project_root / "runtime"

    site_candidates = [
        runtime / "Lib" / "site-packages",
        runtime / "lib" / "site-packages",
    ]
    site_candidates.extend(sorted((runtime / "lib").glob("python*/site-packages")))
    site_dirs = [p for p in site_candidates if p.is_dir()]
    for path in reversed(site_dirs):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)

    dll_candidates = [runtime / "bin", runtime / "lib"]
    fastdds_home = os.environ.get("FASTDDSHOME")
    if fastdds_home:
        home = Path(fastdds_home)
        dll_candidates.extend([home / "bin", home / "lib"])
    dll_dirs = [p.resolve() for p in dll_candidates if p.is_dir()]

    if dll_dirs:
        current_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join(
            [*(str(p) for p in dll_dirs), current_path]
        )

    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        known = {str(getattr(h, "path", "")) for h in _DLL_DIRECTORY_HANDLES}
        for path in dll_dirs:
            if str(path) not in known:
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(path)))

    return {
        "site_packages": [str(p) for p in site_dirs],
        "dll_directories": [str(p) for p in dll_dirs],
    }

