"""Immutable workflow launch pins and the HOME-level supervisor doctor seam.

Workflow runs are deliberately independent from the mutable ``~/.delegate``
runtime.  A pin contains the non-secret configuration and the exact persona
bytes needed by the run, while the executable Python surface is copied into a
content-addressed runtime directory.  The module is intentionally small at the
caller-facing seam: create/load a pin, derive its launch environment/argv, and
reconcile the active-supervisor index.
"""

from __future__ import annotations

import compileall
import hashlib
import json
import os
import re
import shutil
import stat
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
DOCTOR_SCHEMA = "delegate.doctor.v1"
PROMOTION_LOCK_FILE = ".last-promotion.lock"
WORKFLOW_ID_RE = re.compile(r"^wf_[0-9a-f]{12}$")
_PINNED_PERSONA_RESOLVER_ATTR = "_delegate_workflow_pinned_resolver"
_PIN_GUARD_EXIT_CODE = 70


class WorkflowPinError(Exception):
    """A pin cannot be created or trusted for launch."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def require_pinned_persona_resolver() -> None:
    """Hard-exit a pinned process unless sitecustomize installed its resolver.

    CPython reports, but continues after, an exception raised by sitecustomize.
    This guard runs from the CLI's workflow command import after sitecustomize
    and before command dispatch, so a failed patch cannot fall through to
    persona resolution from the live HOME.
    """
    if "DELEGATE_WORKFLOW_PIN" not in os.environ:
        return
    pinned_resolver = getattr(personas, _PINNED_PERSONA_RESOLVER_ATTR, None)
    if pinned_resolver is not None and personas.resolve_persona is pinned_resolver:
        return
    os.write(2, b"workflow_persona_pin_unavailable\n")
    os._exit(_PIN_GUARD_EXIT_CODE)


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
    python_executable: str
    personas: JsonObject
    created_at: str

    @property
    def cli_argv(self) -> list[str]:
        """The executable + entrypoint used by both supervisor and children."""
        return [self.python_executable, str(self.entrypoint)]

    @property
    def environment(self) -> dict[str, str]:
        """Environment additions required to make the pin effective."""
        current = os.environ.get("PYTHONPATH")
        pythonpath = str(self.import_root)
        if current:
            current_entries = [entry for entry in current.split(os.pathsep) if entry != pythonpath]
            pythonpath = os.pathsep.join((pythonpath, *current_entries))
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
        pinned = {}
    if not isinstance(pinned, dict):
        pinned = {}
    try:
        from delegate_agent import personas
    except Exception as exc:
        raise RuntimeError("workflow_persona_pin_unavailable") from exc
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
            from delegate_agent.errors import DelegateError
            raise DelegateError(
                "workspace_persona_refused",
                f"workspace persona {name!r} is refused in safe mode by the workflow pin.",
            )
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

    personas._delegate_workflow_pinned_resolver = resolve
    personas.resolve_persona = resolve


_install()
'''


def _runtime_source_files(source_root: Path | None = None) -> list[tuple[str, bytes]]:
    source_root = source_root or Path(__file__).resolve().parents[1]
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


def live_runtime_digest() -> str:
    """Digest of the runtime this process is executing.

    Launched through ``~/.delegate/bin/delegate.py`` this is the installed
    runtime's identity, which is what ``doctor`` compares against the last
    promotion stamp and what ``promote`` records by default.
    """
    return _runtime_digest(_runtime_source_files())


def entrypoint_path(home: Path | None = None) -> Path | None:
    """The installed launcher, ``~/.delegate/bin/delegate.py``, when present.

    This is deliberately a fixed location rather than ``sys.argv[0]``: the
    console script, ``python -m delegate_agent.cli``, and the profile shell
    shim all reach the same runtime, and the launcher identity must not
    change with the invocation form.
    """
    candidate = (home or Path.home()) / ".delegate" / "bin" / "delegate.py"
    if not candidate.is_file():
        return None
    return candidate


def entrypoint_digest(home: Path | None = None) -> str | None:
    identity = _file_identity(entrypoint_path(home))
    digest = identity.get("sha256") if identity is not None else None
    return digest if isinstance(digest, str) else None


def _file_identity(path: Path | None) -> JsonObject | None:
    """Record bytes and symlink routing without executing an untrusted launcher."""
    if path is None:
        return None
    try:
        links: list[JsonValue] = []
        current = Path(os.path.abspath(path))
        seen: set[Path] = set()
        while current.is_symlink():
            if current in seen or len(links) >= 40:
                return None
            seen.add(current)
            target = os.readlink(current)
            links.append({"path": str(current), "target": target})
            current = Path(os.path.abspath(current.parent / target))
        resolved = current.resolve(strict=True)
        fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
        finally:
            os.close(fd)
        return {
            "path": str(path.absolute()),
            "resolvedPath": str(resolved),
            "sha256": digest.hexdigest(),
            "symlinks": links,
        }
    except (OSError, RuntimeError):
        return None


def _runtime_provenance(home: Path | None = None) -> JsonObject:
    executing_root = Path(__file__).resolve().parents[1]
    installed_root = (home or Path.home()) / ".delegate" / "src"
    launcher = entrypoint_path(home)
    outer = shutil.which("delegate")
    # Wheel/console-script installs have no HOME-level payload. Do not classify
    # an arbitrary checkout as installed merely because it is on PYTHONPATH.
    if not (installed_root / "delegate_agent").is_dir() and executing_root.name in {
        "site-packages",
        "dist-packages",
    }:
        installed_root = executing_root
        launcher = Path(outer) if outer else None
    installed_files = (
        _runtime_source_files(installed_root)
        if (installed_root / "delegate_agent").is_dir()
        else []
    )
    installed_digest = _runtime_digest(installed_files) if installed_files else None
    entry = _file_identity(launcher)
    outer_identity = _file_identity(Path(outer)) if outer else None
    manifest: JsonObject = {
        "importRoot": str(installed_root.resolve()),
        "runtimeDigest": installed_digest,
        "files": [
            {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in installed_files
            if name.startswith("src/delegate_agent/")
        ],
        "entrypoint": entry,
        "pathLauncher": outer_identity,
        "sitecustomize": _file_identity(installed_root / "sitecustomize.py"),
        "executingInterpreter": _file_identity(Path(sys.executable)),
    }
    executing_installed = executing_root == installed_root.resolve() and bool(installed_files)
    return {
        "executingImportRoot": str(executing_root),
        "executionMode": "installed" if executing_installed else "checkout-or-pinned",
        "executingMatchesInstalled": executing_installed,
        "installedRuntimeDigest": installed_digest,
        "installedArtifact": manifest,
        "installedArtifactDigest": _json_digest(manifest),
        "installedArtifactComplete": bool(installed_files and entry and outer_identity),
    }


def _runtime_directory_digest(root: Path) -> str:
    files: list[tuple[str, bytes]] = []
    for source in sorted(root.rglob("*")):
        if source.is_file() and source.suffix not in {".pyc", ".pyo"}:
            files.append((source.relative_to(root).as_posix(), source.read_bytes()))
    return _runtime_digest(files)


def _write_runtime_snapshot(
    root: Path, *, home: Path | None = None
) -> tuple[str, Path, Path, Path]:
    files = _runtime_source_files()
    digest = _runtime_digest(files)
    runtime_pool = pin_root(home) / RUNTIME_DIR
    runtime_root = runtime_pool / digest
    with run_registry.file_lock(runtime_pool / ".runtime-snapshot.lock"):
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
            temporary_root = runtime_pool / f"{digest}.tmp"
            if temporary_root.exists():
                if temporary_root.is_symlink() or not temporary_root.is_dir():
                    raise WorkflowPinError(
                        "runtime_snapshot_collision",
                        f"runtime snapshot temporary path is unsafe: {temporary_root}",
                    )
                shutil.rmtree(temporary_root)
            for relative, content in files:
                target = temporary_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o500 if relative == "bin/delegate.py" else 0o400)
            compileall.compile_dir(str(temporary_root / "src"), quiet=1, legacy=False)
            for cached in temporary_root.rglob("*.pyc"):
                cached.chmod(0o400)
            for directory in sorted(
                (path for path in temporary_root.rglob("*") if path.is_dir()), reverse=True
            ):
                directory.chmod(0o500)
            runtime_pool.chmod(0o700)
            temporary_root.chmod(0o500)
            os.replace(temporary_root, runtime_root)
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
        run_registry.write_json_atomic(config_path, cleaned_config)
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
        run_registry.write_json_atomic(path, payload)
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
    python_executable = runtime.get("pythonExecutable")
    if (
        not isinstance(created_at, str)
        or not isinstance(digest, str)
        or not isinstance(python_executable, str)
        or not python_executable
    ):
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
        python_executable=python_executable,
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


def _reconcile_active_supervisors(*, home: Path | None = None, write: bool = True) -> JsonObject:
    """Drop missing/unlocked workflow entries and return the live index.

    Callers that pass ``write=True`` must hold the index lock. ``write=False``
    returns the same reconciled view without touching the on-disk index and
    needs no lock: the index is only ever replaced atomically, so an
    unlocked read sees a whole old or new file.
    """
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
    if write and (result != payload or path.exists() is False):
        _write_active_index(path, result)
    return result


def reconcile_active_supervisors(*, home: Path | None = None) -> JsonObject:
    """Drop missing/unlocked workflow entries and return the live index."""
    path = active_index_path(home)
    with run_registry.file_lock(path.with_name(f".{path.name}.lock")):
        return _reconcile_active_supervisors(home=home)


def active_supervisors_view(*, home: Path | None = None) -> JsonObject:
    """Reconciled live view of the supervisor index that never writes anything.

    No lock is taken: acquiring one would create the lock file, and ``doctor``
    is admitted through the read-only profile guard on the promise that it
    leaves ``~/.delegate`` untouched.
    """
    return _reconcile_active_supervisors(home=home, write=False)


def register_active_supervisor(
    workflow_id: str,
    *,
    workflow_root: Path,
    workspace: Path,
    pin: WorkflowPin,
    home: Path | None = None,
) -> JsonObject:
    path = active_index_path(home)
    with run_registry.file_lock(path.with_name(f".{path.name}.lock")):
        index = _reconcile_active_supervisors(home=home)
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
        _write_active_index(path, result)
        return result


def doctor(*, home: Path | None = None) -> JsonObject:
    """Read executing/installed artifact identities, stamp, and pinned supervisors."""
    index = active_supervisors_view(home=home)
    entries = index.get("supervisors")
    promotion = _read_promotion(home=home)
    live_digest = live_runtime_digest()
    provenance = _runtime_provenance(home)
    live_entrypoint = entrypoint_path(home)
    live_entrypoint_digest = entrypoint_digest(home)
    stamped_digest = promotion.get("runtimeDigest") if promotion is not None else None
    stamped_entrypoint_digest = promotion.get("entrypointDigest") if promotion is not None else None
    matches = bool(
        promotion
        and promotion.get("artifactVerified") is True
        and provenance["executingMatchesInstalled"]
        and provenance["installedArtifactComplete"]
        and stamped_digest == live_digest == provenance["installedRuntimeDigest"]
        and promotion.get("installedArtifact") == provenance["installedArtifact"]
        and promotion.get("installedArtifactDigest") == provenance["installedArtifactDigest"]
    )
    payload: JsonObject = {
        "ok": True,
        "schema": DOCTOR_SCHEMA,
        "runtimeDigest": live_digest,
        "executingRuntimeDigest": live_digest,
        **provenance,
        "entrypoint": str(live_entrypoint) if live_entrypoint is not None else None,
        "entrypointDigest": live_entrypoint_digest,
        "promotion": promotion,
        "promotionMatchesRuntime": matches,
        "activeSupervisors": entries if isinstance(entries, dict) else {},
    }
    warnings: list[str] = []
    if promotion is None:
        warnings.append(
            "no promotion stamp: the installed runtime has never been recorded; run "
            "'delegate promote --actor <who> --source <commit-or-branch>' after installing."
        )
    elif not matches:
        warnings.append(
            "installed artifact parity is not verified: the executing package, installed "
            "package, launcher manifest, or verified stamp is missing or differs; an artifact "
            "may have changed without 'delegate promote'."
        )
    if (
        isinstance(stamped_entrypoint_digest, str)
        and live_entrypoint_digest is not None
        and stamped_entrypoint_digest != live_entrypoint_digest
    ):
        warnings.append(
            f"installed launcher {live_entrypoint} differs from the one recorded by the last "
            "promotion; the package and its launcher were not promoted together."
        )
    if isinstance(entries, dict) and entries:
        warnings.append(f"{len(entries)} active supervisor(s) are pinned to launch-time runtimes.")
    if not provenance["executingMatchesInstalled"]:
        warnings.append("executing checkout or pinned package is not the installed import root.")
    if warnings:
        payload["warnings"] = warnings
    return payload


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
    source: str,
    runtime_digest: str | None = None,
    home: Path | None = None,
) -> JsonObject:
    """Stamp a deploy/promotion event for the doctor surface.

    ``runtime_digest=None`` records observed executing-package bytes. The lock
    serializes stamp writers, not external installers replacing those bytes.

    Actual deploy mechanics belong to hq tooling; this local seam records only
    the immutable runtime identity, actor, source, and timestamp.
    """
    if not actor.strip() or not source.strip():
        raise WorkflowPinError("invalid_promotion", "actor and source are required")
    if runtime_digest is not None and not runtime_digest.strip():
        raise WorkflowPinError("invalid_promotion", "runtime digest must not be blank")
    path = promotion_path(home)
    run_registry.ensure_private_dir(path.parent)
    # Observe and write under one lock so concurrent promoters cannot reorder
    # their observations. Installer atomicity is outside this seam.
    with run_registry.file_lock(path.with_name(PROMOTION_LOCK_FILE)):
        executing_digest = live_runtime_digest()
        provenance = _runtime_provenance(home)
        payload: JsonObject = {
            "schema": PROMOTION_SCHEMA,
            "actor": actor,
            "source": source,
            "runtimeDigest": runtime_digest if runtime_digest is not None else executing_digest,
            "executingRuntimeDigest": executing_digest,
            **provenance,
            "sourceVerified": False,
            "runtimeDigestOverride": runtime_digest is not None,
            "artifactVerified": bool(
                runtime_digest is None
                and provenance["executingMatchesInstalled"]
                and provenance["installedArtifactComplete"]
                and executing_digest == provenance["installedRuntimeDigest"]
            ),
            "promotedAt": _utc_now(),
        }
        live_entrypoint = entrypoint_path(home)
        if live_entrypoint is not None:
            payload["entrypoint"] = str(live_entrypoint)
            payload["entrypointDigest"] = entrypoint_digest(home)
        active_index = reconcile_active_supervisors(home=home)
        active = active_index.get("supervisors")
        if isinstance(active, dict):
            payload["activeSupervisors"] = active
            if active:
                payload["warnings"] = [
                    f"{len(active)} active supervisor(s) may still use an older pinned runtime."
                ]
        run_registry.write_json_atomic(path, payload)
        path.chmod(0o600)
    return payload


def emit_doctor(*, home: Path | None = None, stdout: TextIO, json_mode: bool = False) -> int:
    payload = doctor(home=home)
    if json_mode:
        print(json.dumps(payload, sort_keys=True), file=stdout)
    else:
        print(f"executing runtime digest: {payload['runtimeDigest']}", file=stdout)
        print(f"execution mode: {payload['executionMode']}", file=stdout)
        print(f"installed runtime digest: {payload['installedRuntimeDigest']}", file=stdout)
        promotion = payload.get("promotion")
        if isinstance(promotion, dict):
            print(
                f"last promotion: {str(promotion.get('runtimeDigest'))[:12]} at "
                f"{promotion.get('promotedAt')} by {promotion.get('actor')} "
                f"({promotion.get('source')})",
                file=stdout,
            )
        else:
            print("last promotion: none", file=stdout)
        active = payload.get("activeSupervisors")
        count = len(active) if isinstance(active, dict) else 0
        print(f"active supervisors: {count}", file=stdout)
        for workflow_id, entry in active.items() if isinstance(active, dict) else ():
            workspace = entry.get("workspace") if isinstance(entry, dict) else None
            print(f"{workflow_id} {workspace or ''}".rstrip(), file=stdout)
        for warning in payload.get("warnings", []):
            if isinstance(warning, str):
                print(f"warning: {warning}", file=stdout)
    return 0


def emit_promote(
    *,
    actor: str,
    source: str,
    runtime_digest: str | None = None,
    home: Path | None = None,
    stdout: TextIO,
    json_mode: bool = False,
) -> int:
    payload = promote(actor=actor, runtime_digest=runtime_digest, source=source, home=home)
    if json_mode:
        print(json.dumps(payload, sort_keys=True), file=stdout)
    else:
        print(f"promoted: {payload['runtimeDigest']} at {payload['promotedAt']}", file=stdout)
    return 0


__all__ = [
    "ACTIVE_INDEX_SCHEMA",
    "DOCTOR_SCHEMA",
    "PIN_SCHEMA",
    "WorkflowPin",
    "WorkflowPinError",
    "active_index_path",
    "active_supervisors_view",
    "create_pin",
    "doctor",
    "emit_doctor",
    "emit_promote",
    "entrypoint_digest",
    "entrypoint_path",
    "live_runtime_digest",
    "load_pin",
    "pin_directory",
    "pin_path",
    "promote",
    "reconcile_active_supervisors",
    "register_active_supervisor",
    "restore_environment",
    "temporarily_apply_environment",
]
