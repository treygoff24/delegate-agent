# Overnight run report — harness compatibility, 2026-09-07

**Outcome.** `feat/harness-compat-audit` is complete, reviewed by three model families, gated green
(3225 tests, up from 3046), pushed to Forgejo, and ready for you to promote. It fixes dlg-ptm and every
other confirmed defect the audits found across the ten supported harnesses at their current releases,
moves Cursor and Oh My Pi prompts off process argv, ships agent-to-agent mail on by default with a
`--no-mail` override, and puts the skill-review preamble behind `tracking.skillReviewPreamble.enabled`
(default off). Nothing was promoted to `~/.delegate` and nothing touched GitHub.

## What shipped (by theme)

**Structured output (the dlg-ptm family).** One eligibility rule, `structured_output.native_schema_eligible`,
now decides native enforcement for both Claude and Codex, on both the workflow and the direct
`--output-schema` paths: explicit root `"type": "object"` and, for Claude, an argv token under 120 000
bytes measured on the exact bytes that go on the command line. Anything else takes the prompt-and-parse
path with a journaled reason, and a launch-time rejection demotes the retry instead of burning it on the
same schema. Claude's answer is read from the documented `structured_output` field, with the text echo as
fallback. Your Opus reviewer lanes work again.

**Event parsing (`harness_events.py`).** Errors now produce failed terminals on every harness, read
`error.message` as well as the top-level field, and are redacted before any persisted sink (the first
version of that fix leaked a bearer token into `recentEvents`; the Opus review caught it). Grok responses
seal on the `usage` boundary so tool preambles stop being glued to the answer; Cursor tool events parse
from the real `(type, subtype)` shape in both the accumulator and the stall watchdog; Pi's renamed
compaction events, Codex usage and preamble suppression, Kimi goal summaries, and unhandled event types are
all handled or counted. Pinned continuity compares model identity per engine (Cursor display names, Claude
dated ids and family aliases) with no substring rule — the reviewer showed containment would accept
`gpt-5.4-mini` for `gpt-5.4`.

**Argv and transport.** Cursor and omp prompts go over stdin (verified live), which deleted two
redaction constants, the 100 KiB argv guard membership, the flag-like-prompt rejection, and the argv
splice branches — and closed the real exposure of the prompt in `/proc/<pid>/cmdline`. Codex `--search`
and `--ask-for-approval` were being parsed and silently dropped before `exec`; they are now `-c` overrides
in the exec scope. Kimi pins its output format; omp pins `--approval-mode yolo` on writable runs and
`--cwd`; Claude safe adds `--permission-prompts none` when discovery proves the flag exists; a warning fires
when an option is typed after the prompt.

**Tables and discovery.** Grok's effort enum is its own (`max` was being sent and rejected); Pi/omp accept
`off`/`minimal`/`auto` where the CLIs do; the bundled Codex/Claude/Grok tables match the live catalogs; a
Grok reasoning-cache row no longer voids the whole cache; Grok's `-` bullets and OpenCode's slashed model
ids parse; Droid custom selectors carry the vendor's index; the Claude probe records `permissionPrompts`.
The discovery-cache reader now tolerates fields written by a newer build instead of discarding the file.

**Boundaries.** bwrap binds `~/.kimi-code` (the real Kimi home) read-write for Kimi only, and only when it
exists; the stall watchdog tracks OpenCode and Grok tool lifecycles and turns its engine default off for the
two harnesses whose stdout is silent during tools, but only when the run has a finite timeout; read-only
OpenCode runs stop ingesting `~/.claude` instructions.

**Mail.** Default on; an absent section means on; `--no-mail` is a global launch-local override; a
workspace where `.delegate/mail` cannot be created degrades with one warning instead of refusing the
launch; the unreachable-sandbox warning is manifest-only and deduplicated; 90-odd lines of dead
parameters, a test-only wrapper, an unreachable branch, and a duplicated loop are gone. `--notify` and
`--mail-push` stay explicit.

**Docs and surfaces.** README, cli-reference, security-model, configuration, troubleshooting, and a
0.31.0-unreleased CHANGELOG entry describe what the code now does; `delegate doctor` warns when
`codex.profile` names a `<name>.config.toml` that does not exist; run records carry
`malformedLines`/`malformedSamples`/`unhandledEventTypes`; `docs/live-runtime.md` gains the
post-promotion `capabilities refresh` step.

## Why these changes, and how the scope was kept honest

The audit brief classified every finding as BROKEN, LATENT, SIMPLIFY, or OK with a vendor citation or a
local reproduction, so the plan could take only defects and the deletions that removed defects. Four
principles then shaped the item list (PLAN.md "Design principles"): fix at the engine boundary rather than
in shared validation; use stdin where a harness now offers it and delete the carve-out; make silent drops
loud; loosen a security gate only to a range the vendor has proven, and keep it a gate.

