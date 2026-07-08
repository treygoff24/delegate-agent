from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delegate_agent import run_registry, wait_cancel_commands
from delegate_agent.constants import (
    KNOWN_ENGINES,
    MODE_CALL,
    MODE_SAFE,
    PROMPT_ENFORCED_SAFE_ENGINES,
    PROMPT_INSTRUCTION_MODE_SLASH,
    PROMPT_INSTRUCTION_MODE_WRAPPED,
)
from delegate_agent.json_types import JsonObject
from delegate_agent.workflows import registry
from delegate_agent.workflows import schema as workflow_schema
from delegate_agent.workflows import script as workflow_script

PROMPT_ARGV_GUARD_BYTES = 100 * 1024
DEFAULT_ENGINE = "codex"
DEFAULT_MODE = "safe"
DEFAULT_STRUCTURED_RETRIES = 2
DEFAULT_ITEM_THREADS = 64
ENGINE_ARGV_TRANSPORT = {"cursor", "kimi"}
WORKFLOW_LOCK_FD_ENV = "DELEGATE_WORKFLOW_LOCK_FD"
KILL_SUPERVISOR_WAIT_SECONDS = 5.0
KILL_SUPERVISOR_FORCE_WAIT_SECONDS = 2.0
_MISSING = object()


class BudgetExceeded(RuntimeError):
    pass


class GateExit(RuntimeError):
    pass


@dataclass
class Budget:
    total: int | None
    _spent: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def spent(self) -> int:
        with self._lock:
            return self._spent

    def remaining(self) -> float:
        with self._lock:
            if self.total is None:
                return float("inf")
            return max(self.total - self._spent, 0)

    def claim(self) -> int:
        with self._lock:
            if self.total is not None and self._spent >= self.total:
                raise BudgetExceeded("workflow budget exceeded")
            self._spent += 1
            return self._spent

    def reconcile_spent(self, minimum: int) -> None:
        with self._lock:
            if minimum > self._spent:
                self._spent = minimum


@dataclass
class WorkflowState:
    wf_id: str
    workspace: Path
    root: Path
    script_path: Path
    config: JsonObject
    cli_argv: list[str]
    args: Any
    budget: Budget
    dry_run: bool = False
    depth: int = 0
    namespace: str = "root"
    replay: dict[str, Any] = field(default_factory=dict)
    replay_keys: set[str] = field(default_factory=set)
    started_without_result: set[str] = field(default_factory=set)
    claimed_keys: set[str] = field(default_factory=set)
    sequence: int = 0
    journal_lock: threading.Lock = field(default_factory=threading.Lock)
    scope_lock: threading.Lock = field(default_factory=threading.Lock)
    lifetime_lock: threading.Lock = field(default_factory=threading.Lock)
    lifetime_counter: list[int] = field(default_factory=lambda: [0])
    gate_lock: threading.Lock = field(default_factory=threading.Lock)
    gate_state: dict[str, Any] = field(
        default_factory=lambda: {"stop_admitting": False, "in_flight_agents": 0}
    )
    gate_condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock())
    )
    dry_runs: list[dict[str, Any]] = field(default_factory=list)
    dry_run_budget_spent: int = 0
    supervisor_token: str = field(default_factory=lambda: os.urandom(8).hex())

    def __post_init__(self) -> None:
        self.journal_path = self.root / registry.JOURNAL_FILE
        self.status_path = self.root / registry.STATUS_FILE
        self.result_path = self.root / registry.RESULT_FILE
        self.thread_local = threading.local()
        self.agent_semaphore = threading.Semaphore(_global_agent_cap())
        self.engine_semaphores = _engine_semaphores(self.config)
        self.item_semaphore = threading.Semaphore(_item_thread_cap(self.config))
        self._load_replay()

    def _load_replay(self) -> None:
        for event in registry.iter_journal(self.journal_path):
            seq = event.get("seq")
            if isinstance(seq, int):
                self.sequence = max(self.sequence, seq)
            key = event.get("key")
            if not isinstance(key, str):
                continue
            if event.get("type") == "budget":
                # Idempotent resume: keys already charged must not re-claim.
                self.claimed_keys.add(key)
            elif event.get("type") == "agent_started":
                self.started_without_result.add(key)
            elif event.get("type") == "agent_finished":
                self.replay_keys.add(key)
                self.replay[key] = event.get("result")
                self.started_without_result.discard(key)
        # Budget events are fsynced but status.json is not, so after a hard
        # crash the seeded spent can lag the durable claim set. One claim per
        # key, so spent is at least len(claimed_keys); status can only lag
        # the journal, never lead it.
        self.budget.reconcile_spent(len(self.claimed_keys))

    def append_event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        with self.journal_lock:
            status = registry.read_json(self.status_path)
            last_seq = status.get("lastSeq") if isinstance(status, dict) else None
            if isinstance(last_seq, int):
                self.sequence = max(self.sequence, last_seq)
            self.sequence += 1
            event = {
                "seq": self.sequence,
                "type": event_type,
                "at": run_registry.utc_now_iso(),
                **payload,
            }
            registry.append_jsonl(self.journal_path, event)
            self._write_status_locked(status="running", last_event=event)
            return event

    def _write_status_locked(
        self,
        *,
        status: str,
        last_event: dict[str, Any] | None = None,
        extra: JsonObject | None = None,
    ) -> None:
        payload: JsonObject = {
            "ok": status not in {"failed", "killed"},
            "wfId": self.wf_id,
            "status": status,
            "workspace": str(self.workspace),
            "scriptPath": str(self.script_path),
            "journalPath": str(self.journal_path),
            "resultPath": str(self.result_path),
            "lastSeq": self.sequence,
            "budget": {
                "total": self.budget.total,
                "spent": self.budget.spent(),
                "remaining": _budget_remaining_json(self.budget),
            },
            "supervisorPid": os.getpid(),
            "supervisorPgid": os.getpgrp(),
            "supervisorToken": self.supervisor_token,
            "updatedAt": run_registry.utc_now_iso(),
        }
        if last_event is not None:
            payload["lastEvent"] = last_event
        if extra:
            payload.update(extra)
        registry.write_status(self.root, payload)

    def write_status(self, status: str, **extra: Any) -> None:
        with self.journal_lock:
            self._write_status_locked(status=status, extra=extra)

    def current_scope(self) -> str:
        return getattr(self.thread_local, "scope", self.namespace)

    def next_child_scope(self, kind: str) -> str:
        with self.scope_lock:
            counters = getattr(self.thread_local, "counters", None)
            if counters is None:
                counters = {}
                self.thread_local.counters = counters
            scope = self.current_scope()
            key = f"{scope}:{kind}"
            value = counters.get(key, 0)
            counters[key] = value + 1
            return f"{scope}/{kind}@{value}"

    @contextlib.contextmanager
    def scope(self, value: str):
        previous = self.current_scope()
        previous_counters = getattr(self.thread_local, "counters", None)
        self.thread_local.scope = value
        self.thread_local.counters = {}
        try:
            yield
        finally:
            self.thread_local.scope = previous
            self.thread_local.counters = previous_counters or {}

    def next_agent_path(self) -> str:
        with self.scope_lock:
            counters = getattr(self.thread_local, "counters", None)
            if counters is None:
                counters = {}
                self.thread_local.counters = counters
            scope = self.current_scope()
            key = f"{scope}:seq"
            value = counters.get(key, 0)
            counters[key] = value + 1
            return f"{scope}/seq#{value}"

    def claim_agent_lifetime(self) -> int:
        with self.lifetime_lock:
            self.lifetime_counter[0] += 1
            if self.lifetime_counter[0] > workflow_script.LIFETIME_AGENT_LIMIT:
                raise RuntimeError("workflow exceeded 1000 lifetime agent() calls")
            return self.lifetime_counter[0]

    @contextlib.contextmanager
    def active_agent(self):
        with self.gate_condition:
            if self.gate_state["stop_admitting"]:
                raise GateExit("workflow gate is closed to new agent calls")
            self.gate_state["in_flight_agents"] += 1
        try:
            yield
        finally:
            with self.gate_condition:
                self.gate_state["in_flight_agents"] -= 1
                self.gate_condition.notify_all()

    def close_gate_and_wait(self) -> None:
        with self.gate_condition:
            self.gate_state["stop_admitting"] = True
            while self.gate_state["in_flight_agents"]:
                self.gate_condition.wait(timeout=0.2)

    def inside_item_thread(self) -> bool:
        return bool(getattr(self.thread_local, "item_depth", 0))

    @contextlib.contextmanager
    def item_slot(self, *, bypass: bool = False, pre_acquired: bool = False):
        manager = contextlib.nullcontext() if bypass or pre_acquired else self.item_semaphore
        with manager:
            previous = getattr(self.thread_local, "item_depth", 0)
            self.thread_local.item_depth = previous + 1
            try:
                yield
            finally:
                self.thread_local.item_depth = previous
                if pre_acquired:
                    self.item_semaphore.release()

    def dry_run_budget_tick(self) -> int:
        with self.lifetime_lock:
            self.dry_run_budget_spent += 1
            return self.dry_run_budget_spent


