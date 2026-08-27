# Writing-plans v2 hardening and ship: Plan B r2

```toml plan
name = "v2-hardening"
repo = "~/Code/writing-plans"

[review_tiers.light]
lanes = 1
min_families = 1

[review_tiers.standard]
lanes = 2
min_families = 2

[review_tiers.critical]
lanes = 3
min_families = 3
```

## 1. Goal lock

```markdown goal-lock
# Goal lock — writing-plans v2 hardening & ship

2026-08-27. Author: Fable coordinator (hq). Status: locked 2026-08-27 (Trey, in-session; no row overrides). Lock pre-grants G1, G2, G-compat, old-skill deletion. Engine side delegated to the delegate-agent resident per ruling 1.

Inputs: `docs/dogfood/2026-08-24-atlasos-deck-parity.md` (441-line run journal, ~40 rulings), `~/Code/hq/scratch/planning-skill-v2/dogfood-2026-08-25-papercuts-night.md` (panel-machinery journal), all 25 open `wp-2m8.*` beads, delegate/workflow papercuts (atlasos + home ledgers), `dlg-*` open beads, `docs/design/2026-08-25-per-item-settle.md`, `docs/design/2026-08-25-spine-freeze-inventory.md`.

## 1. Objective, in my words

Convert everything the deck-parity dogfood taught us into root-cause fixes, then ship writing-plans v2 as the one planning skill. Two workstreams, one arc: the **engine side** (`~/Code/delegate-agent` — gate/pause protocol, retry-preserves-work, worktree lifecycle, structured-output tolerance, observability) is **owned by the resident delegate-agent agent** under a written acceptance contract (hq → delegate-agent mail 20260827-024230-b3ed7b; coordination in #v2-hardening). **Plan B — this plan — is mine**: the emitter, lint, skill text, and personas (`~/Code/writing-plans`) — verify-row identity matching, risk-tiered review regimes that actually drive the render, coordinator_tasks as a first-class channel, close-verifier honesty, the ~12 lint rules the run proved catchable, the Phase 5 operational recipes — ending with the ship tail: canary, G2, install, old-skill deletion. Plan B's workflow executes on the engine the delegate-agent side ships. "Done" means a build like deck-parity runs without a single hand-merge, hand-written approval.json, or kill-patch-resume.

## 2. Who and what it's for

Coordinators (us, any model family) planning multi-day builds; lanes that consume briefs cold; Trey, who reads gates and receipts instead of babysitting. The measure of success is coordinator interventions per run trending to zero while review quality holds.

## 3. Acceptance demo

1. `cd ~/Code/writing-plans && python -m pytest -q` green; `tests/acceptance.sh` green.
2. `~/Code/delegate-agent` house gate green (their side; verified, not taken on faith).
3. **Canary (wp-2m8.30):** a fresh three-task plan runs `plan-lint → plan-to-beads → plan-to-workflow → delegate workflow run` to green receipts on the shipped runtime with **zero coordinator interventions** — no hand-merges, no approval.json writes, no kill-patch-resume.
4. **Chaos demos** (the delegate-agent acceptance contract A–C, run by us at ship): (a) lane killed mid-run → retry resumes from the lane's branch, committed work preserved; (b) forced red close → supervisor parks **gated**, `delegate workflow approve` works, approvals accumulate per (stage, attempt); (c) `delegate worktree prune --merged` during a live run removes nothing belonging to a non-terminal lane.
5. `bin/writing-plans-install --verify` clean on the cell; skill resolves via the library symlink and `claude-skill search`.
6. Every open `wp-2m8.*` bead and every dogfood-tagged delegate papercut is closed with a reason: fixed SHA, healed SHA (repro-on-base proved it already fixed), or deferred-with-recorded-shape. Nothing silently dropped.
7. AAR (wp-2m8.41) committed under `docs/dogfood/`; `skill/lessons.md` appended; G2 PASS receipt committed.

## 4. Rulings I'll make unless you override

| # | Ruling | Why | Cost if wrong |
|---|--------|-----|---------------|
| 1 | Engine side is delegated to the resident delegate-agent agent under the mailed acceptance contract (A–F, note 20260827-024230-b3ed7b); they triage the 9-item inventory into dlg-* beads, bounce planc-side items back over #v2-hardening. Trey 2026-08-27 | Trey's direction; they know their runtime, and they were already mid-flight on the retry/worktree family | Contract items slip without a shared graph — mitigated by the channel + acceptance demos gating ship either way |
| 2 | Plan B executes via the v2 workflow itself, coordinator-babysat under whatever engine defects remain un-shipped at launch | Dogfood to the last mile | Some hand interventions during B — each one is data |
| 3 | Routing for Plan B (Trey 2026-08-27): executors codex **luna max**; reviewer pool round-robin **cursor grok-4.6 / opus / sol xhigh**; fix lanes **terra xhigh**; adjudicator sol xhigh; verifier luna max; architects opus + sol xhigh, grok whole-plan read at r1. Aliases verified against `delegate models` before the routing block is committed | Trey's explicit spec | A weak seat wastes a round; pool order is editable per plan |
| 4 | Replay/journal compatibility is a Plan A invariant: the paused deck-parity run must resume correctly on the upgraded runtime; cell runtime promotion happens at a named checkpoint, never silently under a live run | One paused production run exists; the deploy-under-live-lanes incident is in the corpus | A compat shim we'd rather not carry — kept minimal and tested |
| 5 | Root-cause discipline: every defect closes as fix+regression-test, healed-SHA (repro-on-base proves it), or explicit defer-with-shape. Fix waves start with reproduction per Phase 4 | Half the corpus already healed mid-run; reviewing fixes for healed defects is ceremony | Repro effort on defects that are obviously live |
| 6 | In-run emergency fixes (per-item settle, red() semantics, fix-boundary union, refusal ordering…) are **audited and pinned by tests**, not rewritten — unless review finds them wrong | They shipped under fire and work; the gap is test coverage and docs | An under-designed emergency fix survives — the panel's job to catch |
| 7 | Estate-side root causes (payload deploy wiping `~/.delegate`, managed bashrc PATH) stay out of scope; in-scope countermeasures: runtime+persona pinning at workflow launch, `--verify` preflights | Different repo, different owner; papercuts filed | Deploy can still bite non-pinned runs |
| 8 | The AAR runs as a Phase 1 recon lane; its guardrail recommendations (per-task budget ceilings, wall-clock ceilings, tier defaults) become Plan B tasks if adopted at r1 | Trey's P0 ask; its output feeds the design instead of trailing it | AAR quality gated by the panel later, not at birth |
| 9 | The paused deck-parity run and its beads/receipts are untouched by this arc | Separate build, human gate pending | None |
| 10 | P0/P1 beads are mandatory scope; P2/P3 adopted or dispositioned in the plan with a reason — disposition per bead, per papercuts-night law | Anti-ceremony both directions | A deferred P3 bites later; the shape is recorded |
| 11 | Old-skill deletion (wp-2m8.31/32) ships in Plan B's tail as a pointer migration, pre-authorized by this lock | It's the "one planning skill" objective | Rollback is a git revert in the library |
| 12 | Delegate-side work not implicated by the dogfood corpus (Devin flags, discovery cache, engine retirements…) stays out | Scope fence | None — their beads remain |

## 5. Scope fence

Non-goals: new engine features beyond defect fixes and AAR-mandated guardrails; Mac-side installs (estate-sync carries them); atlasos changes of any kind; post/papercuts/herdr tooling; estate payload/deploy machinery; multi-repo task grammar (two-plan shape is the answer this arc). Discovered work is filed `discovered-from` and not done unless it blocks the acceptance demo.

## 6. Fable gates

Standing G1 (plan review on r2) and G2 (pre-ship verify, reads the receipts — including the chaos demos — and says ship). Build-specific: **G-compat** — before cell runtime promotion, Fable verifies the paused deck-parity run's resume path against the upgraded engine (journal replay on a copy, no live mutation). Trey's lock pre-grants all three.

## What I need from you to lock

One "locked" (or overrides by row number) — this pre-grants G1, G2, G-compat, and the old-skill deletion.
```

## 2. Routing

The reviewer order is load-bearing under today's persona-i-takes-slot-i staffing: reviewer persona 0 receives the cursor route, persona 1 the claude route, and persona 2 the codex route. Standard reviews therefore use two families and critical reviews use three. The positional consequence is explicit: light tasks are reviewed by cursor grok-4.6 alone, standard tasks add claude opus, and codex sol xhigh reviews only critical tasks. D1.DOCS is therefore standard, with its conventions reviewer in slot 0 and seam verifier in slot 1. `fixer` is the dedicated fix-stage role delivered by R1.REGIMES; the bootstrap engine may require coordinator attention before that task lands.

Plan B executes as one workflow run. G1 records the exact resolved run-claim ceiling printed by `bin/plan-lint --envelope`; r2 does not hand-copy a number that can drift when the graph changes.

```toml routing
[executor]
routes = [{engine="codex", model="luna", effort="max", mode="work"}]

[senior-executor]
routes = [{engine="codex", model="luna", effort="max", mode="work"}]

[fixer]
routes = [{engine="codex", model="terra", effort="xhigh", mode="work"}]

[reviewer]
routes = [{engine="cursor", model="grok-4.6", effort="xhigh", mode="work"},
          {engine="claude", model="opus", effort="xhigh", mode="work"},
          {engine="codex", model="sol", effort="xhigh", mode="work"}]

[integrator]
routes = [{engine="codex", model="luna", effort="max", mode="work"}]

[adjudicator]
routes = [{engine="codex", model="sol", effort="xhigh", mode="work"}]

[verifier]
routes = [{engine="codex", model="luna", effort="max", mode="work"}]

[architect]
routes = [{engine="claude", model="opus", effort="xhigh", mode="work"},
          {engine="codex", model="sol", effort="xhigh", mode="work"}]
```

