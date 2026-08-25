# refuter — D2

_lane: refuter:codex:sol · alias codex-8 · exit 0 · 3634 bytes · review-shaped: yes_

=== completionReport ===
Completion report
- Status: completed
- Reviewed: commit `f627faf`, its parent, six changed files, workflow registry/notification paths, and relevant tests.
- Verification: reproduced the parent rejection and current parser counterexamples; ran isolated state-write probes, `git diff --check`, in-memory compilation, and AST test-collection analysis. The canonical gate was blocked because `uv` could not create its cache lock in the read-only sandbox.
- Files changed: none
- Remaining risk: full test execution was unavailable. Static analysis confirmed one test class, 94 unique methods, and all four new tests without duplication.

VERDICT: refuted (5) — dry-run and nested-workflow states still erase `notify`, so a resumed or approved workflow can silently lose its notification target.

[SEV-1] Notification persistence does not cover every `WorkflowState` · src/delegate_agent/workflows/runtime.py:644
Counterexample: Run a notified workflow containing a gated sub-workflow; the child constructs its state without `notify_target`, writes `paused` with `"notify": null`, and `workflow approve` resumes without supplying a replacement. `emit_dry_run` has the same omission at `src/delegate_agent/workflows/commands.py:309`.
Observed: An isolated state-write probe produced `notify=None`; it also dropped `replayJournal=False`, another rebuild-erased key that can corrupt post-dry-run replay behavior (ran: yes)
Fix: Propagate `notify_target` and `replay_journal` into child and dry-run states, with gate/resume tests.

[SEV-1] Every parse-time option error names `delegate dry-run` · src/delegate_agent/cli_parser.py:456
Counterexample: Parse `delegate --json --pass-through codex safe x`; missing `--notify` values and missing `--prompt-file` values are additional examples.
Observed: The errors contain no dry-run hint at all (ran: yes)
Fix: Add the hint centrally when rendering parser-originated option errors instead of patching selected call sites.

[SEV-2] Corrected commands remain safe and readable · src/delegate_agent/cli_parser.py:890
Counterexample: `delegate --isolation none dry-run cursor work --forbid-commit fix`.
Observed: The suggested corrected command is `delegate --isolation worktree cursor work --forbid-commit fix`, which launches instead of validating. Prompt-file corrections also place the prose hint directly after the copyable command, where it can become extra prompt text (ran: yes)
Fix: Preserve `dry-run` in corrected argv and render the validation hint on a separate line outside the command.

[SEV-2] Notification delivery failures degrade observably · src/delegate_agent/workflows/runtime.py:295
Counterexample: A resolved `post` exits nonzero with an `unknown_room` error.
Observed: `send_notification` returns a failed `NotifyOutcome`, but `notify_event` discards it; no status or journal entry records the reason or stderr detail (derived)
Fix: Persist a best-effort `notify_degraded` event or `lastNotify` payload without changing workflow status.

[SEV-2] Journal rows consistently identify the affected task · src/delegate_agent/workflows/runtime.py:1196
Counterexample: Two parallel structured agents both produce schema-invalid output on attempt one.
Observed: Both `agent_structured_retry` rows contain only engine, attempt, and error, although key, label, and model are in scope. The two `agent_timeout` sites also remain schema-inconsistent: the adoption site has key/scope/runId, while the fixed site has key/label/model/timeout but no scope/runId (derived)
Fix: Define common agent-event identity fields and emit them from retry and both timeout paths.
