# Wave 6 holistic review record

> **Historical opening verdict: DON'T-SHIP.** The findings and round-4
> re-verdict below are preserved as review history. They are superseded by the
> post-round-5 final re-verdict at the end of this document.

## Findings

### 1. HIGH - Grouped calls bypass `--timeout`; retried ungrouped calls reset it (`src/delegate_agent/cli.py:648-664`, `src/delegate_agent/runner.py:2412-2457`)

**Defect:** Grouped calls use `execute_tracked()`, whose call site and API carry no timeout. Ungrouped read-only/pure calls do pass the timeout, but pass the full value independently to the first and retry attempts.

**Concrete scenario:** `delegate --group wf_x ... call --timeout 30` can run indefinitely. The same ungrouped call can run for nearly 60 seconds if its first attempt consumes 30 seconds and returns empty, despite help defining the value as the maximum child runtime for the call.

**Fix direction:** Define one monotonic call deadline, thread the remaining budget through tracked and untracked attempts, and either honor that deadline for grouped calls or reject grouped `--timeout` until it can be enforced.

### 2. HIGH - Grouped `call` exposes the registry workspace that its contract says stays hidden (`src/delegate_agent/cli.py:580-603`, `src/delegate_agent/cli.py:619-652`, `docs/cli-reference.md:442-453`)

**Defect:** `_set_child_root_env()` exports the grouped call's real invocation workspace as `DELEGATE_SOURCE_ROOT`. Grouped-call documentation says `--cwd` is used only for registry/config resolution and does not give the child the project tree. Ungrouped calls receive a different bad value: the resolved literal placeholder `<delegate-call-temp-cwd>` rather than an actual source root.

**Concrete scenario:** A grouped read-only call launched with `--cwd /sensitive/project` is explicitly handed `/sensitive/project` in its environment. Engines whose read-only boundary is prompt-enforced can then inspect that absolute path despite being told there is no repository to inspect.

**Fix direction:** Make root metadata mode-aware. A call has no source checkout: omit `DELEGATE_SOURCE_ROOT` or set both roots to the actual temporary call cwd, and keep the registry workspace parent-only.

### 3. HIGH - Empty retry violates the pure/verbatim and slash-passthrough boundaries (`src/delegate_agent/runner.py:1681-1686`, `src/delegate_agent/runner.py:1862-1888`, `src/delegate_agent/runner.py:2429-2457`)

**Defect:** Retry eligibility includes pure calls and all safe runs, then appends Delegate prose without consulting `prompt_instruction_mode`. Pure mode promises stdin is verbatim; slash passthrough promises no suffix and requires the slash command at position zero (`src/delegate_agent/request_build.py:439-442`, `docs/security-model.md:104-112`).

**Concrete scenario:** A hostile-content pure completion gets a second, Delegate-mutated prompt. A safe `/review` or other harness slash command that returns empty is resent with a prose suffix, changing command parsing and semantics.

**Fix direction:** Do not prompt-mutate pure or slash-passthrough requests. Retry them only if the harness offers an out-of-band continuation mechanism; otherwise skip with an explicit warning and keep the verbatim contract intact.

### 4. HIGH - A failed retry is serialized as `emptyRetry.resolved=true` (`src/delegate_agent/runner.py:1689-1700`, `src/delegate_agent/runner.py:1895-1908`, `src/delegate_agent/runner.py:2459-2477`)

**Defect:** Tracked capture quality deliberately returns `ok` for any nonzero/failed capture, and the untracked classifier likewise reserves `empty` for exit zero. Both retry paths define `resolved` as merely “quality is not empty.”

**Concrete scenario:** The first attempt exits zero with no answer; the retry crashes or exits 1 with no answer. The envelope can simultaneously report `status=failed`, `resultQuality=ok`, and `emptyRetry.resolved=true`.

**Fix direction:** Resolution must require a successful retry with usable final output and no failed/cancelled terminal state. Keep failure classification separate from answer-quality classification.

### 5. MEDIUM - The source-root guard reverses containment for existing descendants (`src/delegate_agent/isolation.py:212-229`, `tests/test_delegate_isolation.py:466-481`)