## 3. Interfaces

These contracts are normative. Lane implementations may choose internal names only where this section does not fix one. Receipts preserve original spellings even when comparison uses a canonical form.

### Post-split module map

- `src/planc/contracts.py` owns transport schemas and semantic cross-field validators.
- `src/planc/engine/` is a real importable package. `core.py` owns state, git helpers, stage entries, the settle dispatcher, and scheduler.
- `coverage.py` owns result predicates and declared-row policy. `merge.py` owns merge and refusal guards.
- `close.py` owns close entry, close settlement, and close prompt construction. `guardrails.py` owns admission, persisted claims, and timeout threading.
- The standalone workflow embeds `contracts.py` first, followed by `core.py`, `coverage.py`, `merge.py`, `close.py`, and `guardrails.py` in fixed order.
- Ordered concatenation strips intra-package imports. Embedded bodies contain no `planc` imports; contract names come from the preceding embedded body, and no runtime validator exists twice.
- `src/planc/workflow.py` retains `render()`, `_replace_section`, and file IO only. `_resolve_pools`, `_route_data`, `_stage_data`, `_task_data`, `_owned_map`, `plan_data` assembly, `_close_prompt_parts`, `_final_close_verifies`, `_VERIFY_ADDENDUM`, `_TIMEOUTS`, `_SCHEMAS`, merge, close, and settle move into `src/planc/engine/`.
- No module-level engine statement references a harness-injected global. Values derived from `args`, including the park poll interval, resolve inside a function.
- `tests/test_workflow.py` and `tests/test_workflow_sim.py` split into per-seam modules. No two downstream engine tasks share a test file.

T0.SPLIT is behavior-preserving. The pre-split suite, normal package imports, top-level execution of one rendered script, and the two-run byte-stability simulation across every embedded module are its gate; every later code task assumes this map.

### Grammar and regime contract

```text contract
EFFECTS = {docs, code, filesystem, git, network, credentials, schema, process}

Every executable task has required effects: list[str]. Missing values fail as
missing-effects; values outside EFFECTS fail as unknown-effect.

resolve_regime(effects):
  effects == ["docs"]                                      -> prose
  effects intersects {credentials, schema, process, filesystem} -> critical
  otherwise                                                -> standard

[regimes] is the v2 spelling. [review_tiers] remains a deprecated bootstrap alias
that parses into the same regimes IR and advises deprecated-review-tiers. A plan
using the alias retains the legacy executable-task shape for this run; a plan
using [regimes] must declare effects. Alias deletion is filed discovered-from.

Each regime is:
  {lanes, min_families, fix_rounds,
   re_review: "none" | "scoped",
   adjudicate: bool,
   severity_floor,
   max_task_calls}

The fixed names are prose, standard, critical. The prose path emits
execute -> review -> optional fix -> done and emits no adjudicate stage when
adjudicate=false. review.tier is optional and overrides derivation. Escalation
is free. De-escalation requires non-empty review.tier_reason or lint fails
tier-downgrade-without-reason.

Executable tasks also gain optional consumes: list[str] and bounds: {timeout:
int}. These optional fields stay outside EXEC_FIELDS. Routing role tables gain
timeout: int. The plan header gains optional run_claim_ceiling: int and a
non-empty reason when the authored value lowers the derived ceiling. The
ExecutableTask dataclass gives effects a default factory for direct in-process
constructors, while parse refuses missing effects on the [regimes] path.
```

### Outcome contracts

```text contract
SEVERITY = blocker | major | minor
ORDER = blocker > major > minor

FINDING = {
  id, severity, file, line, claim, counterexample, fix
}

DISPOSITION = {
  id,
  verdict: ADOPT | REJECT | COVERED | COORDINATOR | DEFERRED,
  severity,
  lane,
  persona,
  file,
  line,
  because
}

FIX_TASK = {
  id, delivers, owned_files, change,
  verify: [{run, expect}],
  fixes: [finding_id]
}

COORDINATOR_TASK = {
  id,
  finding_ids: [finding_id],
  why,
  paths: [path],
  change,
  verify: [{run, expect}],
  blocking: bool
}

ADJUDICATION = {
  decision: close | fix | coordinate,
  dispositions: [DISPOSITION],
  fix_tasks: [FIX_TASK],
  accepted_residue: [finding_id],
  coordinator_tasks: [COORDINATOR_TASK]
}

ADJUDICATION has no dry field. fix_tasks is non-empty exactly when
decision=fix. decision=coordinate requires coordinator_tasks. Every FINDING id
has exactly one typed disposition; every fixes/finding_ids/accepted_residue
reference resolves to one of those ids. because is non-empty. accepted_residue
contains only ADOPT ids at or below the resolved severity_floor.

blocking=true parks the affected item through existing STATE machinery.
blocking=false records the item in STATE and close refuses until it is
acknowledged. `plan-unpark acknowledge <coordinator_task_id> --commit <sha>`
validates the id against current STATE and writes an idempotent receipt to
`STATE['coordinator']`; close consumes that exact receipt. An out-of-boundary
fix task is reclassified as a coordinator task and is never dropped.

FIX_VERDICT = {
  verdicts: [{finding_id, state: fixed | not_fixed | regressed, evidence}],
  regressions: [FINDING],
  unrun: [{finding_id, reason}]
}

Every adopted finding has one verdict or one unrun entry. evidence and reason
are non-empty. regressions contains only findings introduced within the fix
diff. Any unrun entry parks. Severity-floor close is evaluated before round
exhaustion: all remaining findings at or below the floor close with accepted
residue; any higher finding or any unrun entry takes the existing gate path.

verify_fixes uses role verifier, persona fix-verifier, and verifier pool slot 0
in the task's review worktree. Its immutable inputs are the adopted finding
records, their declared verify rows, and the fix diff. The persona emits one
FIX_VERDICT marker line and no other result shape.

CLOSE_RESULT = {
  status: complete | refused,
  verify_results: [{declared_row_id, run, expect, exit, passed, note}],
  refused_rows: [{declared_row_id, run, reason}],
  row_coverage: {matched, missing, failing, extra},
  diff_nonempty: {task_id: bool},
  bead_closed: {task_id: bool},
  main_head,
  main_branch,
  spine_head,
  acceptance_commit?
}

Close notes and refusal reasons are non-empty. status=complete requires full
declared-row identity coverage, no missing row, and exact task-key coverage in
both maps. status=refused accounts for every unrun declared row in refused_rows
and cannot claim close success.
```

### Verify identity and cache invalidation

```text contract
row_coverage(reported, declared) -> {matched, missing, failing, extra}

DECLARED_ROW_ID = (task_id, index, normalized_run, expect)

normalize_run(run): strip outer whitespace, collapse internal whitespace, and
drop one trailing semicolon. Apply nothing fuzzier.

matched, missing, and failing contain DECLARED_ROW_ID values. extra contains
reported-row identities beyond the declared multiplicity. One authoritative
execution of a normalized command fans out to every declared row it satisfies,
including rows with distinct expect values; duplicate reported rows beyond the
declared multiplicity are extra.

stage    failing declared row    missing declared row              extra row
execute  red                     note; merge re-runs the row        informational
fix      red                     note; merge re-runs the row        informational
close    red                     red and named                      receipt only

_VERIFY_ADDENDUM says extra rows are informational.

reject(key_or_label, reason) is the delegate resident's script-side tombstone
operation. It records a monotonically sequenced invalidation before any retry
or cache-adoption lookup, invalidates the named key or the key resolved from
the label, and is idempotent for the same target and reason. Repetition cannot
resurrect or duplicate a rejected result. The emitter invokes reject for every
red execute, fix, or close result and for plan-unpark retry before re-admission.
```

### Path, rubric, merge, and close contracts

```text contract
canonical_test_path(path) makes
  <dir>/X.test.ts
and
  <dir>/__tests__/X.test.ts
equivalent for boundary checks. No other path equivalence is inferred. The
original path spelling remains in prompts and receipts. Lint, adjudication,
audit, merge, and close all call this shared canonicalizer.

RUBRIC_CLASS = {
  plan document,
  rendered briefs directory,
  rendered workflow script,
  tests/acceptance.sh
}

No task owned_files or effective fix union may reach RUBRIC_CLASS. Lint fails
an authored violation; settlement reclassifies a generated violation into
coordinator_tasks.

REFUSAL_PRECEDENCE = (
  "dirty",
  "no-head",
  "no-ancestor",
  "outside",
  "fake-contribution",
  "empty",
  "host-not-on-spine",
)

merge_lane evaluates every guard before deciding. Under MERGE_LOCK it resolves
result.branch to tip, requires result.head (recorded_head) to be an ancestor of
tip, and merges tip. Its receipt reports {recorded_head, merged_head} and every
applicable refusal in precedence order. The current first-parent reason
"reported head is on the spine first-parent history, not a lane contribution"
is an idempotent ancestor no-op, not a refusal. Contribution scope is computed
against merge-base(tip, spine); zero or multiple bases produce an explicit
classification rather than an arbitrary base choice.

Close snapshots integration HEAD and immutable receipts under MERGE_LOCK,
creates a pinned detached close worktree, releases the lock for verification,
and reacquires it only for the final head/receipt check and fast-forward. A
moved head causes a fresh close run, never approval of stale evidence.

Each accepted-residue item upserts one discovered-from bead by metadata
`(plan, task, finding_id)`. Retry uses lookup-before-create, and the close
receipt maps every residue finding id to its bead id.
```