def _global_agent_cap() -> int:
    cpus = os.cpu_count() or 2
    return min(16, max(2, cpus - 2))


def _workflow_config(config: JsonObject) -> dict[str, Any]:
    value = config.get("workflows")
    return value if isinstance(value, dict) else {}


def _budget_remaining_json(budget: Budget) -> int | None:
    remaining = budget.remaining()
    if remaining == float("inf"):
        return None
    return int(remaining)


def _engine_semaphores(config: JsonObject) -> dict[str, threading.Semaphore]:
    caps = _workflow_config(config).get("engineCaps")
    if not isinstance(caps, dict):
        return {}
    result: dict[str, threading.Semaphore] = {}
    for engine, cap in caps.items():
        if engine in KNOWN_ENGINES and isinstance(cap, int) and cap > 0:
            result[engine] = threading.Semaphore(cap)
    return result


def _item_thread_cap(config: JsonObject) -> int:
    value = _workflow_config(config).get("itemThreads", DEFAULT_ITEM_THREADS)
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_ITEM_THREADS


def _structured_retries(config: JsonObject) -> int:
    value = _workflow_config(config).get("structuredOutputRetries", DEFAULT_STRUCTURED_RETRIES)
    if isinstance(value, int) and value >= 0:
        return value
    return DEFAULT_STRUCTURED_RETRIES


def execute_workflow(state: WorkflowState) -> Any:
    source = state.script_path.read_text(encoding="utf-8")
    code = workflow_script.compile_workflow(source, filename=str(state.script_path))
    meta = workflow_script.parse_meta(source, filename=str(state.script_path))
    dsl = WorkflowDsl(state, meta)
    globals_dict: dict[str, Any] = {
        "agent": dsl.agent,
        "pipeline": dsl.pipeline,
        "parallel": dsl.parallel,
        "phase": dsl.phase,
        "log": dsl.log,
        "workflow": dsl.workflow,
        "judges": dsl.judges,
        "args": state.args,
        "budget": state.budget,
    }
    exec(code, globals_dict)
    return globals_dict["__delegate_workflow__"]()


