from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import CodeType

from delegate_agent.constants import (
    KNOWN_ENGINES,
    MODE_CALL,
    MODE_SAFE,
    PROMPT_ENFORCED_SAFE_ENGINES,
    VALID_MODES,
)
from delegate_agent.workflows import schema as workflow_schema

# Workflow ``meta`` blocks are user-authored dict literals parsed with
# ast.literal_eval, so values are arbitrary Python literals; ``object`` keeps
# consumers honest (they must isinstance-narrow before use).
WorkflowMeta = dict[str, object]

SCRIPT_SIZE_LIMIT = 512 * 1024
ITEM_LIMIT = 4096
LIFETIME_AGENT_LIMIT = 1000


@dataclass(frozen=True)
class CheckResult:
    meta: WorkflowMeta
    warnings: tuple[str, ...]


class WorkflowScriptError(ValueError):
    pass


def read_script(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > SCRIPT_SIZE_LIMIT:
        raise WorkflowScriptError("workflow script exceeds 512 KiB limit")
    return data.decode("utf-8")


def wrapped_tree(source: str, *, filename: str) -> ast.Module:
    wrapped = "def __delegate_workflow__():\n" + textwrap.indent(source, "    ")
    try:
        tree = ast.parse(wrapped, filename=filename)
    except SyntaxError as exc:
        raise WorkflowScriptError(str(exc)) from exc
    function = tree.body[0]
    if not isinstance(function, ast.FunctionDef):
        raise WorkflowScriptError("failed to wrap workflow body")
    for node in ast.walk(function):
        if hasattr(node, "lineno"):
            node.lineno = max(1, node.lineno - 1)
        if hasattr(node, "end_lineno") and node.end_lineno is not None:
            node.end_lineno = max(1, node.end_lineno - 1)
    return tree


def _workflow_body(tree: ast.Module) -> list[ast.stmt]:
    function = tree.body[0]
    if not isinstance(function, ast.FunctionDef):
        raise WorkflowScriptError("workflow wrapper missing")
    return list(function.body)


def parse_meta(source: str, *, filename: str = "<workflow>") -> WorkflowMeta:
    tree = wrapped_tree(source, filename=filename)
    for stmt in _workflow_body(tree):
        value_node: ast.AST | None = None
        if isinstance(stmt, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "meta" for target in stmt.targets):
                value_node = stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "meta"
        ):
            value_node = stmt.value
        if value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError) as exc:
            raise WorkflowScriptError("meta must be a pure dict literal") from exc
        if not isinstance(value, dict):
            raise WorkflowScriptError("meta must be a dict literal")
        return value
    return {}


def compile_workflow(source: str, *, filename: str) -> CodeType:
    tree = wrapped_tree(source, filename=filename)
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec")


def check_source(source: str, *, filename: str = "<workflow>") -> CheckResult:
    meta = parse_meta(source, filename=filename)
    tree = wrapped_tree(source, filename=filename)
    warnings = [*_determinism_warnings(tree), *_budget_loop_warnings(tree)]
    _validate_literal_schemas(tree)
    _validate_literal_agent_modes(tree, meta)
    return CheckResult(meta=meta, warnings=tuple(warnings))


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _determinism_warnings(tree: ast.AST) -> list[str]:
    warnings: list[str] = []
    blocked_prefixes = ("time.", "random.", "uuid.")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name_of(node.func) or ""
        if name.startswith(blocked_prefixes) or name == "datetime.now":
            warnings.append(f"determinism warning: {name} may break structural resume")
    return sorted(set(warnings))


def _budget_loop_warnings(tree: ast.AST) -> list[str]:
    warnings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.While, ast.For)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and _name_of(child.func) == "budget.remaining":
                warnings.append("budget warning: budget.remaining() loops need an iteration bound")
                break
    return warnings


def _literal_keyword(call: ast.Call, name: str) -> object:
    for keyword in call.keywords:
        if keyword.arg == name:
            try:
                return ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                return None
    return None


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in call.keywords)


def _validate_literal_schemas(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _name_of(node.func)
        if call_name not in {"agent", "judges"}:
            continue
        schema_value = _literal_keyword(node, "schema")
        if schema_value is None and call_name == "judges" and len(node.args) >= 2:
            try:
                schema_value = ast.literal_eval(node.args[1])
            except (ValueError, TypeError):
                schema_value = None
        if isinstance(schema_value, dict):
            try:
                workflow_schema.validate_schema_subset(schema_value)
            except workflow_schema.SchemaError as exc:
                raise WorkflowScriptError(f"invalid schema literal: {exc}") from exc


def _validate_literal_agent_modes(tree: ast.AST, meta: WorkflowMeta) -> None:
    defaults = meta.get("defaults") if isinstance(meta.get("defaults"), dict) else {}
    default_engine = defaults.get("engine") if isinstance(defaults, dict) else None
    default_mode = defaults.get("mode") if isinstance(defaults, dict) else None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _name_of(node.func) != "agent":
            continue
        mode = _literal_keyword(node, "mode") or default_mode
        engine = _literal_keyword(node, "engine") or default_engine
        engines = engine if isinstance(engine, list) else [engine]
        if mode is not None and mode not in VALID_MODES:
            raise WorkflowScriptError("agent mode must be safe, work, or call")
        if any(item is not None and item not in KNOWN_ENGINES for item in engines):
            raise WorkflowScriptError("agent engine must be a real delegate engine")
        passthrough = _literal_keyword(node, "passthrough") is True
        if not passthrough:
            continue
        if mode == MODE_CALL:
            raise WorkflowScriptError(
                "passthrough=True with mode='call' is invalid; slash pass-through needs "
                "a work or argv-enforced-safe lane"
            )
        if _has_keyword(node, "schema") and _literal_keyword(node, "schema") is not None:
            raise WorkflowScriptError("passthrough=True is mutually exclusive with schema=")
        if mode == MODE_SAFE and any(item in PROMPT_ENFORCED_SAFE_ENGINES for item in engines):
            raise WorkflowScriptError(
                "passthrough=True is not supported for prompt-enforced safe engines"
            )
