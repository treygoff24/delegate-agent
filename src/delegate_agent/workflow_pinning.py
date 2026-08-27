"""Immutable workflow launch pins and the HOME-level supervisor doctor seam.

Workflow runs are deliberately independent from the mutable ``~/.delegate``
runtime.  A pin contains the non-secret configuration and the exact persona
bytes needed by the run, while the executable Python surface is copied into a
content-addressed runtime directory.  The module is intentionally small at the
caller-facing seam: create/load a pin, derive its launch environment/argv, and
reconcile the active-supervisor index.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from delegate_agent import personas, redaction, run_registry
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.workflows import registry as workflow_registry

PIN_SCHEMA = "delegate.workflow-pin.v1"
PIN_VERSION = 1
PIN_ROOT_DIRNAME = ".delegate-workflow-pins"
PIN_FILE = "pin.json"
PIN_CONFIG_FILE = "config.json"
RUNTIME_DIR = "runtimes"
PERSONA_DIR = "personas"
ACTIVE_INDEX_FILE = "active-supervisors.json"
PROMOTION_FILE = "last-promotion.json"
ACTIVE_INDEX_SCHEMA = "delegate.active-supervisors.v1"
PROMOTION_SCHEMA = "delegate.promotion.v1"
WORKFLOW_ID_RE = re.compile(r"^wf_[0-9a-f]{12}$")


class WorkflowPinError(Exception):
    """A pin cannot be created or trusted for launch."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


@dataclass(frozen=True)
class WorkflowPin:
    workflow_id: str
    path: Path
    config_path: Path
    config: JsonObject
    runtime_digest: str
    runtime_root: Path
    import_root: Path
    entrypoint: Path
    personas: JsonObject
    created_at: str

    @property
    def cli_argv(self) -> list[str]:
        """The executable + entrypoint used by both supervisor and children."""
        return [sys.executable, str(self.entrypoint)]

    @property
    def environment(self) -> dict[str, str]:
        """Environment additions required to make the pin effective."""
        current = os.environ.get("PYTHONPATH")
        pythonpath = str(self.import_root)
        if current:
            pythonpath = pythonpath + os.pathsep + current
        return {
            "DELEGATE_CONFIG": str(self.config_path),
            "DELEGATE_WORKFLOW_PIN": str(self.path),
            "PYTHONPATH": pythonpath,
        }


def pin_root(home: Path | None = None) -> Path:
    """Return the HOME-level pin root (outside the persistent worktree pool)."""
    return (home or Path.home()) / PIN_ROOT_DIRNAME


def pin_directory(workflow_id: str, *, home: Path | None = None) -> Path:
    _validate_workflow_id(workflow_id)
    return pin_root(home) / workflow_id


def pin_path(workflow_id: str, *, home: Path | None = None) -> Path:
    return pin_directory(workflow_id, home=home) / PIN_FILE


def active_index_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".delegate" / ACTIVE_INDEX_FILE


def promotion_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".delegate" / PROMOTION_FILE


