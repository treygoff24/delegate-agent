# `dlg-adl`: workflow pins freeze operator configuration

Diagnosis target: HEAD `c8fcab6640311285cd7ae881b811c7b295506e28`.

## Config-path map

### Creation and first launch

1. Every ordinary workflow command first loads and validates one merged config, then dispatches it to the workflow command module. The loader order is embedded defaults, global config and its local overlay, explicit `DELEGATE_CONFIG` and its local overlay, then CLI overrides. (`src/delegate_agent/cli.py:1777-1780`, `src/delegate_agent/cli.py:1894-1895`, `src/delegate_agent/config.py:1483-1523`)
2. A new workflow writes its script, args, budget, and notify target, then calls `create_pin(..., config=config)`. (`src/delegate_agent/workflows/commands.py:227-271`)
3. `create_pin` recursively removes secret-shaped keys, copies every other config value, writes that object to `<pin>/config.json`, and stores the same object and its digest in `pin.json`. Both files become read-only and the pin directory becomes non-writable. (`src/delegate_agent/workflow_pinning.py:144-161`, `src/delegate_agent/workflow_pinning.py:375-443`)
4. `WorkflowPin.environment` unconditionally sets `DELEGATE_CONFIG` to that frozen `config.json`, sets `DELEGATE_WORKFLOW_PIN`, and prepends the pinned runtime import root. (`src/delegate_agent/workflow_pinning.py:86-103`)
5. The launcher temporarily installs that environment before detaching. Detachment copies the current environment and either passes it to `Popen` or `execvpe`, so the new supervisor process receives the frozen config path. (`src/delegate_agent/workflows/commands.py:297-357`, `src/delegate_agent/workflows/runtime.py:4565-4595`)

### Supervisor and child reads

6. The detached process re-enters the normal CLI. Its startup load therefore follows `DELEGATE_CONFIG` to the frozen file before `_supervise` dispatch. (`src/delegate_agent/config.py:1489-1515`, `src/delegate_agent/cli.py:1777-1780`)
7. `_supervise` then loads and validates the pin again, reapplies the same pin environment, and explicitly passes `pin.config`, not the config loaded by CLI startup, to `run_supervisor`. This is a second independent barrier against a live config reaching a pinned supervisor. (`src/delegate_agent/workflows/commands.py:71-99`, `src/delegate_agent/workflow_pinning.py:446-525`)
8. `run_supervisor` stores that object once in `WorkflowState`; semaphores are constructed from it at state initialization and the watchdog threshold is resolved once at supervisor start. There is no config reload loop. (`src/delegate_agent/workflows/runtime.py:583-648`, `src/delegate_agent/workflows/runtime.py:4354-4405`)
9. Each agent/follow-up child is invoked through the pinned CLI argv. The subprocess inherits the supervisor environment except for the workflow lock fd, so the child Delegate CLI again loads the frozen `DELEGATE_CONFIG`. (`src/delegate_agent/workflows/runtime.py:3042-3124`, `src/delegate_agent/workflows/runtime.py:3612-3638`, `src/delegate_agent/workflows/runtime.py:3717-3738`)
10. When Delegate finally launches the external engine, it removes the workflow-pin marker and pinned Python paths but deliberately leaves `DELEGATE_CONFIG` intact. That prevents the engine from importing the pinned Delegate runtime; it does not give the Delegate child a live-config path. (`src/delegate_agent/profiles.py:234-289`, `tests/test_workflow_pinning.py:86-128`)

### Resume and approval

