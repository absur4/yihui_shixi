"""Unified Zenoh adapter package.

The package deliberately keeps the console-facing contract independent from
the internal ``zenoh_bench`` engine.
"""

from .adapter import Adapter, create_adapter

__all__ = ["Adapter", "create_adapter"]
__version__ = "1.1.0"
