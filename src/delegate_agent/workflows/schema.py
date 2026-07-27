from __future__ import annotations

import json

from delegate_agent.json_types import JsonObject

SUPPORTED_KEYS = {
    "type",
    "required",
    "properties",
    "items",
    "enum",
    "additionalProperties",
    "minLength",
    "minItems",
}


class SchemaError(ValueError):
    pass


def validate_schema_subset(schema: object, *, path: str = "schema") -> None:
    if not isinstance(schema, dict):
        raise SchemaError(f"{path} must be an object.")
    unknown = set(schema) - SUPPORTED_KEYS
    if unknown:
        raise SchemaError(f"{path} has unsupported keys: {', '.join(sorted(unknown))}.")
    schema_type = schema.get("type")
    if schema_type is not None:
        allowed = {"object", "array", "string", "number", "integer", "boolean", "null"}
        if isinstance(schema_type, list):
            if not schema_type or any(item not in allowed for item in schema_type):
                raise SchemaError(f"{path}.type has unsupported values.")
        elif schema_type not in allowed:
            raise SchemaError(f"{path}.type has unsupported value: {schema_type!r}.")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(item, str) or not item for item in required)
    ):
        raise SchemaError(f"{path}.required must be an array of non-empty strings.")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise SchemaError(f"{path}.properties must be an object.")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise SchemaError(f"{path}.properties keys must be non-empty strings.")
            validate_schema_subset(child, path=f"{path}.properties.{name}")
    items = schema.get("items")
    if items is not None:
        validate_schema_subset(items, path=f"{path}.items")
    enum = schema.get("enum")
    if enum is not None and not isinstance(enum, list):
        raise SchemaError(f"{path}.enum must be an array.")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise SchemaError(f"{path}.additionalProperties must be a boolean.")
    for keyword, applicable_type in (("minLength", "string"), ("minItems", "array")):
        if keyword not in schema:
            continue
        minimum = schema[keyword]
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise SchemaError(f"{path}.{keyword} must be a non-negative integer.")
        declared_types = schema_type if isinstance(schema_type, list) else [schema_type]
        if schema_type is not None and applicable_type not in declared_types:
            raise SchemaError(f"{path}.{keyword} only applies to {applicable_type} values.")


def validate_value(value: object, schema: JsonObject, *, path: str = "value") -> None:
    validate_schema_subset(schema)
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path} must be one of {schema['enum']!r}.")
    schema_type = schema.get("type")
    if schema_type is not None and not _matches_type(value, schema_type):
        raise SchemaError(f"{path} must be {schema_type!r}.")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise SchemaError(
            f"{path} must contain at least {schema['minLength']} characters (minLength)."
        )
    if isinstance(value, list) and len(value) < schema.get("minItems", 0):
        raise SchemaError(f"{path} must contain at least {schema['minItems']} items (minItems).")
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise SchemaError(f"{path}.{key} is required.")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child in properties.items():
                if key in value:
                    validate_value(value[key], child, path=f"{path}.{key}")
            if schema.get("additionalProperties") is False:
                extra = set(value) - set(properties)
                if extra:
                    raise SchemaError(f"{path} has additional keys: {', '.join(sorted(extra))}.")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_value(item, schema["items"], path=f"{path}[{index}]")


def _matches_type(value: object, schema_type: object) -> bool:
    if isinstance(schema_type, list):
        return any(_matches_type(value, item) for item in schema_type)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return False


def parse_json_tolerant(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip():
            return value
        return value
    except json.JSONDecodeError:
        start_candidates = [idx for idx in (stripped.find("{"), stripped.find("[")) if idx >= 0]
        if not start_candidates:
            raise
        start = min(start_candidates)
        value, _end = decoder.raw_decode(stripped[start:])
        return value


def placeholder(schema: JsonObject) -> object:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = schema_type[0] if schema_type else None
    if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
        return schema["enum"][0]
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        keys = properties if not required else {key: properties.get(key, {}) for key in required}
        return {
            key: placeholder(child if isinstance(child, dict) else {})
            for key, child in keys.items()
        }
    if schema_type == "array" or (
        schema_type is None and ("items" in schema or "minItems" in schema)
    ):
        item_schema = schema.get("items", {})
        return [
            placeholder(item_schema if isinstance(item_schema, dict) else {})
            for _ in range(schema.get("minItems", 0))
        ]
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0
    if schema_type == "boolean":
        return False
    if schema_type == "null":
        return None
    return "x" * schema.get("minLength", 0)
