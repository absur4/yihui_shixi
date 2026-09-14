from __future__ import annotations

from fastdds_bench.runtime import configure_runtime

configure_runtime()

from fastdds_bench.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