def _validate_workflow_id(workflow_id: str) -> None:
    if not isinstance(workflow_id, str) or WORKFLOW_ID_RE.fullmatch(workflow_id) is None:
        raise WorkflowPinError("invalid_workflow_id", f"invalid workflow id: {workflow_id!r}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_digest(payload: JsonValue) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _non_secret_config(value: JsonValue, *, key: str | None = None) -> JsonValue:
    """Copy config while removing credential-shaped keys and masking token values."""
    if key is not None and redaction.key_looks_secret(key):
        return None
    if isinstance(value, dict):
        result: JsonObject = {}
        for child_key, child in value.items():
            if not isinstance(child_key, str) or redaction.key_looks_secret(child_key):
                continue
            cleaned = _non_secret_config(child, key=child_key)
            if cleaned is not None:
                result[child_key] = cleaned
        return result
    if isinstance(value, list):
        return [_non_secret_config(item) for item in value]
    if isinstance(value, str):
        return redaction.redact_string(value)
    return value


_LAUNCHER = b'''#!/usr/bin/env python3
"""Content-addressed Delegate workflow launcher."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from delegate_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
'''


_SITE_CUSTOMIZE = b'''"""Resolve workflow personas from the parent pin before live HOME."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path


def _install() -> None:
    pin_path = os.environ.get("DELEGATE_WORKFLOW_PIN")
    if not pin_path:
        return
    try:
        payload = json.loads(Path(pin_path).read_text(encoding="utf-8"))
        pinned = payload.get("personas", {})
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(pinned, dict) or not pinned:
        return
    try:
        from delegate_agent import personas
    except Exception:
        return
    original = personas.resolve_persona

    def resolve(workspace, name, *, mode=None, allow_repo_persona=False):
        item = pinned.get(name)
        if not isinstance(item, dict):
            from delegate_agent.errors import DelegateError
            raise DelegateError(
                "persona_not_found",
                f"persona {name!r} is not present in the workflow launch pin.",
            )
        source = item.get("source")
        if mode == "safe" and source == "workspace" and not allow_repo_persona:
            return original(workspace, name, mode=mode, allow_repo_persona=allow_repo_persona)
        text = item.get("text")
        digest = item.get("digest")
        if not isinstance(text, str) or not isinstance(digest, str):
            from delegate_agent.errors import DelegateError
            raise DelegateError("invalid_persona", f"pinned persona {name!r} is invalid.")
        raw = text.encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != digest:
            from delegate_agent.errors import DelegateError
            raise DelegateError("invalid_persona", f"pinned persona {name!r} digest mismatch.")
        return personas.PersonaResolution(
            name=name,
            source=source if isinstance(source, str) else "global",
            path=Path(item.get("path", "persona.md")),
            text=text,
            size_bytes=len(raw),
            digest=digest,
            preview=personas.escaped_preview(text),
        )

    personas.resolve_persona = resolve


_install()
'''


def _runtime_source_files() -> list[tuple[str, bytes]]:
    source_root = Path(__file__).resolve().parents[1]
    package_root = source_root / "delegate_agent"
    files: list[tuple[str, bytes]] = []
    for source in sorted(package_root.rglob("*")):
        if source.is_file() and source.suffix not in {".pyc", ".pyo"}:
            files.append(
                (
                    f"src/delegate_agent/{source.relative_to(package_root).as_posix()}",
                    source.read_bytes(),
                )
            )
    files.append(("src/sitecustomize.py", _SITE_CUSTOMIZE))
    files.append(("bin/delegate.py", _LAUNCHER))
    return files


def _runtime_digest(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files):
        name_bytes = name.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _runtime_directory_digest(root: Path) -> str:
    files: list[tuple[str, bytes]] = []
    for source in sorted(root.rglob("*")):
        if source.is_file():
            files.append((source.relative_to(root).as_posix(), source.read_bytes()))
    return _runtime_digest(files)


def _write_runtime_snapshot(
    root: Path, *, home: Path | None = None
) -> tuple[str, Path, Path, Path]:
    files = _runtime_source_files()
    digest = _runtime_digest(files)
    runtime_root = pin_root(home) / RUNTIME_DIR / digest
    if runtime_root.exists():
        for relative, content in files:
            target = runtime_root / relative
            if (
                not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest()
                != hashlib.sha256(content).hexdigest()
            ):
                raise WorkflowPinError(
                    "runtime_snapshot_collision", f"runtime snapshot differs: {target}"
                )
    else:
        for relative, content in files:
            target = runtime_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o500 if relative == "bin/delegate.py" else 0o400)
        for directory in sorted(
            (path for path in runtime_root.rglob("*") if path.is_dir()), reverse=True
        ):
            directory.chmod(0o500)
        runtime_root.parent.chmod(0o700)
        runtime_root.chmod(0o500)
    return digest, runtime_root, runtime_root / "src", runtime_root / "bin" / "delegate.py"


def _persona_records(workspace: Path) -> JsonObject:
    records: JsonObject = {}
    for row in personas.list_personas(workspace):
        name = row.get("name")
        source = row.get("source")
        if not isinstance(name, str) or not isinstance(source, str):
            continue
        try:
            resolution = personas.resolve_persona(
                workspace,
                name,
                mode="work",
                allow_repo_persona=True,
            )
        except DelegateError:
            continue
        records[name] = {
            "source": resolution.source,
            "path": str(resolution.path),
            "digest": resolution.digest,
            "sizeBytes": resolution.size_bytes,
            "text": resolution.text,
        }
    return records


