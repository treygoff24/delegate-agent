"""Codex strict structured-output schema preflight."""

from __future__ import annotations

import copy
from typing import Any


class SchemaPreflightError(ValueError):
    pass


def normalize_codex_schema(schema: object) -> tuple[dict[str, Any], tuple[str, ...]]:
    if not isinstance(schema, dict):
        raise SchemaPreflightError("schema must be a JSON object.")
    # Codex strict output requires an object-typed root; catch the explicit
    # non-object declaration here instead of letting the child fail with an
    # opaque 400. Type-less roots (e.g. {}) are deliberately left alone.
    if "type" in schema and not _is_object_node(schema):
        raise SchemaPreflightError(
            "schema root must be object-typed for Codex strict structured output."
        )
    normalized = copy.deepcopy(schema)
    injected: list[str] = []
    _normalize_node(normalized, "schema", injected, normalized, set())
    return normalized, tuple(injected)


def _is_object_node(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    return (
        schema_type == "object"
        or (isinstance(schema_type, list) and "object" in schema_type)
        or "properties" in schema
        or "patternProperties" in schema
    )


def _resolve_local_ref(root: dict[str, Any], ref: object, path: str) -> tuple[dict[str, Any], str]:
    if not isinstance(ref, str) or not ref.startswith("#"):
        raise SchemaPreflightError(
            f"{path} external $ref values are unsupported by Codex strict output."
        )
    if ref == "#":
        return root, "schema"
    if not ref.startswith("#/"):
        raise SchemaPreflightError(f"{path} must be a resolvable local JSON Pointer.")
    target: object = root
    target_path = "schema"
    for raw_token in ref[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and token in target:
            target = target[token]
            target_path += f".{token}"
        elif isinstance(target, list) and token.isdigit() and int(token) < len(target):
            target = target[int(token)]
            target_path += f"[{token}]"
        else:
            raise SchemaPreflightError(f"{path} cannot resolve local $ref {ref!r}.")
    if not isinstance(target, dict):
        raise SchemaPreflightError(f"{path} local $ref {ref!r} must resolve to an object schema.")
    return target, target_path


def _normalize_node(
    schema: dict[str, Any],
    path: str,
    injected: list[str],
    root: dict[str, Any],
    seen: set[int],
) -> None:
    if id(schema) in seen:
        return
    seen.add(id(schema))
    ref_target: tuple[dict[str, Any], str] | None = None
    if "$ref" in schema:
        ref_target = _resolve_local_ref(root, schema["$ref"], f"{path}.$ref")
    if "patternProperties" in schema:
        raise SchemaPreflightError(
            f"{path}.patternProperties is unsupported by OpenAI strict mode."
        )
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for index, child in enumerate(all_of):
            if not isinstance(child, dict):
                continue
            child_target = child
            child_path = f"{path}.allOf[{index}]"
            child_seen = set(seen)
            while "$ref" in child_target and id(child_target) not in child_seen:
                child_seen.add(id(child_target))
                child_target, child_path = _resolve_local_ref(
                    root, child_target["$ref"], f"{child_path}.$ref"
                )
            if _is_object_node(child_target):
                raise SchemaPreflightError(
                    f"{path}.allOf with object branches is unsupported; merge the object "
                    "constraints before using Codex strict output."
                )
    is_object = _is_object_node(schema)
    if is_object:
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaPreflightError(f"{path}.properties must be an object.")
        property_names = set(properties)
        required = schema.get("required")
        repaired = False
        if "required" not in schema:
            # Strict mode requires every property to be listed, so an absent
            # `required` has exactly one correct value. Repair it like
            # additionalProperties instead of rejecting the schema; a present
            # but partial list stays an error because the author's intent for
            # the unlisted properties is genuinely ambiguous.
            schema["required"] = list(properties)
            repaired = bool(property_names)
        elif not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            raise SchemaPreflightError(
                f"{path}.required must be an array listing every property exactly."
            )
        elif len(required) != len(set(required)) or set(required) != property_names:
            missing = sorted(property_names - set(required or ()))
            extra = sorted(set(required or ()) - property_names)
            details = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if extra:
                details.append(f"unknown {', '.join(extra)}")
            raise SchemaPreflightError(
                f"{path}.required must list every property exactly ({'; '.join(details)})."
            )
        additional = schema.get("additionalProperties")
        if "additionalProperties" not in schema:
            schema["additionalProperties"] = False
            repaired = True
        elif additional is not False:
            raise SchemaPreflightError(
                f"{path}.additionalProperties must be false for Codex strict output."
            )
        if repaired:
            injected.append(path)
        for name, child in properties.items():
            if not isinstance(child, dict):
                raise SchemaPreflightError(f"{path}.properties.{name} must be an object.")
            _normalize_node(child, f"{path}.properties.{name}", injected, root, seen)
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise SchemaPreflightError(f"{path}.items must be an object.")
        _normalize_node(items, f"{path}.items", injected, root, seen)
    for keyword in ("$defs", "definitions", "dependentSchemas"):
        children = schema.get(keyword)
        if children is None:
            continue
        if not isinstance(children, dict):
            raise SchemaPreflightError(f"{path}.{keyword} must be an object.")
        for name, child in children.items():
            if not isinstance(child, dict):
                raise SchemaPreflightError(f"{path}.{keyword}.{name} must be an object.")
            _normalize_node(child, f"{path}.{keyword}.{name}", injected, root, seen)
    for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
        children = schema.get(keyword)
        if children is None:
            continue
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            raise SchemaPreflightError(f"{path}.{keyword} must be an array of objects.")
        for index, child in enumerate(children):
            _normalize_node(child, f"{path}.{keyword}[{index}]", injected, root, seen)
    for keyword in ("not", "if", "then", "else", "contains", "propertyNames"):
        child = schema.get(keyword)
        if child is None:
            continue
        if not isinstance(child, dict):
            raise SchemaPreflightError(f"{path}.{keyword} must be an object.")
        _normalize_node(child, f"{path}.{keyword}", injected, root, seen)
    if ref_target is not None:
        target, target_path = ref_target
        _normalize_node(target, target_path, injected, root, seen)
