# Phase 0 round 2: fd-inheritance audit

Date: 2026-08-27 UTC
Worktree: `/home/trey-agent/Code/burndown-worktrees/w1-registry`

## Verdict

The fd-inheritance hypothesis is **not a leak in this checkout**. Registry
lock descriptors are opened by `private_io.open_private_file`, which always
adds `O_CLOEXEC`. Every Python `subprocess.run`/`Popen` call uses the POSIX
default `close_fds=True` unless it explicitly passes a narrower `pass_fds`
set. The tracked harness spawn now states `close_fds=True` explicitly.

The one explicit fd-passing path, `workflows.runtime.detach_supervisor`, passes
its workflow-supervisor lock fd, not a registry lock fd; no call chain opens a
registry lock before that handoff. No `os.spawn*` or `posix_spawn` call exists
in `src/delegate_agent`.

## Audit table

| Spawn site / reachable lock scope | Inheritance contract | Finding |
| --- | --- | --- |
| `runner._run_single_tracked_attempt` -> `_launch_tracked_process` while `registry_lock` is held | `Popen(close_fds=True)`; registry fd is `O_CLOEXEC` | Safe; explicit close discipline added |
| `worktree_remove.remove_worktree` -> `git_utils.run_git` while `registry_lock` is held | `subprocess.run` POSIX default `close_fds=True`; no `pass_fds` | Safe |
| `worktree_mgmt._persist_completion_worktree_fields` | Lock scope contains only JSON reads/writes; no spawn | Safe |
| `wait_cancel_commands` locked cancel/terminal writes | Lock scope contains signals and JSON writes; `ps` probe is outside lock | Safe |
| `mail_core` locked send/read/prune paths | `_workspace_root` git probe runs before lock; locked sections do not spawn | Safe |
| `retention` and `worktree_gc` lock scopes | Archive/filesystem work only; Git probes occur outside registry lock | Safe |
| `workflows.runtime.detach_supervisor` | Explicit `pass_fds` contains only `WORKFLOW_LOCK_FD_ENV`; no registry lock is opened | Safe; unrelated fd handoff |

## Regression proof

`RunRegistryTests.test_lock_fd_is_not_inherited_by_sleeper_spawned_under_lock`
spawns a two-second sleeper with `close_fds=False` while the parent holds the
registry lock. The parent exits its lock scope and immediately reacquires the
same lock, proving the `O_CLOEXEC` descriptor is not retained by the child.

This audit was bounded to the registry-lock reachable spawn paths. The linked
worktree source-lock escape remains unreproduced; the permanent independent
watcher in `tests/registry_lock_guard.py` is the recurrence guardrail.