### Guardrail contract

```text contract
regime_max_task_calls = {prose: 8, standard: 12, critical: 24}

A claim is one live agent launch. Cache hits and replayed calls do not count;
structured retries do count. STATE persists stable claim identities before
admission. The envelope reports the current-run count and cumulative-across-
resumes count separately.

derived_run_claim_ceiling =
  sum(regime_max_task_calls[resolved_regime(task)] for executable tasks)
  + close_count

resolved_run_claim_ceiling = plan.run_claim_ceiling when authored, otherwise
derived_run_claim_ceiling. Lowering the derived value requires a recorded
reason. The soft warning is 80% of the resolved value. A resolved value below
the plan's zero-fix minimum claim floor is lint hard-fail
envelope-under-minimum. Admission at a breached task or run ceiling produces
a typed guardrail park; a rendered-script hand edit is never the raise path.

whole_task_active_seconds = {prose: 7200, standard: 7200, critical: 14400}
Parked time is excluded.

routing_timeout_seconds = {
  executor_standard: 1800,
  executor_critical_or_host: 3000,
  reviewer_standard: 600,
  reviewer_critical: 900,
  adjudicator: 900,
  integrator: 600
}

timeout(task, role, regime, host) resolves in this exact order:
  task.bounds.timeout
  then routing.<role>.timeout
  then routing_timeout_seconds[(role, regime/host)]
  then _TIMEOUTS fallback

Routing timeout flows through _stage_data. These are execution safety bounds,
not schedule estimates. Token and step budgets remain outside Plan B.
```

## 4. Tasks

```toml task
id = "L0.ENVELOPE"
title = "Compile the pre-G1 claim envelope"
delivers = "G1 receives a mechanical claims, retry-slots, ceilings, minimum-floor, and gate-count report for the current grammar"
kind = "build"
blocked_by = []
acceptance = "plan-lint --envelope reports the current plan, resolves a derived ceiling, and refuses a fixture whose ceiling is below its minimum with envelope-under-minimum"
role = "executor"
persona = "test-first-writer"
skills = ["tdd"]
owned_files = ["src/planc/envelope.py", "bin/plan-lint", "tests/test_envelope.py"]
invariants = ["the report runs against the pre-hardening grammar before G1", "claims are live launches rather than cache replays", "minimum claims and close count are derived from the graph", "the report separates current-run and cumulative-across-resumes counts"]
verify = [{run = "python -m pytest -q tests/test_envelope.py -k test_envelope_rejects_ceiling_below_minimum", expect = "exit 0"}, {run = "bin/plan-lint --envelope docs/plans/2026-08-27-v2-hardening-plan.md", expect = "resolved_run_claim_ceiling"}]
reversibility = "reversible"
[review]
tier = "light"
personas = ["conventions"]
```

```toml task
id = "ENGINE.C0"
title = "Freeze the resident reject contract"
delivers = "the delegate resident commits the reject signature and sequencing contract before any emitter lane consumes it"
kind = "checkpoint"
blocked_by = []
acceptance = "Fable records on #v2-hardening the resident's reject(key_or_label, reason) signature, key-or-label resolution, sequencing before cache-adoption lookup, and idempotence"
assignee = "fable"
```

```toml task
id = "ENGINE.W1"
title = "Delegate resident Wave 1 is landed"
delivers = "a blocker receipt proves the shipped engine implements the frozen gate, retry, worktree, parser, and reject contracts"
kind = "checkpoint"
blocked_by = []
acceptance = "Fable verifies the delegate resident's Wave 1 receipts and confirms the ENGINE.C0 contract is implemented as specified"
assignee = "fable"
```

```toml task
id = "G1"
title = "Fable G1 reviews plan r2"
delivers = "a pre-granted Fable verdict admits the locked r2 plan to execution"
kind = "checkpoint"
blocked_by = ["L0.ENVELOPE"]
acceptance = "Fable reads r2, lint rc 0, route-check rc 0, the derived waves, and the L0 envelope and refuses any envelope-under-minimum result before returning PASS"
assignee = "fable"
```

```toml task
id = "T0.SPLIT"
title = "Split the workflow monolith at the ruled seams"
delivers = "contracts.py and the five-module engine package are importable, ordered-embeddable, and behavior-identical while workflow.py is only the renderer"
kind = "build"
blocked_by = ["G1"]
acceptance = "the r1 baseline, normal imports, standalone top-level execution, and deterministic two-run simulation pass after the full relocation and per-seam test split"
role = "senior-executor"
persona = "minimalist-implementer"
skills = ["codebase-design", "diagnosing-bugs"]
owned_files = ["src/planc/workflow.py", "src/planc/contracts.py", "src/planc/engine", "tests/test_workflow.py", "tests/test_workflow_sim.py", "tests/test_engine_split.py", "tests/test_engine_contracts.py", "tests/test_engine_regimes.py", "tests/test_engine_regimes_sim.py", "tests/test_engine_coverage.py", "tests/test_engine_coverage_sim.py", "tests/test_engine_coordinator.py", "tests/test_engine_coordinator_sim.py", "tests/test_engine_merge.py", "tests/test_engine_merge_sim.py", "tests/test_engine_close.py", "tests/test_engine_close_sim.py", "tests/test_engine_guardrails.py", "tests/test_engine_guardrails_sim.py"]
invariants = ["core coverage merge close and guardrails emit by ordered concatenation after contracts with intra-package imports stripped", "every embedded module has one byte-stable copy and embedded bodies contain no planc import", "workflow.py retains render _replace_section and file IO only while _resolve_pools _route_data _stage_data _task_data _owned_map plan_data assembly _close_prompt_parts _final_close_verifies _VERIFY_ADDENDUM _TIMEOUTS _SCHEMAS merge close and settle move into engine", "no engine module-level statement references args or another harness-injected global", "no two downstream engine tasks share a test file", "near-512KiB output literal bracket paths and sorted-JSON permutations remain pinned"]
verify = [{run = "python -m pytest -q", expect = "passed"}, {run = "python -m pytest -q tests/test_engine_split.py", expect = "exit 0"}, {run = "PYTHONPATH=src python -c 'import planc.engine, planc.contracts'", expect = "exit 0"}, {run = "! rg -n '^(_VERIFY_ADDENDUM|_TIMEOUTS|_SCHEMAS)|^def (_resolve_pools|_route_data|_stage_data|_task_data|_owned_map|_close_prompt_parts|_final_close_verifies|merge_lane|settle)' src/planc/workflow.py", expect = "exit 0"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "C1.GRAMMAR"
title = "Compile the regime grammar and policy inputs"
delivers = "IR and parser support effects, regimes, the deprecated review-tier alias, optional tier overrides, consumes, routing timeout, task bounds, and authored claim ceilings"
kind = "build"
blocked_by = ["T0.SPLIT"]
acceptance = "targeted parser and constructor tests prove the v2 fields, legacy alias, missing-effects refusal, optional-field boundary, and self-render compatibility"
role = "executor"
persona = "test-first-writer"
skills = ["tdd"]
owned_files = ["src/planc/ir.py", "src/planc/parse.py", "skill/references/task-grammar.md", "tests/test_parse.py", "tests/test_audit.py", "tests/test_graph.py", "tests/test_routing.py", "tests/test_beads.py", "tests/fixtures/regimes.md", "tests/fixtures/bad/missing-effects.md", "tests/fixtures/bad/unknown-effect.md", "tests/fixtures/bad/deprecated-review-tiers.md"]
invariants = ["the regimes parse path requires effects from the closed vocabulary while the dataclass default preserves direct constructors", "review_tiers parses into the regimes IR with deprecated-review-tiers and preserves Plan B's legacy task shape for this run", "consumes and bounds are optional allowed fields outside EXEC_FIELDS", "run_claim_ceiling and its lowering reason are typed once in IR", "checkpoints max_review_rounds and no-match remain rejected fields or modes", "the shipped compiler renders and dry-runs this plan and the predecessor plan without hand edits"]
verify = [{run = "python -m pytest -q tests/test_parse.py", expect = "exit 0"}, {run = "python -m pytest -q tests/test_audit.py tests/test_graph.py tests/test_routing.py tests/test_beads.py", expect = "exit 0"}, {run = "rg -n 'effects|regimes|consumes|bounds|run_claim_ceiling|deprecated-review-tiers' skill/references/task-grammar.md", expect = "run_claim_ceiling"}, {run = "T=$(mktemp -d); bin/plan-lint docs/plans/2026-08-27-v2-hardening-plan.md && bin/plan-to-workflow docs/plans/2026-08-27-v2-hardening-plan.md -o \"$T/wf.py\" && delegate workflow check \"$T/wf.py\"", expect = "exit 0"}, {run = "T=$(mktemp -d); bin/plan-lint \"$HOME/Code/hq/docs/plans/2026-08-22-writing-plans-v2-plan.md\" && bin/plan-to-workflow \"$HOME/Code/hq/docs/plans/2026-08-22-writing-plans-v2-plan.md\" -o \"$T/wf.py\" && delegate workflow check \"$T/wf.py\"", expect = "exit 0"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "C2.CONTRACTS"
title = "Land v2 outcome schemas and semantic validators"
delivers = "contracts.py exposes FINDING, ADJUDICATION, FIX_VERDICT, and CLOSE_RESULT exactly as Interfaces defines them"
kind = "build"
blocked_by = ["T0.SPLIT"]
acceptance = "tests/test_contracts.py exists and rejects every contradictory decision, unknown severity, dangling finding id, empty evidence, and vacuous complete close"
role = "executor"
persona = "test-first-writer"
skills = ["tdd"]
owned_files = ["src/planc/contracts.py", "tests/test_contracts.py", "tests/test_engine_contracts.py"]
invariants = ["severity is blocker major or minor at the schema boundary", "every finding receives one typed disposition and all references resolve", "CLOSE_RESULT and row_coverage share the four-key declared-row-id shape", "canonical_test_path is the shared boundary helper", "the legacy dry key is rejected with a named error in the same change that updates the required-field pin", "complete close requires row and task-key coverage while refused close names unrun work"]
verify = [{run = "python -m pytest -q tests/test_contracts.py tests/test_engine_contracts.py", expect = "exit 0"}, {run = "python -m pytest -q tests/test_contracts.py -k test_adjudication_rejects_legacy_dry_key", expect = "exit 0"}, {run = "rg -n 'DECLARED_ROW_ID|ADJUDICATION|FIX_VERDICT|CLOSE_RESULT|accepted_residue|coordinator_tasks|canonical_test_path' src/planc/contracts.py", expect = "DECLARED_ROW_ID"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "C3.PERSONAS"
title = "Rewrite the reviewer and decision personas"
delivers = "the shared reviewer preamble and the close, fix, adjudicator, refuter, and skeptic personas carry the ruled review laws and exact report contracts"
kind = "build"
blocked_by = ["T0.SPLIT"]
acceptance = "the preamble lives outside the selectable persona glob, verifier is a valid role, both verifier personas are indexed, and every folded persona-law assertion passes"
role = "executor"
persona = "minimalist-implementer"
skills = ["writing-for-agents"]
owned_files = ["skill/references/reviewer-preamble.md", "skill/personas/test-skeptic.md", "skill/personas/adjudicator.md", "skill/personas/refuter.md", "skill/personas/close-verifier.md", "skill/personas/fix-verifier.md", "skill/personas/index.toml", "tests/test_personas.py"]
invariants = ["the shared preamble carries R1 R2 R4 and R8 once and is not selectable as a persona", "test-skeptic carries the presence-first and green-suit form of R3", "refuter preserves exact base reproduction before reading a fix", "adjudicator quotes the verbatim owned list and emits typed satisfiable work using the shared canonicalizer", "close-verifier emits CLOSE_RESULT and never INTEGRATE_RESULT", "fix-verifier emits the FIX_VERDICT marker and verifier is accepted by the persona test role set"]
verify = [{run = "python -m pytest -q tests/test_personas.py", expect = "exit 0"}, {run = "rg -n 'coordinator_tasks|accepted_residue|canonical' skill/personas/adjudicator.md", expect = "coordinator_tasks"}, {run = "rg -n 'CLOSE_RESULT' skill/personas/close-verifier.md && rg -n 'FIX_VERDICT' skill/personas/fix-verifier.md && rg -n 'base' skill/personas/refuter.md", expect = "FIX_VERDICT"}]
reversibility = "reversible"
[review]
tier = "light"
personas = ["conventions"]
```