**Defect:** The identity walk starts at `target` and ascends toward the filesystem root. That detects “target is inside source,” the inverse of the destructive condition “target is or contains source.” The lexical fallback uses the correct direction, so the same path changes answer when created.

**Concrete scenario:** With `TMPDIR` inside the source repository, a call runs in an existing descendant temp directory, then cleanup raises `source_root_guard`, masks the child result, and leaks the directory. A configured persistent-worktree data home inside the repo is similarly made undeletable. The current test checks only a nonexistent descendant and passes vacuously.

**Fix direction:** Walk upward from the resolved source while comparing it to the target. Add existing/missing descendant parity tests and end-to-end cleanup tests with a repo-local `TMPDIR` and data home.

### 6. MEDIUM - A second tracked launch failure leaves a partially registered run (`src/delegate_agent/runner.py:1791-1810`, `src/delegate_agent/runner.py:1822-1838`, `src/delegate_agent/runner.py:1863-1889`)

**Defect:** Only the initial tracked launch converts `OSError` and calls `_record_tracked_launch_failure()`. Auth-fallback and empty-retry launches sit outside that boundary.

**Concrete scenario:** If the executable disappears between attempts or the retry `Popen` fails with `EMFILE`, a raw exception escapes after the first attempt. The registry still records `running` with the now-dead first PID, and isolated-workspace cleanup can remove the execution cwd referenced by that record.

**Fix direction:** Put every attempt behind one launch wrapper that converts the typed error and terminally records partial runs while preserving prior logs, counters, and attempt metadata.

### 7. MEDIUM - Direct children can inherit a stale outer execution root (`src/delegate_agent/cli.py:580-588`, `src/delegate_agent/profiles.py:232-251`)

**Defect:** For direct work, `_set_child_root_env()` removes `DELEGATE_EXECUTION_ROOT` only from the override dict. `child_environment()` first copies ambient `os.environ`, so an inherited value remains.

**Concrete scenario:** A child launched by Delegate invokes Delegate against another repository in direct work mode. The grandchild inherits the outer isolated run's `DELEGATE_EXECUTION_ROOT`, so hooks or child logic enforce boundaries against the wrong workspace even though `describe` promises the variable is omitted for direct work.

**Fix direction:** Strip reserved Delegate root variables from the ambient base before applying authoritative values, or add explicit deletion semantics. Test nested direct, temporary, persistent, pure, and fallback launches.

### 8. MEDIUM - Codex auth fallback can overwrite Delegate's authoritative root variables (`src/delegate_agent/profiles.py:326-341`, `src/delegate_agent/runner.py:1822-1838`)

**Defect:** Fallback profile environment is merged after the canonical child overrides. Profile validation permits `DELEGATE_SOURCE_ROOT` and `DELEGATE_EXECUTION_ROOT`, so fallback can replace both even when the primary attempt received correct roots.

**Concrete scenario:** A Codex primary hits a usage limit; the configured fallback profile contains an old or crafted Delegate root. Only the fallback child and its hooks receive the spoofed path, making behavior account-dependent.

**Fix direction:** Treat the root variables as reserved and authoritative. Construct fallback overrides from the already-canonical primary environment and change only fallback-specific auth values such as `CODEX_HOME`.

### 9. MEDIUM - Auth fallback plus empty retry undercounts stored output (`src/delegate_agent/runner.py:1819-1838`, `src/delegate_agent/runner.py:1890-1894`)

**Defect:** After auth fallback, `capture` is replaced with `fallback_capture`, discarding the primary attempt's byte counters. Empty retry then aggregates only fallback plus retry even though the logs contain all three attempts.

**Concrete scenario:** A quota-failed primary writes 26 stderr bytes, an empty fallback writes 16, and the final retry writes 16. The stderr log contains 58 child bytes plus delimiters, while the envelope reports 32.

**Fix direction:** Maintain cumulative counters across every attempt independently of which capture supplies final status/assistant text. Add the three-attempt auth-fallback/empty-retry composition to envelope and snapshot tests.

### 10. MEDIUM - Retried Claude calls drop first-attempt token usage (`src/delegate_agent/runner.py:2466-2477`)

**Defect:** The untracked retry result is built with `replace(retry, ...)`. Duration, byte counts, warnings, and truncation are combined, but `usage` remains the retry's usage only.

