# conventions — D2

_lane: conventions:omp:glm · alias omp-11 · exit 0 · 9359 bytes · review-shaped: yes_

=== completionReport ===
**Delegate completion report**

- **Status:** completed — static review only.
- **What I did:** Read-only conventions review of commit `f627faf` ("workflow: a detached supervisor can ring someone"). Verified the worktree HEAD is `f627faff61cea7d57749357309e058703a15497a` via `.git/worktrees/wt/HEAD`, then reviewed the checked-out tree: `workflows/runtime.py`, `workflows/commands.py`, `workflows/registry.py`, `notify.py`, `cli_parser.py` (full), `constants.py`, `command_help.py` (option specs), the four new tests in `tests/test_workflow_commands.py`, `tests/test_notify.py`, `docs/delegate-workflows.md`, `CHANGELOG.md`.
- **Verification:** **My shell is blocked — this environment has no exec tool at all** (file reads only). Every claim below is **static**, not executed: no `git show`, no gate run, no reproduction at parent `ce295d7`. Parent-commit defect existence is corroborated by in-code incident comments and the new tests' subject matter, not by running them. The reported green gate (2288 passed / 14 skipped) is taken from the task brief.
- **Findings:** 6 (2 correctness gaps in the notify carry across `WorkflowState` constructors, 1 journal-identity gap of the same class the commit fixed, 1 changelog/docs rule, 1 misleading hint wording, 1 silent option no-op). The four new tests are correctly placed as plain methods on the existing `WorkflowCommandTests` — no subclass, no suite duplication.
- **Remaining risks:** findings 1–2 below mean the commit's own invariant ("read once at supervisor start and re-emitted on every status write") does not hold on the dry-run and nested-workflow paths; nothing was executed to confirm at runtime.

---

CONVENTIONS: 6 findings

```
[SLOP] Nested-workflow WorkflowState drops notify_target — a gate inside a sub-workflow, once approved, silences the rest of the run · src/delegate_agent/workflows/runtime.py:653
Pattern: comment-claims-invariant-the-code-doesn't-uphold — the field comment at runtime.py:126 says the target is "re-emitted on every status write," but this second constructor never sets it.
Detail: child events rewrite status.json with "notify": null (payload keys override in registry.write_status, registry.py:110–118), so (a) any crash/kill whose last write came from the child leaves a null target that `run --resume` preserves (commands.py:254 only overrides with an explicit --notify); (b) a gate inside the nested workflow writes the "paused" status from the child state (runtime.py:676) — `workflow approve` then resumes a supervisor that reads notify:null and rings nobody for the remainder; (c) agent timeouts inside nested workflows call notify_event on the child state (runtime.py:1286) and silently never ring.
Fix: add `notify_target=self.state.notify_target,` to the child_state constructor at runtime.py:653.
```

```
[SLOP] Dry-run state drops notify_target — the documented dry-run→live resume silently loses the target · src/delegate_agent/workflows/commands.py:308
Pattern: same broken invariant, third constructor.
Detail: `workflow run script --notify channel:x --dry-run` persists the target at create, then any logged/phased/agent event rewrites status.json with "notify": null (emit_dry_run's state defaults to None). The documented flow "a completed dry-run can also become a live run" (docs/delegate-workflows.md § Gates and resume) then resumes a supervisor that reads null and never pages — the operator explicitly asked. Sub-point: `workflow run --resume <id> --dry-run --notify x` also drops the target: the dry-run early return (commands.py:215) precedes the notify update (commands.py:254).
Fix: in emit_dry_run, read the persisted target like run_supervisor does and pass `notify_target=` to the WorkflowState (one mechanical step mirroring runtime.py:1700).
```

```
[SLOP] DRY_RUN_HINT instructs an operator into a second parse error whenever global options are present · src/delegate_agent/constants.py:168
Pattern: decorative banner — advice appended without checking it survives contact with the command shape it's attached to.
Detail: "prefix it with `delegate dry-run`" fails on `delegate dry-run --json codex safe x` and on the corrected command the same message just printed for the forbid-commit site (`delegate dry-run --isolation worktree codex work --forbid-commit …`): both hit misplaced_global_option in parse_dry_run (cli_parser.py:1266) because global options must precede `dry-run`. That is exactly the pc2_5dbc2b8399154f68 frustration — operator relaunched twice — reintroduced by the remediation text itself.
Fix: reword to "validate without launching: `delegate dry-run <engine> <mode> …` (global options stay ahead of `dry-run`)".
```