```toml task
id = "RUBRIC.MIGRATE"
title = "Coordinator migrates rubric compatibility"
delivers = "the acceptance rubric derives persona count and both production plan documents remain executable through the shipped compatibility path"
kind = "checkpoint"
blocked_by = ["C1.GRAMMAR", "C3.PERSONAS"]
acceptance = "the coordinator changes acceptance item 6a to a derived persona count, proves items 2a and 7 parse this plan and the predecessor through the deprecated alias, and files alias deletion as discovered-from work"
assignee = "fable"
```

```toml task
id = "R1.REGIMES"
title = "Drive runtime stages from resolved regimes"
delivers = "engine/core.py compiles task policy and emits distinct prose, standard, and critical stage chains with dedicated fixer and fix-verifier routing"
kind = "build"
blocked_by = ["C1.GRAMMAR", "C2.CONTRACTS", "C3.PERSONAS"]
acceptance = "core tests show prose omits adjudication, standard and critical use the named scoped FIX_VERDICT seat, severity-floor close precedes exhaustion, coordinate becomes a typed durable record, and adversarial reviewers are isolated"
role = "executor"
persona = "test-first-writer"
skills = ["tdd", "codebase-design"]
owned_files = ["src/planc/engine/core.py", "tests/test_engine_regimes.py", "tests/test_engine_regimes_sim.py"]
invariants = ["core consumes regime data rendered by parse and validators embedded from contracts rather than importing planc in the standalone body", "skill/references/reviewer-preamble.md is embedded by path for every reviewer", "prose emits no adjudicate stage", "verify_fixes uses verifier fix-verifier pool slot 0 in the task review worktree with immutable adopted findings rows and diff", "decision coordinate creates a typed coordinator record even before acknowledgement lands", "refuter and explicitly listed attacker personas use isolated worktrees while other reviewers leave a recorded porcelain before-and-after guard", "the shipped compiler renders and dry-runs this plan and the predecessor plan without hand edits"]
verify = [{run = "python -m pytest -q tests/test_engine_regimes.py -k test_regime_prose_omits_adjudicate", expect = "exit 0"}, {run = "python -m pytest -q tests/test_engine_regimes_sim.py -k test_verify_fixes_uses_named_verifier_seat", expect = "exit 0"}, {run = "! rg -n 'fix_review' src/planc/engine/core.py", expect = "exit 0"}, {run = "! rg -n 'fix_adjudicate' src/planc/engine/core.py", expect = "exit 0"}, {run = "T=$(mktemp -d); bin/plan-lint docs/plans/2026-08-27-v2-hardening-plan.md && bin/plan-to-workflow docs/plans/2026-08-27-v2-hardening-plan.md -o \"$T/wf.py\" && delegate workflow check \"$T/wf.py\"", expect = "exit 0"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "L1.ROUTING"
title = "Make routing checks fail closed and explain staffing"
delivers = "the lint rule registry, family resolution, shared route parser, tier diagnostics, and retry-pool advice expose every routing weakness before render"
kind = "build"
blocked_by = ["C1.GRAMMAR", "A1.FIXTURES"]
acceptance = "unknown-family.md exists and routing and lint tests cover default unknown-family, plan-lint --route, persona-to-slot maps, the wp-2m8.10 fixture, and single-route advisories"
role = "executor"
persona = "test-first-writer"
skills = ["tdd"]
owned_files = ["src/planc/routing.py", "src/planc/lint.py", "src/planc/lint/__init__.py", "src/planc/lint/rules_routing.py", "bin/plan-lint", "bin/plan-to-workflow", "tests/test_routing.py", "tests/test_lint.py", "tests/test_lint_routing.py", "tests/fixtures/bad/unknown-family.md"]
invariants = ["lint.check iterates a rule registry whose routing boundary and plan rule modules are independently owned", "families omits unknown routes rather than inventing a family", "unknown-family appears without a network probe", "plan-lint and plan-to-workflow call one route-override parser from routing.py", "tier-unsatisfiable reports persona role slot route and family in staffing order", "the final hardcoded nonexistent fixture string is retargeted after A1"]
verify = [{run = "python -m pytest -q tests/test_routing.py", expect = "exit 0"}, {run = "python -m pytest -q tests/test_lint_routing.py -k test_unknown_family_fails_closed_without_network", expect = "exit 0"}, {run = "bin/plan-lint --help | rg -- '--route'", expect = "--route"}, {run = "rg -n 'parse_overrides' bin/plan-lint bin/plan-to-workflow src/planc/routing.py", expect = "parse_overrides"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "A1.FIXTURES"
title = "Repair acceptance fixture prerequisites"
delivers = "the good fixture renders a skill digest and uses a writable dry-run repository instead of /nonexistent"
kind = "build"
blocked_by = ["C1.GRAMMAR"]
acceptance = "tests/fixtures/good.md makes acceptance items 3 and 5a exercise their intended boundary without editing tests/acceptance.sh"
role = "executor"
persona = "test-first-writer"
skills = ["tdd", "diagnosing-bugs"]
owned_files = ["tests/fixtures/good.md", "tests/test_brief.py", "tests/test_parse.py", "/home/trey-agent/.cache/writing-plans/fixture-repo"]
invariants = ["fixture task T1 names a real installed skill so item 3 receives a sha256 provenance line", "item 5a creates and runs in the dedicated writable fixture repository outside this checkout", "the parse test still forbids the fixture repository from resolving to a live project checkout", "tests/acceptance.sh remains rubric-class and coordinator-owned"]
verify = [{run = "python -m pytest -q tests/test_brief.py -k test_lane_brief_emits_sha256_for_fixture_skill", expect = "exit 0"}, {run = "python -m pytest -q tests/test_parse.py -k test_fixture_repo_is_out_of_tree_and_writable", expect = "exit 0"}, {run = "bin/lane-brief --plan tests/fixtures/good.md --task T1 | rg 'sha256:'", expect = "sha256:"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "R2.COVERAGE"
title = "Unify verify-row coverage and rejection tombstones"
delivers = "engine/coverage.py uses declared-row identity for execute, fix, and close and rejects every red cache result before retry"
kind = "build"
blocked_by = ["R1.REGIMES", "ENGINE.C0"]
acceptance = "coverage tests prove the four-key policy, duplicate-command fan-out, relaxed extra-row reporting, sequenced reject calls, and a named launch preflight before any failure path can raise NameError"
role = "executor"
persona = "test-first-writer"
skills = ["tdd", "diagnosing-bugs"]
owned_files = ["src/planc/engine/coverage.py", "bin/plan-unpark", "tests/test_engine_coverage.py", "tests/test_engine_coverage_sim.py"]
invariants = ["one row_coverage implementation owns normalization DECLARED_ROW_ID multiplicity and stage policy", "one authoritative duplicate command execution fans out by declared expectation and surplus reports are extra", "dead fix and count helpers and obsolete expects payloads are absent", "red uses its declared verifies input", "reject is sequenced before readmission and remains idempotent", "the rendered script asserts every required engine primitive at launch and exits with engine-contract-missing rather than a failure-path NameError"]
verify = [{run = "python -m pytest -q tests/test_engine_coverage.py -k test_row_coverage_duplicate_command_fans_out_by_declared_id", expect = "exit 0"}, {run = "python -m pytest -q tests/test_engine_coverage_sim.py -k test_engine_preflight_names_missing_reject_before_failure_path", expect = "exit 0"}, {run = "! rg -n 'def fix_red' src/planc/engine/coverage.py", expect = "exit 0"}, {run = "! rg -n 'def red_count' src/planc/engine/coverage.py", expect = "exit 0"}, {run = "! rg -n '\"expects\": _verify_count' src/planc/engine/coverage.py", expect = "exit 0"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "R3.COORDINATOR"
title = "Make coordinator work a typed durable channel"
delivers = "engine/core settlement, the acknowledgement API, and boundary lint enforce blocking and nonblocking coordinator_tasks without dropping or leaking rubric work"
kind = "build"
blocked_by = ["R2.COVERAGE", "L1.ROUTING"]
acceptance = "simulation and boundary-rule tests prove STATE parking, idempotent acknowledgement receipts, reclassification-never-drop, canonical boundary union, and all four rubric-class paths"
role = "senior-executor"
persona = "test-first-writer"
skills = ["tdd", "codebase-design"]
owned_files = ["src/planc/engine/core.py", "src/planc/lint/rules_boundary.py", "bin/plan-unpark", "tests/test_engine_coordinator.py", "tests/test_engine_coordinator_sim.py", "tests/test_lint_boundary.py"]
invariants = ["blocking coordinator work parks only the affected item", "nonblocking coordinator work remains in STATE until plan-unpark acknowledge records its coordinator-task id and commit", "acknowledge validates current STATE and is idempotent for the same evidence", "out-of-boundary fix work converts to coordinator work with every finding id preserved", "additional fix_owned canonical paths survive adjudication merge and close", "plan documents briefs scripts and tests/acceptance.sh are unreachable from authored or generated lane boundaries"]
verify = [{run = "python -m pytest -q tests/test_engine_coordinator.py -k test_acknowledge_writes_idempotent_coordinator_receipt", expect = "exit 0"}, {run = "python -m pytest -q tests/test_engine_coordinator_sim.py -k test_additional_fix_owned_paths_survive_merge_and_close", expect = "exit 0"}, {run = "python -m pytest -q tests/test_lint_boundary.py -k test_rubric_class_reclassifies_generated_fix", expect = "exit 0"}, {run = "bin/plan-unpark --help | rg 'acknowledge'", expect = "acknowledge"}]
reversibility = "reversible"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

```toml task
id = "R4.MERGE"
title = "Classify all merge guards before mutation"
delivers = "engine/merge.py merges the resolved branch tip under ancestry guard and reports every refusal in written precedence order"
kind = "build"
blocked_by = ["C2.CONTRACTS"]
acceptance = "pairwise guard tests, branch-tip tests, multi-base tests, canonical test-path scope, and the current ancestor-no-op reason all pass"
role = "senior-executor"
persona = "test-first-writer"
skills = ["tdd", "diagnosing-bugs"]
owned_files = ["src/planc/engine/merge.py", "tests/test_engine_merge.py", "tests/test_engine_merge_sim.py"]
invariants = ["result.branch is resolved under MERGE_LOCK and recorded_head must be its ancestor", "the receipt reports recorded_head and merged_head", "all guards are evaluated and REFUSAL_PRECEDENCE decides once", "contribution scope uses merge-base tip versus spine and handles every returned base explicitly", "the first-parent ancestor case is an idempotent no-op"]
verify = [{run = "python -m pytest -q tests/test_engine_merge.py -k test_merge_refusal_precedence_is_pairwise_complete", expect = "exit 0"}, {run = "python -m pytest -q tests/test_engine_merge_sim.py -k test_merge_resolves_branch_tip_and_all_merge_bases", expect = "exit 0"}, {run = "rg -n 'REFUSAL_PRECEDENCE|recorded_head|merged_head' src/planc/engine/merge.py", expect = "REFUSAL_PRECEDENCE"}]
reversibility = "reversible"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

