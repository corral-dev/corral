"""Zero-dependency JSON Schema validator with exact JSONPath error reporting.

Supports the Draft-7 / 2020-12 structural subset used by workflow steps and
CLI argument validation, returning deterministic ``{path, message}`` entries
so worker agents can self-correct in the same turn.
"""

from __future__ import annotations

import re
from typing import Any

_VALID_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


def _python_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def validate_schema_definition(schema: Any) -> list[str]:
    """Validate that a supplied schema object is well-formed before registering a step."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return ["schema must be a JSON object"]
    schema_type = schema.get("type")
    if schema_type is not None:
        types = [schema_type] if isinstance(schema_type, str) else schema_type
        if not isinstance(types, list) or not types or not all(isinstance(t, str) and t in _VALID_TYPES for t in types):
            errors.append(f"invalid schema 'type': {schema_type!r}")
    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or not all(isinstance(item, str) for item in req):
            errors.append("schema 'required' must be a list of strings")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            errors.append("schema 'properties' must be an object")
        else:
            for key, sub in props.items():
                for err in validate_schema_definition(sub):
                    errors.append(f"properties.{key}: {err}")
    if "items" in schema and isinstance(schema["items"], dict):
        for err in validate_schema_definition(schema["items"]):
            errors.append(f"items: {err}")
    for comb in ("anyOf", "oneOf", "allOf"):
        if comb in schema:
            branch_list = schema[comb]
            if not isinstance(branch_list, list) or not branch_list:
                errors.append(f"schema '{comb}' must be a non-empty list")
            else:
                for idx, sub in enumerate(branch_list):
                    for err in validate_schema_definition(sub):
                        errors.append(f"{comb}[{idx}]: {err}")
    return errors


def validate_json(value: Any, schema: dict[str, Any], path: str = "$") -> list[dict[str, str]]:
    """Return a list of ``{"path": ..., "message": ...}`` schema violations (empty = valid)."""
    if not isinstance(schema, dict):
        return [{"path": path, "message": "schema must be an object"}]

    errors: list[dict[str, str]] = []

    if "const" in schema and value != schema["const"]:
        errors.append({"path": path, "message": f"expected constant {schema['const']!r}, got {value!r}"})
        return errors

    if "enum" in schema:
        allowed = schema["enum"]
        if isinstance(allowed, list) and not any(value == opt and type(value) is type(opt) for opt in allowed):
            errors.append({"path": path, "message": f"value {value!r} not in enum {allowed!r}"})
            return errors

    if "type" in schema:
        expected = schema["type"]
        allowed_types = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_type(value, t) for t in allowed_types):
            expected_label = " | ".join(allowed_types)
            errors.append({"path": path, "message": f"expected {expected_label}, got {_python_type_name(value)}"})
            return errors

    if "allOf" in schema and isinstance(schema["allOf"], list):
        for sub in schema["allOf"]:
            errors.extend(validate_json(value, sub, path))

    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        if not any(len(validate_json(value, sub, path)) == 0 for sub in schema["anyOf"]):
            errors.append({"path": path, "message": "value did not satisfy anyOf schema branches"})

    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        matches = sum(1 for sub in schema["oneOf"] if len(validate_json(value, sub, path)) == 0)
        if matches != 1:
            errors.append({"path": path, "message": f"value must satisfy exactly 1 oneOf branch, matched {matches}"})

    if isinstance(value, str):
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(value) < min_len:
            errors.append({"path": path, "message": f"string length {len(value)} is shorter than minLength {min_len}"})
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and len(value) > max_len:
            errors.append({"path": path, "message": f"string length {len(value)} exceeds maxLength {max_len}"})
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    errors.append({"path": path, "message": f"string does not match pattern {pattern!r}"})
            except re.error:
                errors.append({"path": path, "message": f"invalid regex pattern in schema: {pattern!r}"})

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append({"path": path, "message": f"value {value} is less than minimum {minimum}"})
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append({"path": path, "message": f"value {value} exceeds maximum {maximum}"})
        ex_min = schema.get("exclusiveMinimum")
        if isinstance(ex_min, (int, float)) and value <= ex_min:
            errors.append({"path": path, "message": f"value {value} must be > exclusiveMinimum {ex_min}"})
        ex_max = schema.get("exclusiveMaximum")
        if isinstance(ex_max, (int, float)) and value >= ex_max:
            errors.append({"path": path, "message": f"value {value} must be < exclusiveMaximum {ex_max}"})

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append({"path": path, "message": f"array length {len(value)} is shorter than minItems {min_items}"})
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append({"path": path, "message": f"array length {len(value)} exceeds maxItems {max_items}"})
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                errors.extend(validate_json(item, item_schema, f"{path}[{idx}]"))

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append({"path": f"{path}.{key}", "message": f"missing required property '{key}'"})
        properties = schema.get("properties")
        prop_map = properties if isinstance(properties, dict) else {}
        for key, prop_schema in prop_map.items():
            if key in value and isinstance(prop_schema, dict):
                errors.extend(validate_json(value[key], prop_schema, f"{path}.{key}"))
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in value:
                if key not in prop_map:
                    errors.append({"path": f"{path}.{key}", "message": f"additional property '{key}' is not allowed"})
        elif isinstance(additional, dict):
            for key, item_val in value.items():
                if key not in prop_map:
                    errors.extend(validate_json(item_val, additional, f"{path}.{key}"))

    return errors