11. `workflow run --resume` loads the existing pin before it acquires the workflow lock. The command process did load the operator's live config, but the resume launcher selects `pin.cli_argv`, applies `pin.environment`, and the `_supervise` process selects `pin.config` again. (`src/delegate_agent/workflows/commands.py:158-169`, `src/delegate_agent/workflows/commands.py:297-357`, `src/delegate_agent/workflows/commands.py:71-89`)
12. `workflow approve` has no separate launch path. It reconstructs gate state, synthesizes `WorkflowCommand("run", resume=wf_id)`, and calls the same resume function, so every approval auto-resume takes the frozen path above. (`src/delegate_agent/workflows/commands.py:648-704`)
13. A live config does still affect command-process housekeeping before workflow dispatch, such as run-log retention. That process exits after detachment and its config is not delivered to `WorkflowState`. (`src/delegate_agent/cli.py:1864-1895`, `src/delegate_agent/retention.py:30-48`)

**Verdict:** For every workflow that has a pin, a config-file edit after creation cannot reach the running supervisor, an explicit resume, an approval auto-resume, or a Delegate child through any current workflow path. The end-to-end gate/approve probe at this HEAD changed the live value from `30` to `3600`; the post-approval child observed `30`, matching the pin. The mechanism is the double selection at launch and `_supervise`, followed by inherited `DELEGATE_CONFIG`. (`src/delegate_agent/workflow_pinning.py:92-103`, `src/delegate_agent/workflows/commands.py:71-89`, `src/delegate_agent/workflows/commands.py:343-357`)

Three qualifications keep the verdict precise:

Legacy workflows with no pin take the explicit fallback and do use the command process's config and CLI. (`src/delegate_agent/workflows/commands.py:75-89`)

`DELEGATE_WORKFLOW_WATCHDOG_TIMEOUT_SECONDS` overrides the config key. A newly launched resume can inherit that environment value because pin application does not overwrite it; an already-running process cannot receive a later shell-environment edit. This escape hatch is present in code and tests but not user documentation. (`src/delegate_agent/workflows/runtime.py:4231-4240`, `src/delegate_agent/workflow_pinning.py:92-103`, `tests/test_workflow_watchdog.py:57-72`)

Workflow dry-run is synchronous and receives the command process's config directly, including on `--resume`; it does not launch a supervisor. (`src/delegate_agent/workflows/commands.py:274-296`, `src/delegate_agent/workflows/commands.py:398-452`)

## Key classification

The table is exhaustive for behavior reads on the workflow supervisor and the Delegate children it launches. Validation reads every supported section at each Delegate CLI start, but validation alone does not make a key operational. (`src/delegate_agent/config.py:1526-1642`)