```toml task
id = "A2.AUDIT"
title = "Harden audit run roots and manifest ownership"
delivers = "delegate-audit includes child run roots, pins the two-field host manifest, and uses the shared boundary canonicalizer"
kind = "build"
blocked_by = ["C2.CONTRACTS"]
acceptance = "audit tests close wp-2m8.17 and wp-2m8.20 with child-root coverage, canonical ownership, and a sha256sum-compatible two-field manifest receipt"
role = "executor"
persona = "test-first-writer"
skills = ["tdd", "diagnosing-bugs"]
owned_files = ["src/planc/audit.py", "bin/delegate-audit", "tests/test_audit.py"]
invariants = ["audit._owns calls contracts.canonical_test_path", "child Delegate run roots are included rather than inferred from parent-only records", "the host manifest is exactly the sha256sum two-field layout", "audit receipts distinguish absent evidence from a clean result"]
verify = [{run = "python -m pytest -q tests/test_audit.py -k test_audit_includes_child_delegate_run_roots", expect = "exit 0"}, {run = "python -m pytest -q tests/test_audit.py -k test_audit_manifest_uses_sha256sum_two_field_layout", expect = "exit 0"}, {run = "rg -n 'canonical_test_path' src/planc/audit.py", expect = "canonical_test_path"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "L2.LINT"
title = "Build the two lint tiers and admission reports"
delivers = "plan-lint enforces Tier 1, advises Tier 2, checks quoted waves, renders every brief, and extends the claim envelope for resolved regimes"
kind = "build"
blocked_by = ["R3.COORDINATOR", "L1.ROUTING", "A1.FIXTURES"]
acceptance = "targeted plan-rule, brief, graph, and CLI tests pass for every ruled hard fail, advisory, --waves, --check-routes, regime-envelope, and lint-before-render path"
role = "executor"
persona = "test-first-writer"
skills = ["tdd"]
owned_files = ["src/planc/lint/rules_plan.py", "src/planc/brief.py", "src/planc/graph.py", "bin/plan-lint", "bin/plan-to-workflow", "tests/test_lint_plan.py", "tests/test_brief.py", "tests/test_graph.py"]
invariants = ["quoted or malformed ownership and uncreated missing paths fail", "later-wave acceptance or verify dependencies advise and consumes-aware edges suppress false weak-edge findings", "edges into a declared contracts wave suppress weak-edge even when consumes is omitted", "weak verify bare status orphan prose and single-route findings advise", "--waves and --check-routes mutate no rubric file", "plan-to-workflow calls lint.check before render", "the existing envelope is extended for regime claims retry slots active ceilings and gate count", "the shipped compiler renders and dry-runs this plan and the predecessor plan without hand edits"]
verify = [{run = "python -m pytest -q tests/test_lint_plan.py -k test_declared_contracts_wave_suppresses_weak_edge", expect = "exit 0"}, {run = "python -m pytest -q tests/test_brief.py tests/test_graph.py", expect = "exit 0"}, {run = "bin/plan-lint --help | rg -- '--waves|--envelope|--route|--check-routes'", expect = "--envelope"}, {run = "rg -n 'lint.check' bin/plan-to-workflow", expect = "lint.check"}, {run = "T=$(mktemp -d); bin/plan-lint docs/plans/2026-08-27-v2-hardening-plan.md && bin/plan-to-workflow docs/plans/2026-08-27-v2-hardening-plan.md -o \"$T/wf.py\" && delegate workflow check \"$T/wf.py\"", expect = "exit 0"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["seam-verifier", "test-skeptic"]
```