**Concrete scenario:** A Claude attempt consumes nonzero input/output tokens but returns an empty final result; after a successful retry, billing/telemetry surfaces omit the first attempt's tokens.

**Fix direction:** Sum exact usage fields where the provider semantics permit it, or expose per-attempt usage plus an explicit aggregate basis.

### 11. MEDIUM - Grouped write-capable calls violate the additive envelope contract (`src/delegate_agent/runner.py:1909-1913`, `src/delegate_agent/describe_payload.py:827-829`, `docs/cli-reference.md:775-781`)

**Defect:** A grouped write-capable call that returns empty emits `emptyRetry: {attempted: false, reason: write_capable_call}`. The authored/public contract requires omit-when-not-attempted and describes only `{attempted, resolved}`. Ungrouped write-capable calls already omit the field, so tracking changes the schema for the same execution policy. The docs also incorrectly say every call retries.

**Concrete scenario:** A consumer written to the additive `{attempted, resolved}` shape sees an unexpected field with no `resolved` key only when `--group` is present.

**Fix direction:** Omit `emptyRetry` in both skipped paths and retain a warning if useful. Document the actual eligibility as safe plus read-only/pure call, subject to the verbatim-boundary fix above.

### 12. MEDIUM - NUL-delimited dirty sync is not arbitrary-byte-safe (`src/delegate_agent/safe_workspace.py:243-258`, `src/delegate_agent/safe_workspace.py:318-334`)

**Defect:** Git paths are decoded with `surrogateescape`, then `_git_check_ignore()` re-encodes them with strict UTF-8 and decodes Git output with replacement. Path identity is therefore lost or raises before the typed sync-error boundary.

**Concrete scenario:** On a POSIX filesystem that permits non-UTF-8 filename bytes, an untracked symlink whose target is checked for ignore status can raise `UnicodeEncodeError`; persistent auto-sync aborts and safe isolation can leak an untyped exception.

**Fix direction:** Use `os.fsencode`/`os.fsdecode` consistently or keep Git path identities as bytes end-to-end. Add Linux-capable invalid-byte regular-file, symlink, ignored-target, warning, and teardown tests.

### 13. MEDIUM - Dirty-submodule behavior and cleanup advice still describe the removed contract (`src/delegate_agent/worktree_execution.py:155-169`, `docs/worktrees.md:52-53`, `docs/troubleshooting.md:279-294`)

**Defect:** The implementation refuses dirty submodules with `dirty_source_workspace`, but the primary worktree/reference pages merely say their state is “not mirrored,” which implies launch continues. Troubleshooting first says ordinary dirt is auto-synced, then tells any dirty source to commit/stash. `droid-wiki/systems/isolation-and-worktrees.md:31` and `droid-wiki/how-to-contribute/patterns-and-conventions.md:15` still say persistent worktrees require/refuse any dirty checkout.

**Concrete scenario:** An operator following `docs/worktrees.md` expects a dirty submodule to be omitted and a clean submodule checkout to launch; Delegate instead fails preflight. Another operator needlessly stashes ordinary tracked/untracked changes based on the stale troubleshooting paragraph.

**Fix direction:** State that ordinary tracked/untracked dirt auto-syncs but dirty submodules fail preflight, and narrow commit/stash guidance to submodules or actual sync failures across every documentation surface.

### 14. MEDIUM - Devin's mode catalog is not coherent across discovery surfaces (`src/delegate_agent/describe_payload.py:425-490`, `src/delegate_agent/describe_payload.py:1282-1288`, `src/delegate_agent/argv_builders.py:399-423`)

**Defect:** Devin correctly rejects `safe` and focused help/missing-mode output says `work` or `call`, but `models --summary` advertises `delegate devin {safe,work,call}` with `safeSupported: true`. Full text `describe` says only `modes: safe, work`, omitting call globally.

**Concrete scenario:** An agent discovers Devin through the summary JSON, selects its advertised safe mode, and receives `unsupported_mode`; another agent parsing text `describe` concludes call mode does not exist.

**Fix direction:** Derive summary commands, support booleans, text describe, and errors from one per-engine mode registry. Remove the parallel handwritten overview/mode lists rather than patching more copies.