| Class | Config keys | Consumer and read sites |
|---|---|---|
| **(a) Runtime identity** | `cursor.{argvPrefix,defaultModel,defaultReasoningEffort,reasoningEffortModels,models.*}` | Selects the executable prefix, model, and reasoning route used by a workflow child. (`src/delegate_agent/request_build.py:820-841`, `src/delegate_agent/request_build.py:2639-2767`, `src/delegate_agent/request_build.py:3743-3766`) |
| **(a) Runtime identity** | `{droid,codex,claude,grok,devin,kimi}.{binary,defaultModel,models.*}`; `{droid,codex,claude,grok}.defaultReasoningEffort` | Selects the engine executable, model/alias, and effective effort. (`src/delegate_agent/request_build.py:2790-3103`, `src/delegate_agent/request_build.py:3353-3386`, `src/delegate_agent/request_build.py:3743-3766`, `src/delegate_agent/argv_builders.py:179-238`, `src/delegate_agent/argv_builders.py:241-459`, `src/delegate_agent/argv_builders.py:584-693`) |
| **(a) Runtime identity** | `codex.{profile,fallbackProfile,workSandbox,ephemeral,ignoreUserConfig}` | Changes auth identity, sandbox, session persistence, and user-config loading. (`src/delegate_agent/profiles.py:128-146`, `src/delegate_agent/profiles.py:215-230`, `src/delegate_agent/argv_builders.py:604-681`) |
| **(a) Runtime identity** | `claude.{workPermissionMode,noSessionPersistence,bare}`; `grok.{workPermissionMode,safePermissionMode,safeSandbox,workSandbox,disableWebSearch,noSubagents}` | Changes child permissions, sandboxing, tools, web access, and session behavior. (`src/delegate_agent/argv_builders.py:241-339`, `src/delegate_agent/argv_builders.py:342-410`) |
| **(a) Runtime identity** | `opencode.{binary,defaultModel,defaultReasoningEffort,defaultAgent,models.*.{model,variant}}`; `{pi,omp}.{binary,defaultModel,defaultReasoningEffort,models.*.{model,thinking}}` | Changes engine, agent, model, and native reasoning transport. (`src/delegate_agent/request_build.py:1062-1187`, `src/delegate_agent/request_build.py:3130-3350`, `src/delegate_agent/argv_builders.py:462-581`) |
| **(a) Runtime identity** | `reasoning.capabilities.{codex,droid,grok}.{model}.{supported,default}` | Supplies per-model reasoning declarations used during child request construction. (`src/delegate_agent/reasoning.py:302-310`, `src/delegate_agent/config.py:1185-1248`) |
| **(a) Runtime identity** | `policy.profile`; `policy.{safe,work}.{networkAccess,webSearch,bypassApprovalsAndSandbox,bypassHookTrust}`; the same mode keys under `policy.harness.*.{safe,work}` | Determines the permission and network envelope translated into engine argv. Live changes here would change replay authority, not merely operations. (`src/delegate_agent/config.py:338-359`, `src/delegate_agent/argv_builders.py:110-137`, `src/delegate_agent/argv_builders.py:604-630`) |
| **(a) Runtime identity** | `profiles.{detectFrom,default,definitions.*.env.*}` | Selects account/profile environment, including Codex primary and fallback homes. (`src/delegate_agent/profiles.py:47-59`, `src/delegate_agent/profiles.py:111-125`, `src/delegate_agent/profiles.py:154-230`) |
| **(a) Runtime identity** | `isolation.{safe,work,safeBackend,bwrapBinds[].{path,mode}}`; `worktrees.dataHome` | Selects the workspace isolation mode/backend, host bind surface, and persistent worktree pool. These values define the child trust boundary and filesystem identity. (`src/delegate_agent/config.py:1269-1320`, `src/delegate_agent/sandbox_bwrap.py:90-155`, `src/delegate_agent/isolation.py:242-256`) |
| **(a) Runtime identity** | `personas.forceTransport`; `tracking.completionReport.defaultMode`; `mail.enabled` | Changes persona transport, prompt framing/completion-report contract, and the mail-aware work prompt supplied to children. (`src/delegate_agent/request_build.py:170-212`, `src/delegate_agent/request_build.py:635-696`, `src/delegate_agent/request_build.py:3508-3533`) |
| **(b) Operational safety** | `workflows.engineCaps.*`, `workflows.itemThreads` | Caps concurrent engines and item workers; semaphores are instantiated once per supervisor attempt. (`src/delegate_agent/workflows/runtime.py:640-648`, `src/delegate_agent/workflows/runtime.py:1495-1510`) |
| **(b) Operational safety** | `workflows.structuredOutputRetries` | Controls structured child and follow-up retry counts, and therefore cost/budget pressure. It is read when each structured operation starts. (`src/delegate_agent/workflows/runtime.py:1520-1524`, `src/delegate_agent/workflows/runtime.py:2835-2847`, `src/delegate_agent/workflows/runtime.py:3561-3567`) |
| **(b) Operational safety** | `workflows.watchdogTimeoutSeconds` | Sets the supervisor stale threshold once per attempt, after the environment override. Invalid or absent values fall back to `5.0s`. The key is consumed but is not validated by `_validate_workflows_section` at HEAD. (`src/delegate_agent/workflows/runtime.py:65-70`, `src/delegate_agent/workflows/runtime.py:4231-4240`, `src/delegate_agent/config.py:611-645`) |
| **(b) Operational safety** | `workflows.stallMinutes`, fallback `stallMinutes` | Sets each tracked child run's no-progress watchdog. (`src/delegate_agent/config.py:297-319`, `src/delegate_agent/request_build.py:3627-3636`) |
| **(b) Operational safety** | `tracking.processGroupTerminationGraceSec`; `tracking.{registryLockTimeoutSec,registryLockTimeoutSeconds}` | Controls child process-group termination grace and registry-lock wait. Both also have defaults; registry lock has an environment override. (`src/delegate_agent/config.py:278-286`, `src/delegate_agent/request_build.py:3586-3594`, `src/delegate_agent/request_build.py:3630-3636`, `src/delegate_agent/run_registry.py:135-168`) |
| **(b) Operational safety** | `worktrees.{retireWorktreeOnCompletion,retirementIgnoreGlobs,autoPrune.enabled,autoPrune.mergedOlderThanDays}` | Governs completion-time retirement and pruning of retry worktrees. (`src/delegate_agent/workflows/runtime.py:458-475`, `src/delegate_agent/config.py:1661-1705`) |
| **(b) Operational/observability** | `progress.{enabled,initialDelaySec,intervalSec}` | Controls child progress reporting and timing. (`src/delegate_agent/request_build.py:341-362`, `src/delegate_agent/request_build.py:1813-1827`) |
| **(b), but no detached supervisor** | `workflows.dryRunTimeoutSeconds` | Controls the synchronous dry-run ceiling and already comes from the command process's live config. Like the watchdog key, it is consumed but not validated by `_validate_workflows_section`. (`src/delegate_agent/config.py:1671-1680`, `src/delegate_agent/workflows/commands.py:415-452`, `src/delegate_agent/config.py:611-645`) |
| **(c) Irrelevant to supervisor execution** | `tracking.retention.{enabled,rawLogDays}` | CLI housekeeping may read it before dispatch, but `WorkflowState` and its children do not use it to execute the workflow. (`src/delegate_agent/cli.py:1864-1882`, `src/delegate_agent/retention.py:30-48`) |
| **(c) Irrelevant to supervisor execution** | `worktrees.poolWarnCount` | Produces only an advisory warning when a persistent child worktree is created. (`src/delegate_agent/worktree_execution.py:121-160`) |
| **(c) Validation-only** | `kimi.defaultReasoningEffort`, `devin.defaultReasoningEffort` | Both must be null; non-null values are rejected and neither engine consumes an effort setting. (`src/delegate_agent/config.py:817-827`, `src/delegate_agent/config.py:922-932`, `src/delegate_agent/request_build.py:3068-3084`, `src/delegate_agent/request_build.py:3353-3369`) |
| **(c) Unrecognized extensions** | Any other non-secret key | Pinning recursively copies arbitrary non-secret keys, while the supervisor's behavioral read sites are the finite set above. Such keys enlarge and diverge the pin without affecting execution. (`src/delegate_agent/workflow_pinning.py:144-161`, `src/delegate_agent/workflow_pinning.py:398-429`) |