The Astra plan review (ASTRA-REVIEW.md, 20 items) is where the plan lost weight. It rejected the reused
`_is_object_node` predicate as the root rule, rejected the substring model-identity rule, rejected the
half-done failover-cooldown item and the `ultracode` enum edit, kept the Devin gate closed until its
behavioral probe runs, moved the preamble switch under `tracking` beside `completionReport`, and — the
biggest cut — dropped the ~380-line table refactor of `harness_events.py`/`reasoning.py`, because the shared
audit itself showed the text/terminal handlers are five different state machines, not five spellings.
So this branch is a correctness pass with targeted deletions (+1 804/−407 in `src/`), not a shrink; the
places that got smaller are exactly the places that had a defect.

Three reviewers then read the merged tree independently — Opus (14 findings, 1 blocker), Cursor Grok
(8), GLM 5.3 (7) — and every finding was reproduced before it was fixed or set aside. Two Grok findings
did not hold (Codex accepts `-c` after the stdin `-`; workflow children do get `--timeout`) and are
recorded as such. Every fix landed with a test that was watched failing first; the lane reports name the
mutation used.

## Decisions for you

1. **Promote.** `docs/live-runtime.md` has the steps; the new step 7 (`delegate capabilities refresh`
   per harness) matters because of tonight's incident (below). fin-model's norem workflow
   `wf_8f73ab8b6bd8` is still running on the installed runtime — preflight per that doc or wait.
2. **Devin.** `devin call --read-only` stays refused on Devin ≥ 3000.5 until
   `DELEGATE_DEVIN_BEHAVIOR_TEST` passes on a Devin-equipped machine (the Mac?). Argv is
   documented-compatible with 3000.6.14; only the behavioral proof is missing.
3. **Cursor safe mode.** Both of Cursor's read-only modes block the shell entirely (verified live), so a
   Cursor reviewer in `delegate cursor safe` could no longer run `git diff` or `pytest`. I reverted the
   harness mode for `safe` (the isolated copy remains the boundary, as before) and kept `--mode ask` for
   `cursor call --read-only`, where there is no copy. If you would rather have hermetic-but-shell-less
   reviews, it is one line in `argv_builders.py`.
4. **GitHub.** The history question from dlg-zn9 is unchanged: the 477 receipt files are still in
   `main`'s history (removed from the tree in 0362a90, ignored since). A push of this branch to GitHub
   would carry them. Snapshot branch vs full history is still your call.
5. **Deferred, recorded in CHANGELOG:** followup/resume for grok, droid, opencode, pi, kimi, devin (all
   now possible, all need a live round trip); Grok's Messages-format stream (needs a captured fixture);
   Claude `--restricted`; Droid `--auto` tiers; non-codex failover cooldown; `ultracode`.

## Incident

At ~07:00Z Lane A ran `delegate capabilities refresh claude` with branch code from its worktree. That
wrote the shared discovery cache with a per-harness `capabilities` field; the installed 0.30.0 reader
rejects unknown fields and discarded the whole file, so `gpt-6-astra` (absent from the old bundled table)
failed closed with `unsupported_reasoning_effort`. fin-model's norem T6 failed twice and rotated to Sol.
fin-model's own `capabilities refresh codex` at 07:24Z restored it. Fixes: the tolerant reader (F17), the
promotion-checklist step, and a docs warning that `setup`/`capabilities refresh`/`models --live` from a
development checkout write the shared cache. Papercut filed by fin-model for the lost child stderr on
pre-run launch failures.

## Verification, stated plainly

- `scripts/gate.sh` (pytest, compileall, ruff check, ruff format, pinned ruff 0.15.15) PASS at every
  merged checkpoint; final: see the gate line in the closing message.
- Live smokes: SMOKE.md (18 rows). Not exercised live: any Kimi/Devin/Droid/OpenCode child; Cursor
  `--resume` with a stdin prompt; Codex session-file flush after `turn.completed` (F5 chose not to arm the
  exit kill rather than prove the timing).
- Cross-model reviews: REVIEW-OPUS.md, REVIEW-GROK.md, REVIEW-GLM.md; every finding's disposition is in
  the Lane F brief and report.
- Independently verified by me (re-execution on the merged tree): the redaction blocker, Kimi bwrap with
  an absent home, cursor safe/read-only argv, Pi plain-text fallback, Grok `max` refusal, plus each lane's
  focused test files before its merge.

## Housekeeping done tonight

Cell cleanup freed 28 GB (34 merged worktrees, 108 orphaned processes, caches); this repo's seven merged
`plan-burndown*` worktrees and branches removed; eight dirty `delegate/*` worktrees from this morning's
lanes left for you; `post-bridge.service` fails on purpose (2 undeliverable + 9 `forged_from` quarantined
messages, 6–13 days old) — needs a ruling; 21 GB of prospera-corpus delegate worktrees hold real
uncommitted edits — left alone. Open beads: dlg-cjz.4 (mise stderr noise breaks two shim tests under a
temp HOME in Sol lanes), dlg-o7i (omp review lane exceeded delegate's 16 MiB stdout cap).