```toml task
id = "R5.CLOSE"
title = "Make close evidence explicit and lock scope narrow"
delivers = "engine/close.py verifies from a pinned detached close worktree and validates CLOSE_RESULT coverage before any close or fast-forward"
kind = "build"
blocked_by = ["R4.MERGE"]
acceptance = "close tests prove snapshot and final checks are the only MERGE_LOCK regions, moved heads re-run, residue beads are idempotent, baselines persist through retries, and vacuous reports refuse"
role = "senior-executor"
persona = "test-first-writer"
skills = ["tdd", "diagnosing-bugs"]
owned_files = ["src/planc/engine/close.py", "src/planc/beads.py", "tests/test_engine_close.py", "tests/test_engine_close_sim.py", "tests/test_beads.py"]
invariants = ["close-verifier runs outside MERGE_LOCK in a pinned detached worktree", "complete CLOSE_RESULT consumes the unified four-key row coverage and covers every declared row and real task key", "accepted residue upserts by plan task and finding id and the close receipt maps each finding to its bead", "the original task baseline persists through retries and fix rounds and is never re-observed at integrate start", "integrator remains an INTEGRATE_RESULT-only persona"]
verify = [{run = "python -m pytest -q tests/test_engine_close.py -k test_close_result_requires_four_key_declared_row_coverage", expect = "exit 0"}, {run = "python -m pytest -q tests/test_engine_close_sim.py -k test_task_baseline_persists_through_retries_and_fix_rounds", expect = "exit 0"}, {run = "python -m pytest -q tests/test_beads.py -k test_residue_bead_upserts_by_finding_metadata", expect = "exit 0"}, {run = "rg -n 'close-verifier|CLOSE_RESULT|finding_id' src/planc/engine/close.py src/planc/beads.py", expect = "close-verifier"}]
reversibility = "reversible"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

```toml task
id = "R6.GUARDRAILS"
title = "Enforce task, run, and active-clock guardrails"
delivers = "engine/guardrails.py persists live-launch claims, admits calls only within resolved ceilings, and resolves every stage timeout by one precedence formula"
kind = "build"
blocked_by = ["R1.REGIMES"]
acceptance = "guardrail tests pin 8 12 24 task calls, derived and authored run ceilings, the 80-percent warning, minimum-floor refusal, 7200 and 14400 active clocks, and every timeout precedence branch"
role = "senior-executor"
persona = "test-first-writer"
skills = ["tdd", "diagnosing-bugs"]
owned_files = ["src/planc/engine/guardrails.py", "tests/test_engine_guardrails.py", "tests/test_engine_guardrails_sim.py"]
invariants = ["R6 builds the persisted claim ledger because pre-hardening STATE has none", "a claim is one live launch structured retries count and cache hits or replayed calls do not", "current-run and cumulative-across-resumes claim totals stay separate", "the resolved run ceiling is parsed or derived rather than a constant and rendered-script hand edits never raise it", "parked time is excluded from whole-task active clocks", "timeout precedence is task bounds then routing role scalar then regime-host default then _TIMEOUTS", "senior executor and reviewer timeout values are directly pinned", "token and step budgets are not invented"]
verify = [{run = "python -m pytest -q tests/test_engine_guardrails.py -k test_guardrail_derives_ceiling_and_warns_at_eighty_percent", expect = "exit 0"}, {run = "python -m pytest -q tests/test_engine_guardrails_sim.py -k test_claim_ledger_counts_structured_retry_not_cache_or_replay", expect = "exit 0"}, {run = "python -m pytest -q tests/test_engine_guardrails.py -k test_timeout_precedence_task_role_regime_fallback", expect = "exit 0"}, {run = "rg -n 'max_task_calls|run_claim_ceiling|active_seconds|_stage_data' src/planc/engine/guardrails.py", expect = "run_claim_ceiling"}]
reversibility = "reversible"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

```toml task
id = "D1.DOCS"
title = "Rewrite the skill around enforced contracts and run operations"
delivers = "SKILL.md and its references describe the per-item engine, current grammar, regime behavior, and all eleven Phase 5 recipes without overstating enforcement"
kind = "build"
blocked_by = ["R6.GUARDRAILS", "L2.LINT"]
acceptance = "skill and reference tests pass, pytest is the documented project runner, plan-seed-from-run handles live task ids and help, and the docs name every enforced-versus-procedure boundary grounded in the engine package and lint registry"
role = "executor"
persona = "minimalist-implementer"
skills = ["writing-for-agents"]
owned_files = ["skill/SKILL.md", "skill/references/goal-lock.md", "skill/references/review-regimes.md", "skill/references/workflow-model.md", "skill/references/routing.toml", "bin/plan-seed-from-run", "tests/test_references.py", "tests/test_skill_md.py", "pyproject.toml", "CLAUDE.md"]
invariants = ["workflow-model describes Kahn-ordered per-item settlement and task-scoped parks", "SKILL distinguishes compiler enforcement from coordinator procedure", "Phase 5 carries all eleven recovered operations recipes", "routing reference contains the ruled timeout defaults and labels their evidence scope", "goal-lock and review-regimes match the shipped parser and engine", "skill tests pin writing-plans-install --verify before each panel", "pytest is a declared development dependency and the canonical runner in CLAUDE.md", "plan-seed-from-run uses argparse and reads the compiled script's live task-id list rather than a deck-parity-only regex"]
verify = [{run = "python -m pytest -q tests/test_references.py tests/test_skill_md.py", expect = "exit 0"}, {run = "bin/plan-seed-from-run --help", expect = "usage:"}, {run = "python -m pytest -q tests/test_skill_md.py -k test_skill_requires_install_verify_before_panel", expect = "exit 0"}, {run = "rg -n 'Phase 5|run operations|per-item|enforced' skill/SKILL.md skill/references/workflow-model.md", expect = "Phase 5"}, {run = "rg -n 'pytest' pyproject.toml CLAUDE.md", expect = "pytest"}]
reversibility = "reversible"
[review]
tier = "standard"
personas = ["conventions", "seam-verifier"]
```

```toml task
id = "P1.CANARY"
title = "Run the zero-intervention three-task canary"
delivers = "a coordinator runs plan-lint through delegate workflow run on a three-task v2 plan and receives green receipts with zero hand merges, approval-file writes, or kill-patch-resume"
kind = "build"
blocked_by = ["ENGINE.W1", "D1.DOCS", "R5.CLOSE", "RUBRIC.MIGRATE", "A2.AUDIT", "A1.FIXTURES"]
acceptance = "tests/canary/test_canary.py proves four beads close, all three task commits reach integration, every task has an adjudication verdict, and docs/canary/run.md records zero interventions"
role = "senior-executor"
persona = "test-first-writer"
skills = ["delegate-agent", "tdd", "diagnosing-bugs"]
owned_files = ["tests/canary", "docs/canary"]
invariants = ["the disposable fixture uses effects and regimes with one docs-only prose task one critical task and one checkpoint", "the chain uses unmodified outputs from plan-lint plan-to-beads and plan-to-workflow", "no hand merge approval-file write kill patch or resume occurs", "receipts bind workflow id bead reasons commits verdicts and CLI acceptance output"]
verify = [{run = "python -m pytest -q tests/canary/test_canary.py", expect = "exit 0"}, {run = "rg -n 'workflow_id|zero_interventions|adjudication|close_reason' docs/canary/run.md", expect = "zero_interventions"}]
reversibility = "reversible"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

```toml task
id = "P2.CHAOS"
title = "Run chaos contracts A through C"
delivers = "killed-lane retry preserves committed work, forced red close parks gated with cumulative approvals, and live prune leaves every non-terminal worktree"
kind = "build"
blocked_by = ["P1.CANARY"]
acceptance = "the three chaos cases pass independently and a throwaway worktree at the exact ENGINE.W1 SHA records a green delegate-agent house-gate receipt"
role = "senior-executor"
persona = "test-first-writer"
skills = ["delegate-agent", "tdd", "diagnosing-bugs"]
owned_files = ["tests/canary/test_chaos.py", "docs/acceptance/chaos"]
invariants = ["killed work resumes from the lane branch with committed work preserved", "forced red close parks gated and approve accumulates by stage and attempt", "live prune removes no non-terminal worktree", "the lane creates a detached throwaway worktree from the ENGINE.W1 SHA under $HOME/Code/delegate-agent and runs the house gate there", "the receipt records house_gate_sha and house_gate_exit=0 so close never rechecks the live resident tree", "baseline canary evidence cannot satisfy a missing chaos receipt"]
verify = [{run = "python -m pytest -q tests/canary/test_chaos.py", expect = "exit 0"}, {run = "rg -n 'house_gate_sha|house_gate_exit=0' docs/acceptance/chaos", expect = "house_gate_exit=0"}, {run = "rg -n 'killed_lane|forced_red_close|live_prune' docs/acceptance/chaos", expect = "forced_red_close"}]
reversibility = "costly"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

P3 uses this frozen source inventory. Journal references are evidence links, not alternate compatibility subjects.

| source | pinned value |
|---|---|
| repository | `~/Code/atlasos` |
| integration worktree | `~/Code/atlasos/.worktrees/plan-deck-parity` |
| compatibility workflow | `wf_b43032f7fee5` |
| journaled predecessors | `wf_5007c7a7cf1c`, `wf_bc1b6ab0d7eb`, `wf_12cda9084d53`, `wf_6934a18ead59`, `wf_24b658e34f4c`, `wf_6887f0b06050` |
| state | `.delegate/plan-state.json`, its `plan-state.wf_*.json` predecessor snapshots, and `.delegate/workflows/<workflow-id>/{args.json,journal.jsonl,script.py,status.json,result.json,approval.json}` when present |
| receipts | `docs/plans/deck-parity/receipts/` |
| run roots | `.delegate/runs/<del_* id>` for every child id referenced by the pinned workflow journals |
| clone policy | `git clone --no-local` into a fresh temporary directory, then copy only the pinned state, workflow, receipt, and referenced run-root trees; never bind or symlink the source |
| source manifest | `SRC="$HOME/Code/atlasos/.worktrees/plan-deck-parity"; (cd "$SRC" && { git for-each-ref --format='%(refname) %(objectname)'; find .delegate docs/plans/deck-parity/receipts -type f ! -name workflow.lock ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum; })` before and after replay |
| manifest exclusions | transient `workflow.lock`, Python bytecode caches, the temporary clone, and the manifest output itself |