Budgets and notify targets are operational controls but are not config keys. They live in `status.json`: an explicit resume can replace the budget total or notify target, and approval preserves the existing values. They are therefore not frozen by `DELEGATE_CONFIG`. (`src/delegate_agent/workflows/commands.py:199-222`, `src/delegate_agent/workflows/commands.py:308-340`, `src/delegate_agent/workflows/runtime.py:4363-4392`)

## Design analysis

### A. Live-read category-(b) keys at each use site

**Consistency:** Weak. `engineCaps` and `itemThreads` are materialized at state construction, watchdog timeout is materialized at supervisor start, and retry counts and cleanup policy are read later. Editing one live file could therefore create several effective times within one attempt. (`src/delegate_agent/workflows/runtime.py:640-648`, `src/delegate_agent/workflows/runtime.py:1495-1524`, `src/delegate_agent/workflows/runtime.py:2835-2847`, `src/delegate_agent/workflows/runtime.py:4393-4405`)

**Split brain and replay:** High risk unless children receive the same resolved overlay. Today every child starts another Delegate CLI and loads `DELEGATE_CONFIG`; merely changing supervisor read sites would leave child stall, termination, progress, and cleanup settings on the pin. (`src/delegate_agent/workflows/runtime.py:3115-3124`, `src/delegate_agent/workflows/runtime.py:3717-3738`, `src/delegate_agent/request_build.py:3586-3636`)