### 15. LOW - Source-root resolution failures escape the typed guard path (`src/delegate_agent/isolation.py:212-225`)

**Defect:** `Path.resolve(strict=False)` runs before the fail-closed `samefile` handling and can itself raise `OSError` or `RuntimeError` (for example, on a symlink loop).

**Concrete scenario:** A malformed registry `executionCwd` used by worktree cleanup raises an unhandled exception, masking the original setup failure and preventing `cleanupFailed`/`cleanupRefused` metadata from being recorded. Deletion remains refused, but the error taxonomy and recovery record are lost.

**Fix direction:** Catch resolution failures at the outer guard boundary, return/refuse conservatively, and preserve structured cleanup metadata and the original failure.

### 16. LOW - Dirty-submodule paths can forge multiline diagnostics (`src/delegate_agent/worktree_execution.py:162-168`)

**Defect:** Ordinary dirty-file examples are escaped with `repr`, but dirty submodule paths are joined raw into the typed error message.

**Concrete scenario:** A legal submodule path containing a newline injects an apparent extra stderr diagnostic line or corrupts a log parser's record boundary.

**Fix direction:** Render bounded `repr(path)` previews and include an omitted-count suffix, matching the ordinary dirty disclosure policy.

### 17. LOW - Retry replacement drops first-attempt stdin delivery diagnostics (`src/delegate_agent/runner.py:1890-1894`, `src/delegate_agent/runner.py:1918`)

**Defect:** After retry, only `retry_capture.stdin_failures` survives; the first attempt's tuple is discarded.

**Concrete scenario:** The first child closes stdin and exits empty, then the retry succeeds. The final run loses the warning that explains why the first attempt produced no answer.

**Fix direction:** Merge and deduplicate attempt diagnostics while replacing only the active capture's process/status fields.

### 18. LOW - Fix-round churn left obsolete mechanisms in production code (`src/delegate_agent/isolation.py:248-268`, `src/delegate_agent/safe_workspace.py:232-240`, `src/delegate_agent/safe_workspace.py:261-263`, `src/delegate_agent/argv_builders.py:400-412`)

**Defect:** `require_clean_source()` and `_git_lines()` have no production callers; `dirty_sync_counts()` is test-only; and the Devin-safe `read_only` arm is unreachable after the earlier `unsupported_mode` raise. Tests still pin some of the removed clean-source behavior.

**Concrete scenario:** Future maintainers can update or reuse the obsolete clean-source and Devin-safe branches believing they remain authoritative, recreating the contract divergence already visible in docs and discovery.

**Fix direction:** Delete the dead helpers, obsolete tests, and unreachable arm. Keep one snapshot/preflight mechanism and one supported-mode source.

## Coverage map

- Verified the exact `main..feature/wave6-hardening` range: 29 commits, 45 changed files, with local `main` as the merge base.
- Inspected dirty-state collection/application, NUL path handling, symlink and submodule classification, pre-launch disclosure, persistent-worktree creation/registration/teardown, worktree remove/prune/GC, and every source-root guard call site.
- Inspected child root-environment construction through direct, temporary, persistent, grouped-call, pure, nested, and Codex auth-fallback paths.
- Inspected tracked and untracked retry launch/capture/finalization, auth fallback, timeout handling, prompt transport, attempt logs, state/snapshot transitions, byte/truncation/duration/usage aggregation, and all `emptyRetry` serializers.
- Diffed the new/changed completion, snapshot, run-output, and bare-handle envelope fields against `main`; no unrelated field rename or type change was found beyond the findings above.
- Inspected bare-handle resolution/age normalization/stale warnings and `run-output --tail` selection; no defect was found at the review severity floor.
- Compared `COMMAND_SPECS`, focused help, overview help, `describe`, `models --summary`, README, all changed `docs/` pages, the droid wiki, and Wave 6 design/review briefs against implementation.
- Reviewed the changed tests for vacuous coverage and ran targeted read-only probes for existing-versus-missing guard descendants, ambient/fallback environment precedence, Devin discovery output, and retry/accounting edge cases.
- Used three independent subsystem readers for worktree/security, retry/envelopes, and contracts/errors, then rechecked each accepted finding against the final branch.
- Final gates: all 1,360 unit tests passed on a clean rerun (7 skipped), the one workflow-kill test that failed during the first full run passed in isolation, and `ruff check .`, `ruff format --check .`, and both diff whitespace checks passed.