```toml task
id = "P3.COMPAT"
title = "Prove paused-run compatibility on a full clone"
delivers = "G-compat can promote the cell runtime because a full clone of the pinned deck-parity run resumes while the live paused source stays byte-identical"
kind = "build"
blocked_by = ["ENGINE.W1"]
acceptance = "a no-local clone containing repository refs and objects plus the pinned workflow state receipts and referenced run roots checks and resumes only the clone, preserving the source manifest byte-for-byte"
role = "senior-executor"
persona = "test-first-writer"
skills = ["delegate-agent", "resume-handoff", "tdd"]
owned_files = ["tests/canary/test_compat.py", "docs/acceptance/compat"]
invariants = ["wf_b43032f7fee5 is the compatibility subject and predecessor ids are journal references only", "the paused live run and its receipts are read-only inputs", "the clone carries every pinned workflow state receipt and journal-referenced run-root path", "compatibility is tested against the upgraded engine before cell promotion", "the exact manifest command and exclusions produce identical source manifests before and after replay"]
verify = [{run = "python -m pytest -q tests/canary/test_compat.py", expect = "exit 0"}, {run = "rg -n 'source_manifest|clone_manifest|no_live_mutation|resume' docs/acceptance/compat", expect = "no_live_mutation"}]
reversibility = "costly"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

```toml task
id = "G-COMPAT"
title = "Fable G-compat approves runtime promotion"
delivers = "a pre-granted Fable verdict confirms full-clone replay compatibility before cell runtime promotion"
kind = "checkpoint"
blocked_by = ["P3.COMPAT"]
acceptance = "Fable reads docs/acceptance/compat and returns PASS only when the clone resumed and the paused live run remained byte-identical"
assignee = "fable"
```

```toml task
id = "S2.LEDGER"
title = "Disposition every legacy bead and dogfood papercut"
delivers = "every scoped wp-2m8.* bead and dogfood-tagged delegate papercut is closed with a fixed SHA, a re-verified healed SHA, a delegated dlg-44w id, or a deferred-with-recorded-shape reason"
kind = "build"
blocked_by = ["P2.CHAOS", "G-COMPAT", "A2.AUDIT"]
acceptance = "the disposition matrix covers every wp-2m8 item, every dogfood-tagged delegate papercut, and docs/acceptance/chaos evidence, including rulings 17 and 26, with no silent drop"
role = "senior-executor"
persona = "minimalist-implementer"
skills = ["workflow-closeout", "diagnosing-bugs"]
owned_files = [".beads", "docs/acceptance/dispositions.md"]
invariants = ["wp-2m8.19 and wp-2m8.15 close healed only after re-verification", "wp-2m8.17 and wp-2m8.20 close from A2 receipts and are not deferrable audit gaps", "wp-2m8.36 is labeled as the per-item serial-chain fix and close-row separation remains wp-2m8.8.2", "delegate-side items are dispositioned to dlg-44w identifiers", "wp-2m8.9 closes not-reproducible only with the retained probe logs", "wp-2m8.31 and wp-2m8.32 remain assigned to the pointer-migration task until it lands"]
verify = [{run = "python -c 'from pathlib import Path; p=Path(\"docs/acceptance/dispositions.md\"); assert p.is_file() and all(x in p.read_text() for x in (\"wp-2m8.19\",\"wp-2m8.15\",\"wp-2m8.36\",\"wp-2m8.9\",\"dlg-44w\"))'", expect = "exit 0"}, {run = "bd search 'wp-2m8' --json | jq -e 'all(.[]; (.status == \"closed\") or (.id == \"wp-2m8.31\") or (.id == \"wp-2m8.32\") or (.id == \"wp-2m8\"))'", expect = "exit 0"}]
reversibility = "costly"
[review]
tier = "light"
personas = ["conventions"]
```

```toml task
id = "S3.INSTALL"
title = "Install and verify the shipped skill on the host"
delivers = "the live skill, lessons, personas, workflow helper, shared copy, and CLI mirrors match the repository byte-for-byte"
kind = "build"
blocked_by = ["P2.CHAOS", "G-COMPAT", "A1.FIXTURES"]
acceptance = "the twelve source-linked lessons land, installer tests and exact destination assertions pass, verify mode is clean, claude-skill resolves writing-plans, and the install receipt records every digest"
role = "senior-executor"
persona = "test-first-writer"
skills = ["diagnosing-bugs", "workflow-closeout"]
owned_files = ["skill/lessons.md", "tests/test_references.py", "tests/test_install.py", "/home/trey-agent/.agents/skill-library/writing-plans", "/home/trey-agent/.claude-shared/skills/writing-plans", "/home/trey-agent/.delegate/personas", "/home/trey-agent/.delegate/workflows/checkpoint.py", "/home/trey-agent/.local/bin", "docs/acceptance/install.md"]
invariants = ["active runs and host destinations are inspected before mutation", "skill/lessons.md carries all twelve source-linked scout candidates", "tests/test_install.py pins every installer destination including the shared copy and checkpoint workflow", "the install publishes every ruled persona", "credential presence is never printed", "the receipt records source and installed digests"]
verify = [{run = "python -m pytest -q tests/test_references.py -k test_lessons_include_twelve_source_linked_candidates", expect = "exit 0"}, {run = "python -m pytest -q tests/test_install.py", expect = "exit 0"}, {run = "bin/writing-plans-install --repo \"$PWD\" --verify", expect = "exit 0"}, {run = "claude-skill search -n writing-plans | rg -x 'writing-plans'", expect = "writing-plans"}, {run = "test -s docs/acceptance/install.md", expect = "exit 0"}]
reversibility = "costly"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

```toml task
id = "S4.MIGRATE"
title = "Delete old planning skills as a pointer migration"
delivers = "every live pointer resolves to writing-plans, all four old skill names are absent from live roots, and rollback is the committed archive plus repository revert"
kind = "build"
blocked_by = ["S3.INSTALL", "S2.LEDGER"]
acceptance = "the archive contains exactly the seven named roots planning-workflow, plan-review-loop, plan-to-beads, spec-quality-checklist, claude-shared, codex, and pointer-manifest.tsv; every pointer row has before and after text; old names are absent; writing-plans resolves; and wp-2m8.31 plus wp-2m8.32 close with receipts"
role = "senior-executor"
persona = "minimalist-implementer"
skills = ["writing-for-agents", "workflow-closeout"]
owned_files = ["/home/trey-agent/.agents/skill-library/planning-workflow", "/home/trey-agent/.agents/skill-library/plan-review-loop", "/home/trey-agent/.agents/skill-library/plan-to-beads", "/home/trey-agent/.agents/skill-library/spec-quality-checklist", "/home/trey-agent/.claude-shared/skills/plan-review-loop", "/home/trey-agent/.claude-shared/skills/plan-to-beads", "/home/trey-agent/.claude-shared/skills.globals", "/home/trey-agent/.claude-shared/CLAUDE.md", "/home/trey-agent/.claude-shared/agents/plan-reviewer.md", "/home/trey-agent/.agents/skill-library/brainstorming/SKILL.md", "/home/trey-agent/.agents/skill-library/simplify-and-refactor-code-isomorphically/scripts/check_skills.sh", "/home/trey-agent/.agents/skill-library/simplify-and-refactor-code-isomorphically/references/CROSS-SKILL.md", "/home/trey-agent/.agents/skill-library/documentation-website-for-software-project/scripts/check-skills.sh", "/home/trey-agent/.codex/skills/plan-to-beads", "/home/trey-agent/.codex/AGENTS.md", "/home/trey-agent/.codex/agents/plan_reviewer.toml", "docs/migration"]
invariants = ["the new writing-plans install and docs/acceptance/dispositions.md are verified before deletion", "the committed archive exposes exactly the seven acceptance-named roots before any host removal", "every pointer changes before its target disappears", "the migration is idempotent and its rollback path is the archive plus repository revert"]
verify = [{run = "for s in planning-workflow plan-review-loop plan-to-beads spec-quality-checklist; do claude-skill search -n \"$s\" | rg -x \"$s\" && exit 1; done; exit 0", expect = "exit 0"}, {run = "! rg -l 'planning-workflow|plan-review-loop|plan-to-beads|spec-quality-checklist' \"$HOME/.claude-shared/CLAUDE.md\" \"$HOME/.claude-shared/skills.globals\" \"$HOME/.claude-shared/agents\" \"$HOME/.claude-shared/skills\" \"$HOME/.agents/skill-library\" \"$HOME/.codex/AGENTS.md\" \"$HOME/.codex/agents\" \"$HOME/.codex/skills\" --glob '!**/memory/**' --glob '!**/docs/migration/**'", expect = "exit 0"}, {run = "python -c 'import tarfile; names={x.name.split(\"/\",1)[0] for x in tarfile.open(\"docs/migration/old-skills.tar.gz\").getmembers()}; expected={\"planning-workflow\",\"plan-review-loop\",\"plan-to-beads\",\"spec-quality-checklist\",\"claude-shared\",\"codex\",\"pointer-manifest.tsv\"}; assert names == expected, (names, expected)'", expect = "exit 0"}, {run = "rg -n 'wp-2m8.31|wp-2m8.32' docs/acceptance/dispositions.md && claude-skill search -n writing-plans | rg -x 'writing-plans'", expect = "writing-plans"}]
reversibility = "irreversible"
[review]
tier = "critical"
personas = ["refuter", "test-skeptic", "3am-ops"]
```