**Surface and failures:** Broad and shallow. Every category-(b) use site gains file I/O, validation, fallback, and provenance logic. A malformed live file forces a bad choice during execution: refuse the running attempt or fall back. The current threshold helper sends invalid or missing values to `5.0s`, which would recreate the false-kill. (`src/delegate_agent/workflows/runtime.py:4231-4240`)

**Watchdog-fix interaction:** It can change the threshold during a run, but it does not address the separate false-positive branches and debounce gap in `dlg-dxd`. (`src/delegate_agent/workflows/runtime.py:4299-4351`)

### B. Warn on divergence and keep freezing everything

**Consistency:** Strong. Supervisor, children, and replay remain byte-consistent because behavior does not change. (`src/delegate_agent/workflow_pinning.py:505-525`, `src/delegate_agent/workflows/runtime.py:3725-3738`)

**Surface:** Moderate. The comparison belongs at the explicit resume seam used by approval, with one structured divergence record projected to command stderr, journal, and status. Existing resume warnings and stderr emission provide part of that interface. (`src/delegate_agent/workflows/commands.py:191-198`, `src/delegate_agent/workflows/commands.py:381-387`, `src/delegate_agent/workflows/commands.py:648-704`)

**Failures:** It diagnoses but does not heal. Detached supervisor stderr is `/dev/null`, so warning only inside `_supervise` is useless; the command process must emit it before detachment and persist it. Operators can still miss a warning, automation can ignore it, and the watchdog remains at the unsafe pinned value. (`src/delegate_agent/workflows/runtime.py:4592-4595`)

**Watchdog-fix interaction:** No behavioral conflict, but no protection if the watchdog lane retains or transiently reaches a false-kill path. (`src/delegate_agent/workflows/runtime.py:4299-4351`)

### C. Dedicated live ops-overlay file

**Consistency:** Good only if the overlay is read once per supervisor attempt, validated against an allowlist, and the resulting effective config is shared with all Delegate children. Reading it at each use has design A's split-time problem. (`src/delegate_agent/workflows/runtime.py:583-648`, `src/delegate_agent/workflows/runtime.py:3717-3738`)

**Surface:** A new file path, schema, loader, merge rule, precedence rule, status/journal provenance, and child-delivery mechanism. The existing config loader cannot identify the operator source after pin application because `DELEGATE_CONFIG` has already been replaced with the pin path. (`src/delegate_agent/workflow_pinning.py:92-103`, `src/delegate_agent/config.py:1502-1515`)

**Failures:** The second file creates a second operator mental model. Missing, malformed, wrong-profile, or stale overlay files need a fail-closed policy; silently falling back to the pin recreates the current defect. A mutable shared file also needs atomic writes if it is read during an attempt. (`src/delegate_agent/config.py:1473-1480`, `src/delegate_agent/config.py:1502-1515`)

**Watchdog-fix interaction:** Clean if attempt-scoped. The watchdog lane receives one validated threshold for the attempt while its debounce/read-error fix remains independent. (`src/delegate_agent/workflows/runtime.py:4393-4405`)

### D. Re-snapshot the whole config on resume

**Consistency:** Poor. A resumed replay could change binaries, models, accounts, policy, isolation, prompt framing, and cleanup behavior while retaining the old runtime, script, journal, and persona bytes. (`src/delegate_agent/workflow_pinning.py:3-8`, `src/delegate_agent/workflow_pinning.py:409-429`, `src/delegate_agent/workflows/runtime.py:652-749`)