## Explicitly not probed

- The referenced `CONTEXT.md` terminology contract is absent from both `main` and the branch, so terminology could not be checked against it.
- No real provider CLI, live auth/account fallback, network request, or production workflow was executed.
- No wall-clock timeout hang, process-table race, concurrent path swap, or executable-disappears-between-attempts failure was injected end to end.
- Invalid-byte Linux filenames, case-folding filesystem behavior, WSL namespace behavior, and symlink-loop registry artifacts were reviewed statically/read-only rather than exercised on their native platforms.
- Packaging/build/install promotion and the live `~/.delegate` runtime were not probed because this was a branch review, not a release or install task.

## Round-4 re-verdict (2026-07-16, superseded)

### Original findings

1. **Fixed** — Grouped calls now pass `request.timeout`, and tracked and untracked retries consume one monotonic deadline (`src/delegate_agent/cli.py:654-670`, `src/delegate_agent/runner.py:1816-1822`, `src/delegate_agent/runner.py:2479-2527`, `tests/test_runner_capture.py:2644-2683`).
2. **Fixed** — Call-mode root metadata now resolves to the throwaway execution cwd rather than the registry workspace (`src/delegate_agent/cli.py:581-593`, `src/delegate_agent/request_build.py:768-772`, `tests/test_wave4_launch_features.py:105-112`).
3. **Fixed** — Pure and slash-passthrough prompts skip mutation and retry while emitting the verbatim-boundary warning (`src/delegate_agent/runner.py:1918-1920`, `src/delegate_agent/runner.py:2495-2500`, `tests/test_runner_capture.py:2685-2709`).
4. **Fixed** — Empty retry resolves only for exit zero, a non-failed/cancelled terminal state, and usable output (`src/delegate_agent/runner.py:1962-1975`, `src/delegate_agent/runner.py:2531-2550`, `tests/test_runner_capture.py:2711-2749`).
5. **Fixed** — The source-root guard now walks upward from the source, and existing and missing descendants agree and clean up successfully (`src/delegate_agent/isolation.py:212-232`, `tests/test_delegate_isolation.py:451-470`, `tests/test_execution_argv_and_prompt.py:82-112`).
6. **Partial** — Every tracked attempt now terminalizes launch failures, but the shared failure recorder still replaces prior attempt state with zero counters and an empty snapshot (`src/delegate_agent/runner.py:1259-1282`, `src/delegate_agent/runner.py:1684-1729`); an executable-disappears probe preserved primary stderr in the log while state reported `stderrBytes=0` and no `finishedAt`.
7. **Fixed** — Child environment construction strips ambient Delegate roots before applying authoritative overrides (`src/delegate_agent/profiles.py:232-254`, `tests/test_profiles.py:673-682`).
8. **Fixed** — Codex fallback overrides retain the primary authoritative roots and filter profile-supplied root values (`src/delegate_agent/profiles.py:337-351`, `tests/test_profiles.py:684-707`).
9. **Partial** — The primary + fallback + empty-retry case now sums all byte counts (`src/delegate_agent/runner.py:1860-1894`, `src/delegate_agent/runner.py:1948-1955`, `tests/test_runner_capture.py:2550-2598`), but a substantive successful fallback skips the retry branch and still finalizes fallback-only counters and diagnostics (`src/delegate_agent/runner.py:1984-1992`).
10. **Fixed** — Exact Claude usage is summed across both call attempts, while non-exact inputs conservatively return an unavailable basis (`src/delegate_agent/runner.py:551-561`, `src/delegate_agent/runner.py:2538-2550`, `tests/test_runner_capture.py:2427-2454`).
11. **Partial** — Skipped retries now omit `emptyRetry`, and only attempted retries serialize `{attempted, resolved}` (`src/delegate_agent/runner.py:1918-1979`, `src/delegate_agent/cli.py:741-746`, `tests/test_runner_capture.py:2751-2793`), but `docs/cli-reference.md:777-779` still incorrectly says every call retries.
12. **Fixed** — Git ignore probes round-trip surrogateescaped paths with `os.fsencode`/`os.fsdecode` (`src/delegate_agent/safe_workspace.py:286-318`, `tests/test_safe_workspace_isolation.py:99-107`); native invalid-byte end-to-end coverage remains Linux-only and was not available on this macOS host.
13. **Partial** — Worktree, troubleshooting, and wiki pages now say dirty submodules fail preflight, but the canonical CLI reference still says their state is merely “not mirrored” (`docs/cli-reference.md:122-123`), and the documentation contract test omits that file (`tests/test_execution_worktree_preflight.py:13-25`).
14. **Fixed** — Devin discovery now derives `{work,call}` and support flags from the engine mode registry, and text describe includes call (`src/delegate_agent/constants.py:25-36`, `src/delegate_agent/describe_payload.py:428-492`, `src/delegate_agent/describe_payload.py:1285-1290`, `tests/test_delegate_help_cli.py:627-632`).
15. **Partial** — `OSError` and `RuntimeError` during root resolution now fail closed (`src/delegate_agent/isolation.py:212-218`, `tests/test_delegate_isolation.py:494-497`), but an embedded-NUL registry path still raises an untyped `ValueError` at the later existence probe.
16. **Fixed** — Dirty-submodule diagnostics now use bounded `repr()` previews with an omitted-count suffix (`src/delegate_agent/worktree_execution.py:162-170`, `tests/test_execution_worktree_preflight.py:143-148`).
17. **Fixed** — Primary, auth-fallback, and empty-retry stdin delivery failures are merged and deduplicated before finalization (`src/delegate_agent/runner.py:1860-1893`, `src/delegate_agent/runner.py:1948-1955`).
18. **Fixed** — `require_clean_source`, `_git_lines`, and `dirty_sync_counts` are gone, and Devin’s remaining read-only arm is call-only (`src/delegate_agent/argv_builders.py:399-422`; repo-wide symbol search found no remnants).