```
[SLOP] agent_structured_retry is the journal's remaining impoverished event — the exact defect class this commit fixed · src/delegate_agent/workflows/runtime.py:1196
Pattern: same papercut as pc2_fab29db36bf151c2 — learning which task burned structured retries still means cross-referencing agent_started by timestamp; the event carries only engine/attempt/error, no key/scope/label. Also, the two agent_timeout sites are still not consistent: the live site (runtime.py:1285) records engine/timeout/key/label/model but no scope; the adoption site (runtime.py:972) records key/scope/runId but drops label and engine though both are in scope there.
Fix: add key/scope/label to agent_structured_retry; add scope to the live timeout row and label to the adoption row (union of fields at both sites).
```

```
[RULE] User-visible changes shipped with no changelog entry and no workflow-docs mention · CHANGELOG.md:10 (Unreleased)
Rule: CHANGELOG.md § preamble — "All notable changes to this project are documented in this file."
Detail: at this commit the Unreleased section has no entry for --notify on `workflow run`, the agent_timeout journal schema, or the dry-run hint, and docs/delegate-workflows.md is silent on --notify — the detached-supervisor ringing behavior is precisely what a workflow operator needs documented (when it fires: paused/failed/succeeded/agent timeout; how to set it; that it degrades).
Fix: three Unreleased bullets plus one sentence in docs/delegate-workflows.md § Gates and resume.
```

```
[SLOP] --notify silently accepted-and-ignored for every workflow action except run · src/delegate_agent/cli_parser.py:57
Pattern: silent no-op where the parser's own pinned contract rejects meaningless options — NOTIFY_SUBCOMMANDS admits `workflow` wholesale, but only `run` threads the value through (cli_parser.py:2307); `workflow status/kill/list/check/save` parse `--notify room:x` fine and drop it. The established convention is rejection: tests/test_notify.py § ParserAndDryRunTests pins `--notify` failing closed for non-launch subcommands.
Fix: gate --notify to the `run` action inside parse_workflow (reject for other workflow actions), mirroring the --group treatment.
```

**Attack questions, answered directly (static):**

- **Other paths that lose the target:** kill and approve are clean — `emit_kill` merges into on-disk status (commands.py:560), approve delegates to the resume path, and resume preserves the disk value unless `--notify` is passed explicitly. The losses are exactly the two constructors above (dry-run, nested workflow). **Other rebuild-erased keys:** none unnoticed — `createdAt`, `scriptSha256`, `args`, `createdOrdinal` are explicitly rescued by `registry.write_status` (registry.py:113–121, the dlg-7ay lesson); `notify` is the only create-time key outside that rescue set, and it is *actively* clobbered because `_write_status_locked` always emits it, even `None`. (Nit: the field comment at runtime.py:126 says status.json is "REBUILT from scratch rather than merged" — it is a payload-overwrites merge with a preserved-key whitelist; the invariant claim, not the mechanism, is what matters.)
- **Failure modes worth pinning:** post-absent is pinned at the workflow level (tests/test_workflow_commands.py:291); nonzero exit, stderr-detail, and timeout are pinned at unit level (tests/test_notify.py § SendNotificationTests). The one unpinned seam: the timeout test passes an explicit `timeout=0.2`, while `notify_event` relies on the default `NOTIFY_TIMEOUT_SEC = 10` — nothing pins that a hanging post cannot stall the supervisor unboundedly (it can't today; notify.py:141 defaults the timeout). Low cost, one small test.
- **Tests arrangement:** the four tests are plain methods on the existing `WorkflowCommandTests` (tests/test_workflow_commands.py:198–312), single class, `unittest.main()` footer — they run once each under both runners; the subclass re-run mistake is not present.
- **No test pinned an exact old string:** [INFERENCE] from the reported green gate — any equality assertion on a pre-hint message would now fail; the four launch-grammar sites that actually emit the hint all read acceptably aside from the wording finding above.
- **Observation, not filed:** `raise_misplaced_global_option`'s `argv` branch (cli_parser.py:922) is unreachable from production — every caller passes only a message — so the most common parse-error class (misplaced global options) still never mentions dry-run; the fix effectively covers 4 of its 5 sites. Also a policy call, out of scope per the brief: each timed-out child rings once (runtime.py:1286), so a flaky 42-task workflow can page 42 times.