**Surface:** It conflicts with the current immutable/digest-validated pin contract and file modes. Implementing it honestly requires pin revisions and provenance rather than overwriting `config.json`. (`src/delegate_agent/workflow_pinning.py:401-443`, `src/delegate_agent/workflow_pinning.py:505-525`)

**Failures:** It heals only on resume, does nothing for a running supervisor, and creates non-deterministic replay under one workflow id. A failed rewrite can also make the pin unloadable. (`src/delegate_agent/workflow_pinning.py:446-512`)

**Watchdog-fix interaction:** The timeout refreshes, but every category-(a) key changes with it; the cure is much wider than the watchdog defect. (`src/delegate_agent/workflows/runtime.py:4231-4240`)

### E. Attempt-scoped, allowlisted operational snapshot

This is a narrower layered design. On each new launch, explicit resume, or approval auto-resume, merge only category-(b) keys from the command process's already-loaded live config over the immutable pin. Materialize that effective object as a read-only per-attempt config with a digest and source record. Pass the same path to the pinned supervisor and every Delegate child for that attempt. Do not mutate `pin.json` or the creation config. The current resume command already possesses both inputs, `config` and `pin`, at the correct seam. (`src/delegate_agent/workflows/commands.py:142-169`, `src/delegate_agent/workflows/commands.py:297-357`)

**Consistency:** Strong within an attempt. Category-(a) keys still come from the creation pin; category-(b) keys refresh only at a named attempt boundary. Supervisor and children share one effective file, so no split brain. Replay is deterministic relative to an `(attempt, effectiveConfigDigest)` pair. (`src/delegate_agent/workflows/runtime.py:583-648`, `src/delegate_agent/workflows/runtime.py:3725-3738`)

**Surface:** One deep merge/projection module at the launch seam, one per-attempt artifact, pin-environment support for an alternate effective config path, and status/journal fields such as `effectiveConfigDigest`, `opsSource`, and `opsChangedKeys`. `_supervise` must accept the effective artifact instead of replacing it with `pin.config`. (`src/delegate_agent/workflow_pinning.py:86-103`, `src/delegate_agent/workflows/commands.py:71-89`, `src/delegate_agent/workflows/runtime.py:1166-1196`)

**Failures:** Missing or invalid live category-(b) values must refuse the new attempt loudly; fallback to stale pinned ops would repeat this incident. A currently running attempt does not change under a file edit; the emergency path remains the environment override followed by restart/resume, or a later explicit `workflow reload-ops` command. (`src/delegate_agent/workflows/runtime.py:4231-4240`)

**Watchdog-fix interaction:** Cleanest of the options. `dlg-dxd` owns detection reliability; this design owns threshold delivery. Each attempt constructs one watchdog with the refreshed threshold, so debounce behavior is stable for that attempt and the two fixes can be tested independently. (`src/delegate_agent/workflows/runtime.py:4299-4351`, `src/delegate_agent/workflows/runtime.py:4393-4405`)

## Recommendation

Recommendation: approve E, attempt-scoped allowlisted operational snapshots. Pins should freeze runtime identity and the security envelope, but not silently freeze operator scheduling, timeout, termination, observability, or cleanup controls across supervisor attempts. Attempt boundaries are the right seam: the explicit resume and approval paths already converge there, and one immutable effective config can serve both supervisor and children. (`src/delegate_agent/workflows/commands.py:297-357`, `src/delegate_agent/workflows/commands.py:648-704`)

Do not live-read at each use site, and do not rewrite the creation pin. Record the effective digest and changed operational keys in both journal and status so an operator can prove what a supervisor received. Keep the dedicated watchdog environment override as highest precedence for a newly launched attempt, but record that source too. (`src/delegate_agent/workflows/runtime.py:1166-1196`, `src/delegate_agent/workflows/runtime.py:4231-4240`)