### New findings

- **LOW — Tracked timeouts leak child pipe descriptors and drain threads.** The timeout raises before the join/close block (`src/delegate_agent/runner.py:1378-1385`, `src/delegate_agent/runner.py:1438-1442`); the isolated timeout test reproducibly passes with two unclosed-`FileIO` `ResourceWarning`s.
- **LOW — The Devin argv builder now accepts unknown modes as dangerous work.** After removing the unreachable safe arm, every other non-read-only value gets `--permission-mode dangerous` without `validate_mode()` (`src/delegate_agent/argv_builders.py:399-422`); `build_devin_argv(..., "bogus", ...)` produced runnable argv, although normal CLI parsing rejects it earlier.

### Verification

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests`: 1,370 passed, 7 skipped.
- `ruff check .`, `ruff format --check .`, `python3 -m compileall -q src tests bin`, and `git diff --check 66597ec^..HEAD`: passed.
- Targeted probes covered the grouped timeout, second-attempt executable disappearance, fallback accounting, embedded-NUL root handling, Devin discovery, and unknown-mode builder path.

**Superseded round-4 verdict: SHIP-WITH-FIXES.** At this point no HIGH-severity blocker remained, but the residual attempt-accounting and documentation defects still required correction before Wave 6 could close.

## Post-round-5 final re-verdict (2026-07-16)

Round 5 closed the six residuals from the superseded re-verdict: tracked launch
failures and successful authentication fallback now retain cumulative attempt
state; tracked timeout streams close cleanly; retry and dirty-submodule docs
match behavior and are contract-tested; malformed Devin modes fail closed; and
the final retry documentation contract is pinned. The approved authored spec now
records the two safety-driven deviations discovered during review rather than
pretending the original wording shipped unchanged.

### Per-item release record

| Item | Status | Primary commit | Test additions and final coverage |
| --- | --- | --- | --- |
| Auto include-dirty | Shipped | `9b66aee` | `test_execution_worktree_preflight`, `test_execution_worktree_failure_cleanup`, and `test_wave4_launch_features` cover clean, tracked, untracked, non-Git, sync-failure teardown, dirty submodules, warning counts, and live launch behavior. Later hardening covers arbitrary filename bytes and escaped diagnostics. |
| Source-root guard | Shipped | `5e462b5` | `test_delegate_isolation`, `test_safe_workspace_isolation`, `test_execution_worktree_failure_cleanup`, `test_wave4_launch_features`, and `test_worktree_remove` cover child envs plus absolute, relative, symlinked, case-variant, malformed, and descendant cleanup paths. Profile tests cover ambient and fallback precedence. |
| Empty-success retry | Shipped | `254d576` | `test_runner_capture`, `test_execution_argv_and_prompt`, and `test_codex_pure_sandbox` cover successful and still-empty retries, output/accounting retention, one deadline, tracked launch failures, authentication fallback, and no replay for work, write-capable call, pure, or slash pass-through requests. |
| `run-output --tail` | Shipped | `47e6f11` | `test_snapshot_run_output` and `test_delegate_help_cli` pin implicit stdout, explicit selectors, stderr opt-in, invalid combinations, and generated help. |
| Bare-handle resolution | Shipped | `a1d9def` | `test_snapshot_run_output` and `test_wait_cancel_commands` cover Snapshot, run-output, and wait JSON/text metadata plus the older-than-24-hours warning. |
| Devin safe preflight | Shipped | `2471154` | `test_engine_argv` and `test_delegate_help_cli` cover `unsupported_mode`, work/call preservation, unknown-mode rejection, and coherent help/discovery output. |

All six primary SHAs above resolve on `feature/wave6-hardening`. Review fixes
remain as focused follow-up commits rather than amendments; this intentionally
preserves auditability after the six primary feature commits.

### Approved deviations

- Empty-success replay is limited to safe and read-only call requests whose
  prompt transport permits mutation. Write-capable calls are not replayed
  because they may duplicate side effects; pure and slash pass-through prompts
  are not replayed because Delegate must preserve them verbatim.
- Call mode has no source checkout. Its throwaway cwd is exported as
  `DELEGATE_SOURCE_ROOT`, and the Registry/config workspace is not disclosed to
  the child. `DELEGATE_EXECUTION_ROOT` is emitted only when source and execution
  roots differ.
- The branch contains review and fix commits beyond the six primary item
  commits. This follows the no-amend rule and leaves each correction reviewable.

### Residual risks

- Hook-side enforcement for `DELEGATE_SOURCE_ROOT` and
  `DELEGATE_EXECUTION_ROOT` remains machine configuration outside this repo.
- Persistent worktrees and temporary isolation remain containment mechanisms,
  not host security sandboxes; children retain whatever absolute-path, network,
  credential, and tool access their runtime permits.
- Invalid-byte filenames, case-folding behavior, process races, and live
  provider authentication fallback received unit/static coverage but were not
  all exercised on every supported platform or against every provider CLI.
- Tag creation and live publication remain separate release actions.

### Final verification and verdict

The coordinator verified a fresh `origin/main` and merge base at
`bfea26d5fe2f1795893245eaa66f412b569add96`, with a clean worktree before this
evidence-only documentation update. The final local gates passed:

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests`: 1,374
  passed, 7 skipped in 210.850 seconds.
- `python3 -m compileall -q src tests bin`, `ruff check .`,
  `ruff format --check .`, and the three-dot diff whitespace check: passed.
- Gitleaks full-history scan: 346 commits scanned, no leaks. TruffleHog's Git
  scan and clean `git archive` filesystem scan: zero verified or unverified
  secrets. A follow-up hygiene sweep removed the private performance journal
  and private-alias-named review artifacts; the remaining shippable tree scan
  was empty.
- A clean `git archive` build produced the 0.15.0 sdist and wheel; Twine passed
  both, the forbidden-artifact scan was empty, and a clean wheel install
  reported `delegate 0.15.0` with 51 commands in `describe`.
- Safe dry-runs passed for codex, claude, grok, opencode, and kimi. These were
  argv/config preflight checks only; no live provider child was executed.

This evidence supersedes the earlier residual note that packaging and secret
scans were still pending. Tag creation and live publication remain separate
release actions.

**Final verdict: SHIP.** The authored contract, implementation, public docs, and
release metadata now agree. No known Wave 6 correctness or compatibility blocker
remains; the residuals above are bounded release/platform risks, not hidden
feature defects.
