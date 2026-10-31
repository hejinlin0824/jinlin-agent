"""Minimal JSON-Schema validator for tool arguments.

Only the subset used by our tool contracts (object/string/integer/number/
boolean/array, enum, required, additionalProperties, items, min/max, anyOf).
This keeps the package dependency-free while still rejecting malformed model
output before it can reach a tool.
"""

from __future__ import annotations

from typing import Any

_TYPES = {
    "object": dict,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "null": type(None),
}


def validate(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    _validate(schema, value, path, errors)
    return errors


def _validate(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    if "anyOf" in schema:
        branch_errors = [validate(s, value, path) for s in schema["anyOf"]]
        if any(not e for e in branch_errors):
            return
        errors.append(f"{path}: no anyOf branch matched")
        return

    expected = schema.get("type")
    if expected is not None:
        if isinstance(expected, list):
            if not any(_is_type(t, value) for t in expected):
                errors.append(f"{path}: expected one of {expected}, got {type(value).__name__}")
                return
        elif not _is_type(expected, value):
            errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
            return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema:
            import re

            if not re.search(schema["pattern"], value):
                errors.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        props = schema.get("properties") or {}
        for key, item in value.items():
            if key in props:
                _validate(props[key], item, f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{key}'")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate(schema["items"], item, f"{path}[{i}]", errors)


def _is_type(name: str, value: Any) -> bool:
    py = _TYPES.get(name)
    if py is None:
        return True
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, py)