The initial allowlist should be exactly category (b) above except `workflows.dryRunTimeoutSeconds`, which already uses the live command config and has no detached attempt. Keep `policy`, engine/profile/model settings, isolation, worktree pool location, persona transport, completion-report framing, and mail prompt behavior pinned. (`src/delegate_agent/workflows/commands.py:398-452`, `src/delegate_agent/config.py:338-359`, `src/delegate_agent/config.py:1269-1320`)

## Test plan

1. **Exact regression, explicit resume:** create a pinned paused workflow with watchdog `30`, change live config to `3600`, resume, and assert status records `3600` plus the attempt digest while `pin.json` still records `30`. Place it beside existing pin/resume integration coverage. (`tests/test_workflow_commands.py:780-811`, `tests/test_workflow_commands.py:911-933`)
2. **Exact regression, approval:** repeat through `workflow approve`; the first post-gate child must observe the attempt config and `3600`. This pins the auto-resume path that failed in the field. (`tests/test_workflow_commands.py:1555-1594`, `src/delegate_agent/workflows/commands.py:691-704`)
3. **Identity stays pinned:** in the same live edit, change `codex.binary`, model, profile, policy, and isolation. Assert the resumed child still uses the creation values while category-(b) values refresh. (`src/delegate_agent/request_build.py:2867-2936`, `src/delegate_agent/config.py:338-359`, `src/delegate_agent/config.py:1269-1320`)
4. **No split brain:** assert supervisor status, a Delegate child, and an external fake engine all report the same effective config path/digest for one attempt. Existing environment tests already pin the expected handoff shape. (`tests/test_workflow_pinning.py:86-128`, `src/delegate_agent/workflows/runtime.py:3717-3738`)
5. **Attempt immutability:** edit live ops after an attempt starts; later children in that attempt must retain the recorded digest. After a gate/approve boundary they must receive the new digest. (`src/delegate_agent/workflows/runtime.py:583-648`, `src/delegate_agent/workflows/commands.py:648-704`)
6. **Fail loudly:** delete or corrupt the live config before resume and assert no supervisor detaches, command stderr/JSON names the config failure, status remains resumable, and no stale-ops fallback occurs. (`src/delegate_agent/config.py:1502-1515`, `src/delegate_agent/workflows/commands.py:358-369`)
7. **Precedence:** assert `DELEGATE_WORKFLOW_WATCHDOG_TIMEOUT_SECONDS` wins over the attempt config and its source is recorded; assert an invalid env override refuses or follows one explicitly chosen fallback policy. (`src/delegate_agent/workflows/runtime.py:4231-4240`)
8. **Validation debt:** add validation tests for `workflows.watchdogTimeoutSeconds` and `workflows.dryRunTimeoutSeconds`; neither is validated by the current workflow-section validator. (`src/delegate_agent/config.py:611-645`, `src/delegate_agent/config.py:1671-1680`, `src/delegate_agent/workflows/runtime.py:4231-4240`)
9. **Watchdog-lane independence:** run the `dlg-dxd` debounce/read-error tests at two attempt-scoped thresholds and prove the delivery tests still pass when watchdog reason handling is mutated. (`src/delegate_agent/workflows/runtime.py:4299-4351`)

Mutation-check each new assertion before accepting green: remove `watchdogTimeoutSeconds` from the allowlist; accidentally allow `codex.binary`; make `_supervise` choose `pin.config`; let children inherit the creation config path; swap env/config precedence; and permit stale fallback after a malformed live file. Each mutation must make one named test above fail for the intended reason. The relevant mutation anchors are the launch selection, environment delivery, and watchdog precedence seams. (`src/delegate_agent/workflows/commands.py:71-89`, `src/delegate_agent/workflows/commands.py:343-357`, `src/delegate_agent/workflows/runtime.py:4231-4240`)