class WorkflowDsl:
    def __init__(self, state: WorkflowState, meta: dict[str, Any]) -> None:
        self.state = state
        self.meta = meta
        defaults = meta.get("defaults")
        self.defaults = defaults if isinstance(defaults, dict) else {}
        self.current_phase: str | None = None

    def phase(self, title: str) -> None:
        self.current_phase = str(title)
        self.state.append_event("phase", phase=self.current_phase)

    def log(self, message: object) -> None:
        self.state.append_event("log", message=str(message))

    def pipeline(self, items: list[Any], *stages: Callable[[Any, Any, int], Any]) -> list[Any]:
        if not isinstance(items, list):
            raise TypeError("pipeline() expects an array")
        if len(items) > workflow_script.ITEM_LIMIT:
            raise ValueError("pipeline() item limit is 4096")
        if any(not callable(stage) for stage in stages):
            raise TypeError("pipeline() stages must be functions")
        base_scope = self.state.next_child_scope("pipeline")
        results: list[Any] = [None] * len(items)
        threads: list[threading.Thread] = []
        bypass_item_cap = self.state.inside_item_thread()
        gate_errors: list[GateExit] = []

        def run_item(index: int, item: Any, pre_acquired: bool) -> None:
            # Nested primitives bypass the item-thread cap so an outer callback
            # cannot hold every slot while waiting for its child item threads.
            with self.state.item_slot(bypass=bypass_item_cap, pre_acquired=pre_acquired):
                previous = item
                with self.state.scope(f"{base_scope}/item#{index}"):
                    for stage_index, stage in enumerate(stages):
                        with self.state.scope(f"{base_scope}/item#{index}/stage#{stage_index}"):
                            try:
                                previous = stage(previous, item, index)
                            except BudgetExceeded:
                                previous = None
                                break
                            except GateExit as exc:
                                gate_errors.append(exc)
                                previous = None
                                break
                            except Exception as exc:
                                self.state.append_event(
                                    "stage_failed",
                                    scope=self.state.current_scope(),
                                    error=str(exc),
                                )
                                previous = None
                                break
                            if previous is None:
                                break
                results[index] = previous

        for index, item in enumerate(items):
            if self.state.gate_state["stop_admitting"] or gate_errors:
                # Gate already closed: short-circuit remaining items without
                # spawning threads that would only block then die on admission.
                break
            pre_acquired = False
            if not bypass_item_cap:
                self.state.item_semaphore.acquire()
                pre_acquired = True
                # Re-check after acquire: with a tight item-thread cap the gate
                # may have closed while we were blocked on the semaphore.
                if self.state.gate_state["stop_admitting"] or gate_errors:
                    self.state.item_semaphore.release()
                    break
            thread = threading.Thread(
                target=run_item, args=(index, item, pre_acquired), daemon=False
            )
            try:
                thread.start()
            except BaseException:
                if pre_acquired:
                    self.state.item_semaphore.release()
                raise
            threads.append(thread)
        for thread in threads:
            thread.join()
        if gate_errors:
            raise gate_errors[0]
        if self.state.gate_state["stop_admitting"]:
            raise GateExit("workflow gate is closed to new agent calls")
        return results

    def parallel(self, thunks: list[Callable[[], Any]]) -> list[Any]:
        if not isinstance(thunks, list):
            raise TypeError("parallel() expects an array")
        if len(thunks) > workflow_script.ITEM_LIMIT:
            raise ValueError("parallel() item limit is 4096")
        if any(not callable(thunk) for thunk in thunks):
            raise TypeError("parallel() items must be functions")
        base_scope = self.state.next_child_scope("parallel")
        results: list[Any] = [None] * len(thunks)
        threads: list[threading.Thread] = []
        bypass_item_cap = self.state.inside_item_thread()
        gate_errors: list[GateExit] = []

        def run_thunk(index: int, thunk: Callable[[], Any], pre_acquired: bool) -> None:
            with (
                self.state.item_slot(bypass=bypass_item_cap, pre_acquired=pre_acquired),
                self.state.scope(f"{base_scope}/thunk#{index}"),
            ):
                try:
                    results[index] = thunk()
                except BudgetExceeded:
                    results[index] = None
                except GateExit as exc:
                    gate_errors.append(exc)
                    results[index] = None
                except Exception as exc:
                    self.state.append_event(
                        "thunk_failed",
                        scope=self.state.current_scope(),
                        error=str(exc),
                    )
                    results[index] = None

        for index, thunk in enumerate(thunks):
            if self.state.gate_state["stop_admitting"] or gate_errors:
                break
            pre_acquired = False
            if not bypass_item_cap:
                self.state.item_semaphore.acquire()
                pre_acquired = True
                if self.state.gate_state["stop_admitting"] or gate_errors:
                    self.state.item_semaphore.release()
                    break
            thread = threading.Thread(
                target=run_thunk, args=(index, thunk, pre_acquired), daemon=False
            )
            try:
                thread.start()
            except BaseException:
                if pre_acquired:
                    self.state.item_semaphore.release()
                raise
            threads.append(thread)
        for thread in threads:
            thread.join()
        if gate_errors:
            raise gate_errors[0]
        if self.state.gate_state["stop_admitting"]:
            raise GateExit("workflow gate is closed to new agent calls")
        return results

    def judges(
        self,
        prompt: str,
        schema: dict[str, Any],
        engines: list[Any] | None = None,
    ) -> list[Any]:
        selected = engines or ["codex"]
        thunks = []
        for item in selected:
            engine, model = _parse_engine_spec(item)
            thunks.append(
                lambda engine=engine, model=model: self.agent(
                    prompt,
                    engine=engine,
                    model=model,
                    mode=MODE_CALL,
                    schema=schema,
                    label=f"judge:{engine if model is None else model}",
                )
            )
        return self.parallel(thunks)

    def workflow(self, name_or_path: str, args: Any = None, gate: bool | str = False) -> Any:
        if self.state.depth >= 3:
            raise RuntimeError("workflow nesting depth exceeded 3")
        child_path = resolve_workflow_reference(name_or_path, self.state.script_path.parent)
        child_source = workflow_script.read_script(child_path)
        workflow_script.check_source(child_source, filename=str(child_path))
        name = Path(name_or_path).stem
        scope = self.state.next_child_scope(f"wf:{name}")
        if self.state.dry_run:
            self.state.dry_runs.append(
                {
                    "scope": scope,
                    "workflow": name,
                    "mode": "workflow",
                    "phase": self.current_phase,
                    "gate": gate,
                }
            )
            self.state.append_event("workflow_stubbed", scope=scope, child=name, gate=gate)
            return None
        child_state = WorkflowState(
            wf_id=self.state.wf_id,
            workspace=self.state.workspace,
            root=self.state.root,
            script_path=child_path,
            config=self.state.config,
            cli_argv=self.state.cli_argv,
            args=args,
            budget=self.state.budget,
            dry_run=self.state.dry_run,
            depth=self.state.depth + 1,
            namespace=scope,
            replay=self.state.replay,
            replay_keys=self.state.replay_keys,
            started_without_result=self.state.started_without_result,
            claimed_keys=self.state.claimed_keys,
            lifetime_counter=self.state.lifetime_counter,
            gate_state=self.state.gate_state,
        )
        child_state.journal_lock = self.state.journal_lock
        child_state.scope_lock = self.state.scope_lock
        child_state.lifetime_lock = self.state.lifetime_lock
        child_state.gate_condition = self.state.gate_condition
        child_state.agent_semaphore = self.state.agent_semaphore
        child_state.engine_semaphores = self.state.engine_semaphores
        child_state.item_semaphore = self.state.item_semaphore
        child_state.thread_local.item_depth = getattr(self.state.thread_local, "item_depth", 0)
        result = execute_workflow(child_state)
        should_gate = gate is True or (gate == "on-failure" and _gate_failed(result))
        if should_gate:
            gate_key = _stable_hash(f"gate:{scope}:{_canonical_json(args)}")
            approved = _approval_allows(self.state.root, gate_key)
            if not approved:
                self.state.close_gate_and_wait()
                self.state.append_event("gate", key=gate_key, child=name, result=result)
                self.state.write_status("paused", gateKey=gate_key, gateResult=result)
                raise GateExit("workflow gate checkpoint reached")
        return result

    def agent(
        self,
        prompt: str,
        engine: str | list[str] | None = None,
        mode: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        schema: dict[str, Any] | None = None,
        label: str | None = None,
        phase: str | None = None,
        isolation: str | None = None,
        passthrough: bool = False,
        timeout: int | float | None = None,
        retries: int | None = None,
    ) -> Any:
        if not isinstance(prompt, str):
            prompt = str(prompt)
        engines = _engine_chain(engine or self.defaults.get("engine") or DEFAULT_ENGINE)
        resolved_mode = mode or self.defaults.get("mode") or DEFAULT_MODE
        if passthrough and resolved_mode == MODE_CALL:
            raise ValueError(
                "passthrough=True with mode='call' is invalid; slash pass-through "
                "needs a work or argv-enforced-safe lane"
            )
        if passthrough and schema is not None:
            raise ValueError("passthrough=True is mutually exclusive with schema=")
        resolved_model = model or self.defaults.get("model")
        resolved_effort = effort or self.defaults.get("effort")
        resolved_isolation = isolation or self.defaults.get("isolation")
        resolved_phase = phase or self.current_phase
        opts = {
            "engine": engines if len(engines) > 1 else engines[0],
            "mode": resolved_mode,
            "model": resolved_model,
            "effort": resolved_effort,
            "schema": schema,
            "isolation": resolved_isolation,
        }
        path = self.state.next_agent_path()
        key = _agent_key(path, prompt, opts)
        if key in self.state.replay_keys:
            result = self.state.replay[key]
            self.state.append_event(
                "agent_cache_hit",
                key=key,
                scope=path,
                label=label,
                phase=resolved_phase,
                result=result,
            )
            return result
        if key in self.state.started_without_result:
            adopted = self._adopt_existing_agent_run(
                key,
                scope=path,
                label=label,
                phase=resolved_phase,
                schema=schema,
                prefer_assistant=schema is not None,
                timeout=timeout,
            )
            if adopted is not _MISSING:
                self.state.replay_keys.add(key)
                self.state.replay[key] = adopted
                self.state.started_without_result.discard(key)
                self.state.append_event(
                    "agent_finished",
                    key=key,
                    scope=path,
                    result=adopted,
                    adopted=True,
                )
                return adopted
            # Failed/cancelled/unparseable/schema-invalid: keep the key in
            # started_without_result and fall through to a live respawn.
        already_claimed = key in self.state.claimed_keys
        if not already_claimed:
            self.state.claim_agent_lifetime()
        if self.state.dry_run:
            if already_claimed:
                spent = self.state.dry_run_budget_spent
            else:
                spent = self.state.dry_run_budget_tick()
                self.state.claimed_keys.add(key)
            remaining = (
                None if self.state.budget.total is None else max(self.state.budget.total - spent, 0)
            )
            self.state.append_event(
                "budget",
                key=key,
                spent=spent,
                total=self.state.budget.total,
                remaining=remaining,
                simulated=True,
            )
            placeholder = workflow_schema.placeholder(schema) if schema else ""
            self.state.dry_runs.append(
                {
                    "scope": path,
                    "engine": engines,
                    "mode": resolved_mode,
                    "phase": resolved_phase,
                    "label": label,
                    "schema": bool(schema),
                }
            )
            self.state.append_event("agent_started", key=key, scope=path, dryRun=True)
            self.state.append_event("agent_finished", key=key, scope=path, result=placeholder)
            return placeholder
        if already_claimed:
            spent = self.state.budget.spent()
        else:
            spent = self.state.budget.claim()
            self.state.claimed_keys.add(key)
        self.state.append_event(
            "budget",
            key=key,
            spent=spent,
            total=self.state.budget.total,
            remaining=_budget_remaining_json(self.state.budget),
        )
        with self.state.active_agent():
            self.state.append_event(
                "agent_started",
                key=key,
                workflowAgentKey=key,
                scope=path,
                label=label,
                phase=resolved_phase,
                engine=engines,
                mode=resolved_mode,
            )
            for candidate in engines:
                try:
                    result = self._run_agent_attempts(
                        candidate,
                        prompt,
                        key=key,
                        mode=resolved_mode,
                        model=resolved_model,
                        effort=resolved_effort,
                        schema=schema,
                        isolation=resolved_isolation,
                        passthrough=passthrough,
                        timeout=timeout,
                        retries=retries,
                    )
                except Exception as exc:
                    self.state.append_event(
                        "agent_failed",
                        key=key,
                        scope=path,
                        engine=candidate,
                        error=str(exc),
                    )
                    result = None
                if result is not None:
                    self.state.replay_keys.add(key)
                    self.state.replay[key] = result
                    self.state.append_event(
                        "agent_finished",
                        key=key,
                        scope=path,
                        engine=candidate,
                        result=result,
                    )
                    return result
            self.state.replay_keys.add(key)
            self.state.replay[key] = None
            self.state.append_event(
                "agent_finished", key=key, scope=path, result=None, exhausted=True
            )
            return None

    def _adopt_existing_agent_run(
        self,
        key: str,
        *,
        scope: str,
        label: str | None,
        phase: str | None,
        schema: dict[str, Any] | None,
        prefer_assistant: bool,
        timeout: int | float | None,
    ) -> Any:
        run_id = _find_workflow_agent_run(self.state.workspace, self.state.wf_id, key)
        if run_id is None:
            return _MISSING
        if not _workflow_run_terminal(self.state.workspace, run_id):
            waited = _wait_for_workflow_agent_run(self.state.workspace, run_id, timeout)
            if not waited and not _workflow_run_terminal(self.state.workspace, run_id):
                # Match live-path timeout: cancel the child; timeout is definitive.
                # The terminal re-check closes the race where the child finished
                # between the wait deadline and the cancel — adopt that instead.
                cancel_workflow_agent_child(self.state.workspace, self.state.wf_id, key)
                self.state.append_event("agent_timeout", key=key, scope=scope, runId=run_id)
                return None
        text = _workflow_agent_run_result(
            self.state.workspace,
            run_id,
            prefer_assistant=prefer_assistant,
        )
        if text is None:
            # Failed/cancelled/unparseable children are not definitive — respawn.
            return _MISSING
        if schema is None:
            result: Any = text
        else:
            try:
                value = workflow_schema.parse_json_tolerant(text)
                workflow_schema.validate_value(value, schema)
                result = value
            except Exception as exc:
                self.state.append_event(
                    "agent_adopt_rejected",
                    key=key,
                    scope=scope,
                    runId=run_id,
                    error=str(exc),
                )
                return _MISSING
        self.state.append_event(
            "agent_adopted",
            key=key,
            workflowAgentKey=key,
            scope=scope,
            label=label,
            phase=phase,
            runId=run_id,
            result=result,
        )
        return result

    def _run_agent_attempts(
        self,
        engine: str,
        prompt: str,
        *,
        key: str,
        mode: str,
        model: str | None,
        effort: str | None,
        schema: dict[str, Any] | None,
        isolation: str | None,
        passthrough: bool,
        timeout: int | float | None,
        retries: int | None,
    ) -> Any:
        if engine not in KNOWN_ENGINES:
            raise ValueError(f"engine must be one of {', '.join(KNOWN_ENGINES)}")
        if mode == MODE_SAFE and passthrough and engine in PROMPT_ENFORCED_SAFE_ENGINES:
            raise ValueError("passthrough=True is not supported for prompt-enforced safe engines")
        if (
            engine in ENGINE_ARGV_TRANSPORT
            and len(prompt.encode("utf-8")) > PROMPT_ARGV_GUARD_BYTES
        ):
            raise ValueError(
                "stage output too large for cursor/kimi argv transport; "
                "route this stage to codex/claude/droid"
            )
        engine_sem = self.state.engine_semaphores.get(engine)
        with self.state.agent_semaphore:
            if engine_sem is None:
                return self._run_structured_or_text(
                    engine,
                    prompt,
                    mode=mode,
                    model=model,
                    effort=effort,
                    schema=schema,
                    isolation=isolation,
                    passthrough=passthrough,
                    timeout=timeout,
                    retries=retries,
                    key=key,
                )
            with engine_sem:
                return self._run_structured_or_text(
                    engine,
                    prompt,
                    mode=mode,
                    model=model,
                    effort=effort,
                    schema=schema,
                    isolation=isolation,
                    passthrough=passthrough,
                    timeout=timeout,
                    retries=retries,
                    key=key,
                )

    def _run_structured_or_text(
        self,
        engine: str,
        prompt: str,
        *,
        mode: str,
        model: str | None,
        effort: str | None,
        schema: dict[str, Any] | None,
        isolation: str | None,
        passthrough: bool,
        timeout: int | float | None,
        retries: int | None,
        key: str,
    ) -> Any:
        if schema is None:
            return self._run_delegate(
                engine,
                prompt,
                mode=mode,
                model=model,
                effort=effort,
                isolation=isolation,
                passthrough=passthrough,
                timeout=timeout,
                output_schema=None,
                prefer_assistant=False,
                workflow_agent_key=key,
            )
        workflow_schema.validate_schema_subset(schema)
        attempts = retries if retries is not None else _structured_retries(self.state.config)
        prior_output = ""
        prior_error = ""
        for attempt in range(attempts + 1):
            attempt_prompt = _correction_prompt(prompt, prior_output, prior_error)
            if engine == "codex":
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", delete=False
                ) as schema_file:
                    json.dump(schema, schema_file)
                    schema_path = schema_file.name
                try:
                    text = self._run_delegate(
                        engine,
                        attempt_prompt,
                        mode=mode,
                        model=model,
                        effort=effort,
                        isolation=isolation,
                        passthrough=False,
                        timeout=timeout,
                        output_schema=schema_path,
                        prefer_assistant=True,
                        workflow_agent_key=key,
                    )
                finally:
                    Path(schema_path).unlink(missing_ok=True)
            else:
                attempt_prompt = _structured_prompt(prompt, schema, prior_output, prior_error)
                text = self._run_delegate(
                    engine,
                    attempt_prompt,
                    mode=mode,
                    model=model,
                    effort=effort,
                    isolation=isolation,
                    passthrough=False,
                    timeout=timeout,
                    output_schema=None,
                    prefer_assistant=True,
                    workflow_agent_key=key,
                )
            try:
                value = workflow_schema.parse_json_tolerant(text or "")
                workflow_schema.validate_value(value, schema)
                return value
            except Exception as exc:
                prior_output = text or ""
                prior_error = str(exc)
                self.state.append_event(
                    "agent_structured_retry",
                    engine=engine,
                    attempt=attempt,
                    error=prior_error,
                )
        return None

    def _run_delegate(
        self,
        engine: str,
        prompt: str,
        *,
        mode: str,
        model: str | None,
        effort: str | None,
        isolation: str | None,
        passthrough: bool,
        timeout: int | float | None,
        output_schema: str | None,
        prefer_assistant: bool,
        workflow_agent_key: str,
    ) -> str | None:
        payload: JsonObject = {
            "engine": engine,
            "mode": mode,
            "prompt": prompt,
        }
        if mode != MODE_CALL:
            payload["cwd"] = str(self.state.workspace)
        if model is not None:
            payload["model"] = model
        if effort is not None:
            payload["reasoningEffort"] = effort
        if isolation is not None:
            payload["isolation"] = isolation
        if output_schema is not None:
            payload["outputSchema"] = output_schema
        payload["workflowAgentKey"] = workflow_agent_key
        payload["promptInstructionMode"] = (
            PROMPT_INSTRUCTION_MODE_SLASH if passthrough else PROMPT_INSTRUCTION_MODE_WRAPPED
        )
        if mode == MODE_CALL:
            payload["readOnly"] = True
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(payload, handle)
            input_path = handle.name
        try:
            argv = [
                *self.state.cli_argv,
                "--json",
                "--group",
                self.state.wf_id,
                "run",
                "--input-json",
                input_path,
            ]
            completed = _run_child_command(argv, cwd=str(self.state.workspace), timeout=timeout)
        except subprocess.TimeoutExpired:
            cancel_workflow_agent_child(self.state.workspace, self.state.wf_id, workflow_agent_key)
            self.state.append_event("agent_timeout", engine=engine, timeout=timeout)
            return None
        finally:
            Path(input_path).unlink(missing_ok=True)
        text = completed.stdout.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(
                stderr or text or f"delegate child failed with {completed.returncode}"
            )
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"delegate child returned invalid JSON: {text[:500]}") from exc
        if not isinstance(result, dict) or not result.get("ok", False):
            return None
        run_id = result.get("runId")
        if isinstance(run_id, str):
            self.state.append_event("agent_child", engine=engine, runId=run_id)
        if isinstance(result.get("text"), str):
            return result["text"]
        assistant = result.get("assistantText")
        if prefer_assistant and isinstance(assistant, str) and assistant.strip():
            return assistant
        report_path = result.get("completionReportPath")
        if isinstance(report_path, str):
            path = Path(report_path)
            if not path.is_absolute():
                path = self.state.workspace / path
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace").strip()
        if isinstance(assistant, str):
            return assistant
        return ""