```toml task
id = "G2"
title = "Fable G2 issues the ship verdict"
delivers = "a committed pre-granted G2 PASS receipt declares writing-plans v2 shipped or names the blocking evidence"
kind = "checkpoint"
blocked_by = ["S4.MIGRATE"]
acceptance = "Fable runs tests/acceptance.sh; reads audit, canary, chaos, compatibility, install, disposition, migration, AAR, and lesson receipts; names any AAR claim contradicted by shipped evidence; keeps lost evidence labeled lost; and commits a PASS receipt before declaring ship"
assignee = "fable"
```

## 5. Scope fence

Plan B changes the writing-plans compiler, emitted runtime, linter and CLIs, personas, skill references, regression fixtures, canary and compatibility proof, acceptance receipts, lessons, ledgers, and the pre-authorized pointer migration. The delegate-agent repository is an external contract and proof target, not a Plan B edit target. The paused deck-parity run is a read-only compatibility input.

Plan B is an explicit bootstrap exception: it stays on the old grammar for its whole run; P1.CANARY is the regime dogfood and its fixture must use `effects`, `[regimes]`, one prose task, and one critical task.

Non-goals remain exactly those in the lock: new engine features beyond defect fixes and adopted guardrails; Mac-side installs; atlasos changes; post, papercuts, herdr, or estate deploy tooling; and multi-repo task grammar. `checkpoints`, one-field `max_review_rounds`, `expect = "no-match"`, deletion-ripple imports, persisted checkpoint-wave drift, formatter lint, and `plan-explain --html` stay cut or deferred in the ruled shape. Token and step budgets stay delegate-side. Discovered work gets a `discovered-from` record and enters this graph only when it blocks the acceptance demo.

Rubric-class artifacts are coordinator-owned, not unowned; a plan that changes them declares the change as a coordinator checkpoint. This plan document, generated briefs, the rendered workflow script, and `tests/acceptance.sh` are the rubric class.

## 6. Wave map

| wave | tasks |
|---|---|
| 1 | L0.ENVELOPE · ENGINE.C0 · ENGINE.W1 |
| 2 | G1 · P3.COMPAT |
| 3 | T0.SPLIT · G-COMPAT |
| 4 | C1.GRAMMAR · C2.CONTRACTS · C3.PERSONAS |
| 5 | RUBRIC.MIGRATE · R1.REGIMES · A1.FIXTURES · R4.MERGE · A2.AUDIT |
| 6 | L1.ROUTING · R2.COVERAGE · R5.CLOSE · R6.GUARDRAILS |
| 7 | R3.COORDINATOR |
| 8 | L2.LINT |
| 9 | D1.DOCS |
| 10 | P1.CANARY |
| 11 | P2.CHAOS |
| 12 | S2.LEDGER · S3.INSTALL |
| 13 | S4.MIGRATE |
| 14 | G2 |

## 7. Acceptance demo

1. `cd ~/Code/writing-plans && python -m pytest -q` is green; `tests/acceptance.sh` is green.
2. The `~/Code/delegate-agent` house gate is green in a throwaway worktree at the ENGINE.W1 SHA, and the immutable receipt records that SHA and exit 0.
3. A fresh three-task canary using `effects`, `[regimes]`, one prose task, and one critical task runs `plan-lint → plan-to-beads → plan-to-workflow → delegate workflow run` to green receipts on the shipped runtime with zero coordinator interventions: no hand merges, no `approval.json` writes, and no kill-patch-resume.
4. Chaos A proves a killed lane resumes from its branch with committed work preserved. Chaos B proves a forced red close parks gated, `delegate workflow approve` works, and approvals accumulate per `(stage, attempt)`. Chaos C proves `delegate worktree prune --merged` removes nothing belonging to a non-terminal lane.
5. `bin/writing-plans-install --verify` is clean on the cell; the skill resolves through the library symlink and `claude-skill search`.
6. Every open `wp-2m8.*` bead and every dogfood-tagged delegate papercut closes with a fixed SHA, a re-verified healed SHA, or a deferred-with-recorded-shape reason. Nothing is silently dropped.
7. The AAR is committed under `docs/dogfood/`, `skill/lessons.md` contains the twelve source-linked lessons, contradictions against shipped receipts are named, lost evidence remains labeled lost, and the G2 PASS receipt is committed.

## 8. Invariant → verify table

| task | invariant boundary | verified by |
|---|---|---|
| L0.ENVELOPE | G1 has a mechanical ceiling and minimum-floor report | envelope unit test and plan-lint output |
| ENGINE.C0 | reject shape and sequencing are frozen before emitter use | Fable channel receipt |
| ENGINE.W1 | the frozen resident contract is shipped before proof runs | Fable implementation receipt |
| G1 | r2 admission reads lint, routes, waves, and L0 envelope | Fable checkpoint receipt |
| T0.SPLIT | five engine seams and their tests preserve emitted bytes | full baseline, imports, absence grep, and determinism simulation |
| C1.GRAMMAR | v2 grammar, legacy alias, constructors, and real plans parse | parser, constructor, and self-render rows |
| C2.CONTRACTS | schemas, declared-row identity, and semantic contradictions refuse | contract-side tests |
| C3.PERSONAS | reviewer laws and both verifier markers are selectable correctly | persona tests and marker assertions |
| RUBRIC.MIGRATE | acceptance and production plan corpus survive the grammar change | coordinator checkpoint receipt |
| R1.REGIMES | resolved policy changes the emitted stage chain and fix seat | regime, isolation, convergence, and self-render tests |
| L1.ROUTING | rule registry, route families, and staffing fail closed | routing and routing-rule tests |
| A1.FIXTURES | acceptance fixture supplies provenance and an out-of-tree writable repo | brief, parse-pin, and CLI rows |
| R2.COVERAGE | declared-row identity, primitive preflight, and tombstones govern retry | coverage and cache simulations plus absence greps |
| R3.COORDINATOR | coordinator work parks or acknowledges and rubric work reclassifies | settlement, acknowledgement, union, and boundary-rule tests |
| R4.MERGE | branch tip, ancestry, all guards, precedence, and bases are explicit | merge and pairwise collision tests |
| A2.AUDIT | child roots, canonical ownership, and manifests are complete | focused audit tests |
| L2.LINT | both lint tiers and regime admission reports match rulings | plan-rule, graph, brief, CLI, and self-render tests |
| R5.CLOSE | detached close evidence, baseline, and residue beads are durable | close, baseline, and bead-idempotency tests |
| R6.GUARDRAILS | live claims, active clocks, derived ceiling, and timeout precedence gate admission | guardrail and active-time simulations |
| D1.DOCS | published procedure, runner, and seed CLI match enforced behavior | skill/reference tests and CLI help smoke |
| P1.CANARY | v2 grammar and the full compiler-to-runtime path need no intervention | canary test and run receipt |
| P2.CHAOS | contracts A through C pass and the resident gate is SHA-pinned | chaos test and immutable house-gate receipt |
| P3.COMPAT | the pinned full-clone replay mutates no live paused state | compatibility test and source manifests |
| G-COMPAT | compatibility evidence admits a paused runtime promotion | Fable checkpoint receipt |
| S2.LEDGER | every scoped legacy item has one explicit disposition | disposition manifest and Beads query |
| S3.INSTALL | twelve lessons and every installed destination match source | lesson, installer, resolution, and digest rows |
| S4.MIGRATE | seven named rollback roots precede deletion and every pointer moves | absence sweep, named archive inventory, disposition, and resolution rows |
| G2 | acceptance, AAR cross-check, and all proof receipts support ship | committed Fable G2 receipt |

## 9. Fable gates

G1, G-compat, and G2 are `kind = "checkpoint"`, assigned to Fable, and pre-granted by the locked goal. Pre-grant authorizes the gates to run; it does not predetermine PASS.

- **G1** runs after L0.ENVELOPE and before T0.SPLIT. It reads lint rc 0, route-check rc 0, the derived wave map, the resolved claim ceiling, and the minimum-floor result.
- **G-compat** runs after the full-clone proof and before cell runtime promotion. It reads the clone and source manifests without touching the paused live run.
- **G2** runs after the host install, ledger disposition, and pointer migration. It reads every acceptance and audit receipt and commits its PASS receipt before the skill is declared shipped.

Plan B's own workflow pauses across G-compat and resumes after promotion. Ruling 4's paused-run discipline applies to Plan B itself as well as deck-parity.

ENGINE.C0 and ENGINE.W1 are separate blocker checkpoints for the external resident engine contract. ENGINE.C0 freezes the interface before R2; ENGINE.W1 proves the resident implementation before P1 and P3. Neither is a standing Fable gate or waivable by G1.

## Revision history

**r2, 2026-08-27:** Panel round 1 produced 74 findings, including 13 blockers, across the six persona reports plus the whole-plan read. The coordinator adjudication applied every adopted cluster, resolved each ruling challenge by the amended-ruling path, recorded the rejected alternatives with reasons, and re-derived the graph. The r1 panel is review round 1 of the five-round cap; this r2 patch is not a review round.