def _write_persona_snapshots(root: Path, records: JsonObject) -> None:
    target_root = root / PERSONA_DIR
    for name, record in records.items():
        if not isinstance(record, dict):
            continue
        digest = record.get("digest")
        text = record.get("text")
        if not isinstance(digest, str) or not isinstance(text, str):
            continue
        target = target_root / f"{digest}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = text.encode("utf-8")
        if target.exists() and target.read_bytes() != raw:
            raise WorkflowPinError(
                "persona_snapshot_collision", f"persona snapshot differs: {name}"
            )
        if not target.exists():
            target.write_bytes(raw)
            target.chmod(0o400)
    if target_root.exists():
        target_root.chmod(0o500)


def create_pin(
    workflow_id: str,
    *,
    workspace: Path,
    config: JsonObject,
    data_home: Path | None = None,
    home: Path | None = None,
) -> WorkflowPin:
    """Create (or verify) the immutable pin for a new workflow."""
    _validate_workflow_id(workflow_id)
    root = pin_directory(workflow_id, home=home)
    configured_pool = (
        (data_home or (home or Path.home()) / ".delegate" / "worktrees").expanduser().resolve()
    )
    if root.resolve().is_relative_to(configured_pool):
        raise WorkflowPinError(
            "pin_root_inside_worktree_pool",
            "workflow pin root must be outside worktrees.dataHome",
        )
    run_registry.ensure_private_dir(root.parent)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    digest, runtime_root, import_root, entrypoint = _write_runtime_snapshot(root, home=home)
    cleaned_config = _non_secret_config(config)
    if not isinstance(cleaned_config, dict):
        cleaned_config = {}
    config_path = root / PIN_CONFIG_FILE
    if config_path.exists():
        existing_config = json.loads(config_path.read_text(encoding="utf-8"))
        if existing_config != cleaned_config:
            raise WorkflowPinError("pin_collision", f"pin config already differs: {config_path}")
    else:
        config_path.write_text(
            json.dumps(cleaned_config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        config_path.chmod(0o400)
    records = _persona_records(workspace)
    _write_persona_snapshots(root, records)
    path = root / PIN_FILE
    created_at = _utc_now()
    payload: JsonObject = {
        "schema": PIN_SCHEMA,
        "version": PIN_VERSION,
        "workflowId": workflow_id,
        "createdAt": created_at,
        "runtime": {
            "digest": digest,
            "root": str(runtime_root),
            "importRoot": str(import_root),
            "entrypoint": str(entrypoint),
            "pythonExecutable": sys.executable,
        },
        "configPath": str(config_path),
        "configDigest": _json_digest(cleaned_config),
        "config": cleaned_config,
        "personas": records,
    }
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorkflowPinError("invalid_pin", f"could not read workflow pin: {path}") from exc
        if isinstance(existing, dict) and isinstance(existing.get("createdAt"), str):
            payload["createdAt"] = existing["createdAt"]
        if existing != payload:
            raise WorkflowPinError("pin_collision", f"pin already differs: {path}")
    else:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o400)
    root.chmod(0o500)
    return load_pin(workflow_id, home=home)


def load_pin(workflow_id: str, *, home: Path | None = None) -> WorkflowPin | None:
    """Load and validate a pin; ``None`` is the explicit pre-pinning path."""
    _validate_workflow_id(workflow_id)
    path = pin_path(workflow_id, home=home)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowPinError("invalid_pin", f"could not read workflow pin: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PIN_SCHEMA:
        raise WorkflowPinError("invalid_pin", f"unsupported workflow pin: {path}")
    if payload.get("workflowId") != workflow_id:
        raise WorkflowPinError("invalid_pin", "workflow pin id does not match its path")
    runtime = payload.get("runtime")
    config = payload.get("config")
    personas_payload = payload.get("personas", {})
    if (
        not isinstance(runtime, dict)
        or not isinstance(config, dict)
        or not isinstance(personas_payload, dict)
    ):
        raise WorkflowPinError(
            "invalid_pin", "workflow pin is missing runtime, config, or personas"
        )
    config_path_value = payload.get("configPath")
    if not isinstance(config_path_value, str):
        raise WorkflowPinError("invalid_pin", "workflow pin config path is invalid")
    config_path = Path(config_path_value)
    if not config_path.is_file():
        raise WorkflowPinError("invalid_pin", f"workflow pin config is missing: {config_path}")
    runtime_values = [runtime.get("root"), runtime.get("importRoot"), runtime.get("entrypoint")]
    if not all(isinstance(value, str) for value in runtime_values):
        raise WorkflowPinError("invalid_pin", "workflow pin runtime paths are invalid")
    runtime_root = Path(runtime_values[0])
    import_root = Path(runtime_values[1])
    entrypoint = Path(runtime_values[2])
    pin_root_path = path.parent.resolve(strict=False)
    runtime_pool = pin_root(home).resolve(strict=False)
    if not config_path.resolve(strict=False).is_relative_to(pin_root_path):
        raise WorkflowPinError("invalid_pin", "workflow pin config escapes its pin directory")
    if (
        not runtime_root.resolve(strict=False).is_relative_to(runtime_pool)
        or not runtime_root.is_dir()
        or not import_root.is_dir()
        or not entrypoint.is_file()
        or not entrypoint.resolve(strict=False).is_relative_to(runtime_root.resolve(strict=False))
    ):
        raise WorkflowPinError("invalid_pin", "workflow pin runtime snapshot is incomplete")
    created_at = payload.get("createdAt")
    digest = runtime.get("digest")
    if not isinstance(created_at, str) or not isinstance(digest, str):
        raise WorkflowPinError("invalid_pin", "workflow pin metadata is incomplete")
    if _runtime_directory_digest(runtime_root) != digest:
        raise WorkflowPinError("invalid_pin", "workflow pin runtime digest does not match snapshot")
    try:
        disk_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowPinError("invalid_pin", "workflow pin config is unreadable") from exc
    if disk_config != config or payload.get("configDigest") != _json_digest(config):
        raise WorkflowPinError("invalid_pin", "workflow pin config does not match its digest")
    return WorkflowPin(
        workflow_id=workflow_id,
        path=path,
        config_path=config_path,
        config=config,
        runtime_digest=digest,
        runtime_root=runtime_root,
        import_root=import_root,
        entrypoint=entrypoint,
        personas=personas_payload,
        created_at=created_at,
    )


def temporarily_apply_environment(pin: WorkflowPin) -> dict[str, str | None]:
    """Apply pin env in the current process, returning prior values for restore."""
    previous: dict[str, str | None] = {}
    for key, value in pin.environment.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value
    return previous


def restore_environment(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _read_active_index(path: Path) -> JsonObject:
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise WorkflowPinError("invalid_active_supervisors", f"could not read {path}") from exc
    if not isinstance(payload, dict):
        raise WorkflowPinError(
            "invalid_active_supervisors", f"invalid active supervisor index: {path}"
        )
    entries = payload.get("supervisors", {})
    if not isinstance(entries, dict):
        raise WorkflowPinError("invalid_active_supervisors", f"invalid supervisor entries: {path}")
    return {"schema": ACTIVE_INDEX_SCHEMA, "supervisors": entries}


def _write_active_index(path: Path, payload: JsonObject) -> None:
    run_registry.ensure_private_dir(path.parent)
    run_registry.write_json_atomic(path, payload)
    path.chmod(0o600)


def reconcile_active_supervisors(*, home: Path | None = None) -> JsonObject:
    """Drop missing/unlocked workflow entries and return the live index."""
    path = active_index_path(home)
    payload = _read_active_index(path)
    entries = payload["supervisors"]
    assert isinstance(entries, dict)
    live: JsonObject = {}
    for workflow_id, entry in entries.items():
        if not isinstance(workflow_id, str) or not isinstance(entry, dict):
            continue
        root_value = entry.get("workflowRoot")
        if not isinstance(root_value, str):
            continue
        root = Path(root_value)
        if root.is_dir() and workflow_registry.supervisor_alive(root):
            live[workflow_id] = entry
    result: JsonObject = {"schema": ACTIVE_INDEX_SCHEMA, "supervisors": live}
    if result != payload or path.exists() is False:
        _write_active_index(path, result)
    return result


def register_active_supervisor(
    workflow_id: str,
    *,
    workflow_root: Path,
    workspace: Path,
    pin: WorkflowPin,
    home: Path | None = None,
) -> JsonObject:
    index = reconcile_active_supervisors(home=home)
    entries = index["supervisors"]
    assert isinstance(entries, dict)
    entries[workflow_id] = {
        "workflowId": workflow_id,
        "workflowRoot": str(workflow_root),
        "workspace": str(workspace),
        "pinPath": str(pin.path),
        "startedAt": pin.created_at,
    }
    result: JsonObject = {"schema": ACTIVE_INDEX_SCHEMA, "supervisors": entries}
    _write_active_index(active_index_path(home), result)
    return result


def doctor(*, home: Path | None = None) -> JsonObject:
    """Return the machine-local running-supervisor view with stale entries fixed."""
    index = reconcile_active_supervisors(home=home)
    entries = index.get("supervisors")
    return {
        "ok": True,
        "schema": ACTIVE_INDEX_SCHEMA,
        "activeSupervisors": entries if isinstance(entries, dict) else {},
        "promotion": _read_promotion(home=home),
    }


def _read_promotion(*, home: Path | None = None) -> JsonObject | None:
    path = promotion_path(home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def promote(
    *,
    actor: str,
    runtime_digest: str,
    source: str,
    home: Path | None = None,
) -> JsonObject:
    """Stamp a deploy/promotion event for the doctor surface.

    Actual deploy mechanics belong to hq tooling; this local seam records only
    the immutable runtime identity, actor, source, and timestamp.
    """
    if not actor.strip() or not source.strip() or not runtime_digest.strip():
        raise WorkflowPinError(
            "invalid_promotion", "actor, source, and runtime digest are required"
        )
    payload: JsonObject = {
        "schema": PROMOTION_SCHEMA,
        "actor": actor,
        "source": source,
        "runtimeDigest": runtime_digest,
        "promotedAt": _utc_now(),
    }
    path = promotion_path(home)
    run_registry.ensure_private_dir(path.parent)
    run_registry.write_json_atomic(path, payload)
    path.chmod(0o600)
    return payload


def emit_doctor(*, home: Path | None = None, stdout: TextIO, json_mode: bool = False) -> int:
    payload = doctor(home=home)
    if json_mode:
        print(json.dumps(payload, sort_keys=True), file=stdout)
    else:
        active = payload.get("activeSupervisors")
        count = len(active) if isinstance(active, dict) else 0
        print(f"active supervisors: {count}", file=stdout)
        for workflow_id, entry in active.items() if isinstance(active, dict) else ():
            workspace = entry.get("workspace") if isinstance(entry, dict) else None
            print(f"{workflow_id} {workspace or ''}".rstrip(), file=stdout)
    return 0


def emit_promote(
    *,
    actor: str,
    runtime_digest: str,
    source: str,
    home: Path | None = None,
    stdout: TextIO,
    json_mode: bool = False,
) -> int:
    payload = promote(
        actor=actor,
        runtime_digest=runtime_digest,
        source=source,
        home=home,
    )
    if json_mode:
        print(json.dumps(payload, sort_keys=True), file=stdout)
    else:
        print(f"promoted: {payload['runtimeDigest']} at {payload['promotedAt']}", file=stdout)
    return 0


__all__ = [
    "ACTIVE_INDEX_SCHEMA",
    "PIN_SCHEMA",
    "WorkflowPin",
    "WorkflowPinError",
    "active_index_path",
    "create_pin",
    "doctor",
    "emit_doctor",
    "emit_promote",
    "load_pin",
    "pin_directory",
    "pin_path",
    "promote",
    "reconcile_active_supervisors",
    "register_active_supervisor",
    "restore_environment",
    "temporarily_apply_environment",
]
