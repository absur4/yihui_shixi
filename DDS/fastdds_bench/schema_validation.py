from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .util import PROJECT_ROOT

SCHEMA_PATH = PROJECT_ROOT / "schemas" / "unified_result.schema.json"


def _reject_nonfinite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"JSON result contains a non-finite number at {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{path}[{index}]")


def validate_result(document: dict[str, Any], schema_path: Path = SCHEMA_PATH) -> None:
    _reject_nonfinite(document)
    with Path(schema_path).open("r", encoding="utf-8") as stream:
        schema = json.load(stream)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        messages = []
        for error in errors[:20]:
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            messages.append(f"{location}: {error.message}")
        if len(errors) > 20:
            messages.append(f"... and {len(errors) - 20} more error(s)")
        raise ValueError("JSON Schema validation failed:\n" + "\n".join(messages))