def _run_child_command(
    argv: list[str],
    *,
    cwd: str,
    timeout: int | float | None,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(  # nosec B603 - argv is Delegate's own validated CLI.
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
        exc.output = stdout
        exc.stderr = stderr
        raise exc
    return subprocess.CompletedProcess(
        argv,
        process.returncode,
        stdout,
        stderr,
    )


def _terminate_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), sig)


def _structured_prompt(
    prompt: str, schema: dict[str, Any], prior_output: str, prior_error: str
) -> str:
    parts = [
        prompt,
        "",
        "Return ONLY a JSON value matching this schema. Fenced JSON is allowed, but no prose.",
        json.dumps(schema, sort_keys=True),
    ]
    if prior_output or prior_error:
        parts.extend(
            [
                "",
                "Your prior output was invalid. Correct it now.",
                "Prior output:",
                prior_output,
                "Validation error:",
                prior_error,
            ]
        )
    return "\n".join(parts)


def _correction_prompt(prompt: str, prior_output: str, prior_error: str) -> str:
    if not prior_output and not prior_error:
        return prompt
    return "\n".join(
        [
            prompt,
            "",
            "Your prior output was invalid. Correct it now.",
            "Prior output:",
            prior_output,
            "Validation error:",
            prior_error,
        ]
    )


