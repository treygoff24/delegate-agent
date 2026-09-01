from __future__ import annotations

import json
import math

from delegate_agent.json_types import JsonObject, JsonValue

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


# Claude's schema preflight parses numbers at binary64 precision, so integers
# past 2**53 collapse onto their neighbours there; refusing them keeps the
# subset's notion of "distinct" identical to the consumer's.
MAX_EXACT_INTEGER = 2**53


def _json_identity(value: object, *, path: str) -> object:
    """Hashable key under JSON equality: 1 == 1.0, true != 1, NaN is not JSON."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        if abs(value) > MAX_EXACT_INTEGER:
            raise SchemaError(
                f"{path} contains an integer beyond 2**53, which JSON consumers round."
            )
        return ("number", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError(f"{path} contains a non-finite number, which is not JSON.")
        return ("number", int(value) if value.is_integer() else value)
    if value is None or isinstance(value, str):
        return (type(value).__name__, value)
    if isinstance(value, list):
        return ("array", tuple(_json_identity(item, path=path) for item in value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise SchemaError(f"{path} contains an object with non-string keys, which is not JSON.")
        return (
            "object",
            tuple(sorted((key, _json_identity(item, path=path)) for key, item in value.items())),
        )
    raise SchemaError(f"{path} contains a non-JSON value: {type(value).__name__}.")


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
            if len(set(schema_type)) != len(schema_type):
                raise SchemaError(f"{path}.type must not repeat a type.")
        elif schema_type not in allowed:
            raise SchemaError(f"{path}.type has unsupported value: {schema_type!r}.")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(item, str) or not item for item in required)
    ):
        raise SchemaError(f"{path}.required must be an array of non-empty strings.")
    if required and len(set(required)) != len(required):
        raise SchemaError(f"{path}.required must not repeat a property.")
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
    if enum is not None:
        # Claude's --json-schema preflight rejects empty and duplicate enums
        # before launch, so the subset must reject them too or a workflow that
        # validated here fails the moment the child starts.
        if not isinstance(enum, list) or not enum:
            raise SchemaError(f"{path}.enum must be a non-empty array.")
        keys = [_json_identity(item, path=f"{path}.enum") for item in enum]
        if len(set(keys)) != len(keys):
            raise SchemaError(f"{path}.enum must not repeat a value.")
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        validate_schema_subset(additional, path=f"{path}.additionalProperties")
    elif additional is not None and not isinstance(additional, bool):
        raise SchemaError(f"{path}.additionalProperties must be a boolean or a schema.")
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
    if "enum" in schema:
        # Membership under JSON equality, matching the declaration check:
        # Python would accept True for 1 and 1.0 for 1, JSON does not.
        allowed = {_json_identity(item, path=path) for item in schema["enum"]}
        if _json_identity(value, path=path) not in allowed:
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
            additional = schema.get("additionalProperties")
            extra = sorted(set(value) - set(properties))
            if additional is False and extra:
                raise SchemaError(f"{path} has additional keys: {', '.join(extra)}.")
            if isinstance(additional, dict):
                for key in extra:
                    validate_value(value[key], additional, path=f"{path}.{key}")
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


def _schema_requires_non_string_value(schema: JsonObject | None) -> bool:
    if schema is None:
        return False
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return "string" not in schema_type
    return schema_type not in (None, "string") or "properties" in schema


def _decode_string_payload(value: str, schema: JsonObject | None) -> JsonValue:
    """Decode one JSON layer when a provider wraps a structured value in a string."""
    if not _schema_requires_non_string_value(schema):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value
    return decoded if not isinstance(decoded, str) else value


def parse_json_tolerant(text: str, schema: JsonObject | None = None) -> JsonValue:
    """Pull the JSON value out of child output that may be wrapped in prose.

    Children routinely answer with a markdown report whose final fenced block
    is the structured result, and that prose can contain decoy brackets (a
    `[T1]` task tag, a `{run, exit, note}` contract line). Every top-level
    decodable value is collected in text order. A leading value that already
    validates wins; otherwise the last value that validates against ``schema``
    wins, then the last decodable value, so a trailing report block can recover
    from an invalid leading fragment.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    candidates: list[JsonValue] = []
    first_error: json.JSONDecodeError | None = None
    try:
        value, end = decoder.raw_decode(stripped)
        leading_value = _decode_string_payload(value, schema) if isinstance(value, str) else value
        candidates.append(leading_value)
        if not stripped[end:].strip():
            return leading_value
        if schema is None:
            return leading_value
        try:
            validate_value(leading_value, schema)
        except SchemaError:
            pass
        else:
            return leading_value
        position = end
    except json.JSONDecodeError as exc:
        first_error = exc
        position = 0
    while True:
        starts = [
            idx
            for idx in (
                stripped.find("{", position),
                stripped.find("[", position),
                stripped.find('"', position),
            )
            if idx >= 0
        ]
        if not starts:
            break
        start = min(starts)
        try:
            value, end = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        candidates.append(
            _decode_string_payload(value, schema) if isinstance(value, str) else value
        )
        position = start + end
    if not candidates:
        assert first_error is not None
        raise first_error
    if schema is not None:
        for value in reversed(candidates):
            try:
                validate_value(value, schema)
            except SchemaError:
                continue
            return value
    return candidates[-1]


def placeholder(schema: JsonObject) -> JsonValue:
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