def _parse_engine_spec(value: Any) -> tuple[str, str | None]:
    if isinstance(value, dict):
        return str(value.get("engine", DEFAULT_ENGINE)), value.get("model")
    if isinstance(value, str):
        if value in KNOWN_ENGINES:
            return value, None
        return "droid", value
    return DEFAULT_ENGINE, None


def _engine_chain(value: object) -> list[str]:
    if isinstance(value, list):
        chain = [str(item) for item in value if isinstance(item, str) and item]
        return chain or [DEFAULT_ENGINE]
    if isinstance(value, str) and value:
        return [value]
    return [DEFAULT_ENGINE]


def _agent_key(scope_path: str, prompt: str, opts: dict[str, Any]) -> str:
    canonical_opts = _canonical_json(opts)
    return _stable_hash(f"v1:{scope_path}{prompt}{canonical_opts}")


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _gate_failed(result: Any) -> bool:
    return result is None or (isinstance(result, dict) and result.get("ok") is False)


def _approval_allows(root: Path, key: str) -> bool:
    payload = registry.read_json(root / registry.APPROVAL_FILE)
    return (
        isinstance(payload, dict)
        and payload.get("gateKey") == key
        and payload.get("approved") is True
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_workflow_reference(name_or_path: str, parent_script_dir: Path) -> Path:
    raw = str(name_or_path)
    try:
        saved = registry.saved_workflow_path(raw).resolve()
    except ValueError:
        saved = None
    if saved is not None and saved.exists():
        return saved
    candidate = Path(raw).expanduser()
    user_root = registry.user_workflow_root().resolve()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if resolved.exists() and _is_relative_to(resolved, user_root):
            return resolved
        raise RuntimeError(
            "nested workflow paths must be saved workflow names, absolute paths inside "
            "~/.delegate/workflows, or paths inside the parent workflow directory"
        )
    parent_root = parent_script_dir.resolve()
    resolved = (parent_root / candidate).resolve()
    if resolved.exists() and _is_relative_to(resolved, parent_root):
        return resolved
    raise RuntimeError(
        "nested workflow paths must be saved workflow names, absolute paths inside "
        "~/.delegate/workflows, or paths inside the parent workflow directory"
    )


def load_args(root: Path) -> Any:
    payload = registry.read_json(root / registry.ARGS_FILE)
    if not isinstance(payload, dict):
        return None
    return payload.get("args")


def cancel_workflow_children(workspace: Path, wf_id: str) -> list[JsonObject]:
    return _cancel_workflow_runs(workspace, wf_id, workflow_agent_key=None)


def cancel_workflow_agent_child(
    workspace: Path, wf_id: str, workflow_agent_key: str
) -> list[JsonObject]:
    return _cancel_workflow_runs(workspace, wf_id, workflow_agent_key=workflow_agent_key)


def _cancel_workflow_runs(
    workspace: Path, wf_id: str, *, workflow_agent_key: str | None
) -> list[JsonObject]:
    root = run_registry.registry_root_if_exists(workspace) or run_registry.registry_root(workspace)
    if not root.exists():
        return []
    index = run_registry.load_index(root)
    handles: list[str] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(run_id, str) or not isinstance(entry, dict):
            continue
        if entry.get("group") != wf_id:
            continue
        if workflow_agent_key is not None and entry.get("workflowAgentKey") != workflow_agent_key:
            continue
        state = run_registry.load_run_state_or_none(root, run_id)
        if (
            run_registry.status_fields(state).get("effectiveStatus")
            not in run_registry.TERMINAL_STATUSES
        ):
            handles.append(run_id)
    if not handles:
        return []
    command = wait_cancel_commands.CancelCommand(tuple(handles), json_mode=True)
    out = io.StringIO()
    wait_cancel_commands.emit_cancel(command, workspace_path=str(workspace), stdout=out)
    try:
        payload = json.loads(out.getvalue())
    except json.JSONDecodeError:
        return []
    cancelled = payload.get("runs") if isinstance(payload, dict) else None
    return cancelled if isinstance(cancelled, list) else []


def _run_registry_root(workspace: Path) -> Path:
    return run_registry.registry_root_if_exists(workspace) or run_registry.registry_root(workspace)


def _find_workflow_agent_run(workspace: Path, wf_id: str, workflow_agent_key: str) -> str | None:
    root = _run_registry_root(workspace)
    if not root.exists():
        return None
    index = run_registry.load_index(root)
    matches: list[tuple[int, str]] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(run_id, str) or not isinstance(entry, dict):
            continue
        if entry.get("group") != wf_id or entry.get("workflowAgentKey") != workflow_agent_key:
            continue
        ordinal = entry.get("registrationOrdinal", 0)
        matches.append((ordinal if isinstance(ordinal, int) else 0, run_id))
    if not matches:
        return None
    matches.sort()
    return matches[-1][1]


def _workflow_run_terminal(workspace: Path, run_id: str) -> bool:
    root = _run_registry_root(workspace)
    state = run_registry.load_run_state_or_none(root, run_id)
    return (
        run_registry.status_fields(state).get("effectiveStatus") in run_registry.TERMINAL_STATUSES
    )


def _wait_for_workflow_agent_run(workspace: Path, run_id: str, timeout: int | float | None) -> bool:
    command = wait_cancel_commands.WaitCommand(
        (run_id,),
        timeout_seconds=int(timeout or wait_cancel_commands.WAIT_DEFAULT_TIMEOUT_SECONDS),
        interval_seconds=1,
        json_mode=True,
    )
    out = io.StringIO()
    exit_code = wait_cancel_commands.emit_wait(command, workspace_path=str(workspace), stdout=out)
    return exit_code != 124


def _workflow_agent_run_result(
    workspace: Path,
    run_id: str,
    *,
    prefer_assistant: bool,
) -> str | None:
    root = _run_registry_root(workspace)
    state = run_registry.load_run_state_or_none(root, run_id)
    if run_registry.status_fields(state).get("effectiveStatus") != run_registry.STATUS_SUCCEEDED:
        return None
    snapshot = run_registry.load_run_snapshot_or_none(root, run_id)
    assistant = snapshot.get("assistantText") if isinstance(snapshot, dict) else None
    if prefer_assistant and isinstance(assistant, str) and assistant.strip():
        return assistant
    completion = snapshot.get("completionReport") if isinstance(snapshot, dict) else None
    report_path = completion.get("path") if isinstance(completion, dict) else None
    if isinstance(report_path, str):
        path = Path(report_path)
        if not path.is_absolute():
            path = workspace / path
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace").strip()
    if isinstance(assistant, str):
        return assistant
    return ""


@contextlib.contextmanager
def _held_workflow_lock(root: Path):
    raw_fd = os.environ.get(WORKFLOW_LOCK_FD_ENV)
    if raw_fd is not None:
        try:
            fd = int(raw_fd)
        except ValueError:
            fd = registry.acquire_workflow_lock(root)
            close_fd = True
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            close_fd = True
    else:
        fd = registry.acquire_workflow_lock(root)
        close_fd = True
    try:
        yield
    finally:
        if close_fd:
            with contextlib.suppress(OSError):
                os.close(fd)


def run_supervisor(
    *,
    workspace: Path,
    wf_id: str,
    cli_argv: list[str],
    config: JsonObject,
) -> int:
    root = registry.workflow_dir(workspace, wf_id)
    with _held_workflow_lock(root):
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        script_path = root / registry.SCRIPT_FILE
        args = load_args(root)
        budget_payload = status.get("budget") if isinstance(status, dict) else None
        total = budget_payload.get("total") if isinstance(budget_payload, dict) else None
        spent = budget_payload.get("spent") if isinstance(budget_payload, dict) else None
        total_budget = total if isinstance(total, int) else None
        spent_budget = spent if isinstance(spent, int) and spent >= 0 else 0
        state = WorkflowState(
            wf_id=wf_id,
            workspace=workspace,
            root=root,
            script_path=script_path,
            config=config,
            cli_argv=cli_argv,
            args=args,
            budget=Budget(total_budget, spent_budget),
        )
        state.write_status("running")
        try:
            result = execute_workflow(state)
        except GateExit:
            return 0
        except BaseException as exc:
            state.append_event("workflow_failed", error=str(exc))
            registry.write_result(root, {"ok": False, "wfId": wf_id, "error": str(exc)})
            state.write_status("failed", error=str(exc))
            return 1
        registry.write_result(root, {"ok": True, "wfId": wf_id, "result": result})
        state.append_event("workflow_finished", result=result)
        state.write_status("succeeded")
        return 0


def detach_supervisor(argv: list[str], *, cwd: Path, lock_fd: int | None = None) -> None:
    env = os.environ.copy()
    pass_fds: tuple[int, ...] = ()
    if lock_fd is not None:
        os.set_inheritable(lock_fd, True)
        env[WORKFLOW_LOCK_FD_ENV] = str(lock_fd)
        pass_fds = (lock_fd,)
    if os.environ.get("DELEGATE_WORKFLOW_NO_DAEMON") == "1":
        subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            pass_fds=pass_fds,
        )
        return
    first = os.fork()
    if first > 0:
        os.waitpid(first, 0)
        return
    os.setsid()
    second = os.fork()
    if second > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.execvpe(argv[0], argv, env)


def kill_supervisor(pid: int, pgid: int | None, *, force: bool = False) -> bool:
    if pid <= 1 or pgid is None or pgid <= 1:
        return False
    try:
        live_pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    if live_pgid != pgid:
        return False
    sig = signal.SIGKILL if force else signal.SIGTERM
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)
    if force:
        return True
    deadline = time.monotonic() + KILL_SUPERVISOR_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True
        else:
            time.sleep(0.05)
            continue
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    return True


def wait_for_workflow_lock(root: Path, *, timeout_seconds: float) -> bool:
    """Block until the workflow lock can be acquired (supervisor has exited)."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fd = registry.acquire_workflow_lock(root)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
            continue
        with contextlib.suppress(OSError):
            os.close(fd)
        return True


__all__ = [
    "KILL_SUPERVISOR_FORCE_WAIT_SECONDS",
    "KILL_SUPERVISOR_WAIT_SECONDS",
    "Budget",
    "BudgetExceeded",
    "WorkflowState",
    "cancel_workflow_agent_child",
    "cancel_workflow_children",
    "detach_supervisor",
    "execute_workflow",
    "kill_supervisor",
    "run_supervisor",
    "wait_for_workflow_lock",
]
