# Fix review: `call --pure` hardening

## Verdict

**Not correct and complete for the dogfood scope.** Claude is now the only
reachable pure engine, and the Claude tripwire fails closed correctly, but the
stdin timeout remains bypassable by the exact real full-pipe condition it is
supposed to bound. Three smaller honesty/test inconsistencies also remain.

The required compile, unit, lint, and format gates are green. That does not
clear the timeout finding: the new fake-pipe test does not reproduce the lock
behavior of `subprocess.PIPE`.

## Residual findings

### High: closing stdin before terminating can still block past the deadline

The new writer thread moves the blocking write off the main thread and starts
the deadline before it (`src/delegate_agent/runner.py:1786-1789`,
`src/delegate_agent/runner.py:1820-1843`). On expiry, however, the main thread
calls `process.stdin.close()` before terminating the process group
(`src/delegate_agent/runner.py:1897-1904`). A real `Popen.stdin` is a buffered
writer. If its writer thread is blocked on a full pipe while holding the
buffered-writer lock, `close()` waits for that same lock. Delegate never reaches
the termination call that would close the child's read end and unblock the
writer.

The added test passes because its fake `close()` only flips a boolean and never
contends with `write()` (`tests/test_pure_call.py:384-402`). My equivalent
sleeping-fake probe returned `call_timeout` in 0.306 seconds for a 0.25-second
deadline. A second no-network probe used a real `subprocess.PIPE`, a child that
never reads stdin, and a 1 MiB prompt: the helper was still hung after four
seconds on a one-second timeout and had to be killed externally.

Terminate the process group before closing the parent stdin handle, then close
and join the I/O threads. Add a regression test using a real child that does not
read a payload larger than pipe capacity; the current cooperative fake is not a
sufficient model.

### Medium: `describe` still falsely reports that Claude lacks `outputSchema`

The canonical capability map correctly marks both Claude and Codex as
`structuredOutput` capable (`src/delegate_agent/constants.py:34-43`), and the
CLI reference now documents both (`docs/cli-reference.md:550-555`). But
`_engine_capabilities()` still hard-codes the legacy `outputSchema` field to
Codex only (`src/delegate_agent/describe_payload.py:623-630`). A live local
`--json describe` probe returned:

```text
claude: outputSchema=false, structuredOutput=true
codex:  outputSchema=true,  structuredOutput=true
```

This leaves the machine-facing contract internally contradictory and means fix
8 is incomplete. Derive `outputSchema` from `structuredOutput`, or remove the
legacy alias if compatibility permits. The changed capability test checks only
`structuredOutput`, so it misses the contradiction
(`tests/test_capability_commands.py:178-188`).

### Low: the opt-in Codex live suite is now guaranteed to fail

The affirmative environment gate works (`tests/test_codex_pure_sandbox.py:22-39`,
`tests/test_codex_pure_sandbox.py:382-386`), so default discovery skips all
seven live tests. But the gated helper still invokes `codex call --pure` and
requires success (`tests/test_codex_pure_sandbox.py:390-416`), while the same
file now asserts that this command must be rejected
(`tests/test_codex_pure_sandbox.py:349-354`). Enabling
`DELEGATE_RUN_LIVE_CODEX_PURE_TESTS=1` therefore selects an impossible suite.
Remove or quarantine these tests until Codex becomes eligible again; do not
present the flag as a usable integration gate in the meantime.

### Low: focused `dry-run` help does not state the Claude-only restriction

Per-engine help and the top-level overview are correct. Focused
`delegate dry-run --help` is not: its call usage groups Claude with ineligible
engines (`src/delegate_agent/command_help.py:524-545`), the augmentation looks
for the literal substring `"claude call"` and therefore adds no `--pure` usage
(`src/delegate_agent/describe_payload.py:136-152`), while the generic dry-run
option list still advertises `--pure` without saying Claude-only
(`src/delegate_agent/describe_payload.py:172-181`). Split out the Claude call
usage or label the option `Claude call mode only`.

## Fix-by-fix verification

| # | Result | Verification |
| --- | --- | --- |
| 1. Remove Codex eligibility | **Pass** | `pure_call_supported()` returns true only for Claude and feeds `PURE_CALL_ENGINES` (`src/delegate_agent/constants.py:26-48`). Codex also rejects at argv construction (`src/delegate_agent/argv_builders.py:471-475`) and shared request construction (`src/delegate_agent/request_build.py:1487-1493`). Direct CLI and `run --input-json` probes both returned `unsupported_pure_call` before launch. |
| 2. Remove OpenCode eligibility | **Pass** | The same canonical registry excludes OpenCode (`src/delegate_agent/constants.py:26-48`), and its argv builder rejects pure before constructing native `--pure` arguments (`src/delegate_agent/argv_builders.py:426-442`). Direct CLI, direct builder, and `run --input-json` paths reject it; the regression coverage is at `tests/test_pure_call.py:46-58` and `tests/test_delegate_parser.py:252-274`. |
| 3. Fail-closed Claude tripwire | **Pass** | Missing or non-list evidence returns exit 1 / `pure_boundary_unverified`; non-empty lists return `pure_boundary_violation`; only an empty list reaches normal success (`src/delegate_agent/runner.py:1977-2007`). Direct parser probes produced the required outcomes for missing, null, object, empty, and non-empty values. Tests cover malformed shapes and both list cases (`tests/test_pure_call.py:188-270`). |
| 4. Timeout covers stdin write | **Fail** | The common deadline and writer thread landed (`src/delegate_agent/runner.py:1786-1789`, `src/delegate_agent/runner.py:1820-1873`), but close-before-terminate can still block indefinitely (`src/delegate_agent/runner.py:1897-1904`). The required sleeping fake passes; a real full-pipe probe hangs past the deadline. |
| 5. Remove auth hardlinks | **Pass** | `_copy_auth()` unconditionally uses `shutil.copy2()` and sets mode `0600` (`src/delegate_agent/runner.py:2032-2035`); the Codex branch calls it (`src/delegate_agent/runner.py:2075-2080`). The inode/mutation regression test confirms an independent copy (`tests/test_pure_call.py:439-449`). |
| 6. Gate live Codex tests | **Pass for default discovery; residual above** | The environment flag is checked before all platform/auth probes (`tests/test_codex_pure_sandbox.py:22-39`) and guards the class (`tests/test_codex_pure_sandbox.py:382-386`). The isolated-HOME full suite skipped seven tests. The opt-in suite itself is now contradictory and unusable. |
| 7. Advertise pure only where eligible | **Partial** | All eight focused engine-help probes and every top-level overview call line advertise `--pure` only for Claude. `_call_help_spec()` derives per-engine exposure from `pure_call_supported()` (`src/delegate_agent/describe_payload.py:132-188`). Focused `dry-run` help retains the ambiguous generic option described above. |
| 8. Honest README/reference and schema capability | **Partial** | README now says Claude-only, hostile-input, and schema-capable (`README.md:189-200`); the CLI reference says Claude-only and distinguishes ordinary Codex schema use (`docs/cli-reference.md:397-408`) and documents JSON `outputSchema` for both engines (`docs/cli-reference.md:550-555`). The legacy `describe.outputSchema` field remains wrong for Claude (`src/delegate_agent/describe_payload.py:623-630`). |
| 9. Distinct stderr overflow code | **Pass** | The drain records the overflowing stream and raises `call_stdout_overflow` or `call_stderr_overflow` accordingly (`src/delegate_agent/runner.py:1783-1814`, `src/delegate_agent/runner.py:1905-1911`). The stderr regression test asserts the distinct code (`tests/test_pure_call.py:451-495`) and passed. |

## Alternate-entry and dead-code review

- `run --input-json` cannot bypass eligibility. It routes through
  `build_request()`, whose shared pure validation runs before engine argv
  construction (`src/delegate_agent/request_build.py:1253-1301`,
  `src/delegate_agent/request_build.py:1487-1493`). Empirical Codex and OpenCode
  input files both failed with `unsupported_pure_call` and exit 2.
- Dormant Codex/OpenCode pure implementation remains behind the new guards:
  Codex pure argv construction after the unconditional rejection
  (`src/delegate_agent/argv_builders.py:473-500`), OpenCode pure-specific request
  plumbing (`src/delegate_agent/request_build.py:1863-1915`), and the Codex
  Seatbelt/auth execution branch (`src/delegate_agent/runner.py:2069-2103`). It
  is unreachable through supported request paths, so it is not a current
  boundary bypass, but it is dead code that can drift and be re-enabled
  accidentally.

## Surviving surface audit

- README, CLI reference, per-engine help, top-level overview, parser errors,
  `run --input-json`, and `engineCapabilities.pureCall` all present Claude as the
  sole pure engine.
- **Known out-of-scope item:** `docs/security-model.md` still advertises Claude,
  OpenCode, and macOS Codex pure and describes the old hardlink path
  (`docs/security-model.md:103-141`). This was explicitly excluded from the fix
  scope; it should remain a tracked documentation follow-up.
- Historical changelog text describing Codex-only `outputSchema` at the time of
  its original release is historical, not a current capability claim.

## Verification

No live model calls were made.

- Tripwire parser probe: missing/null/object -> exit 1,
  `pure_boundary_unverified`; empty list -> exit 0; non-empty list -> exit 1,
  `pure_boundary_violation`.
- Sleeping fake-pipe timeout probe: `call_timeout` after 0.306 seconds on a
  0.25-second deadline.
- Real full-pipe timeout probe: still running after four seconds on a one-second
  deadline; externally killed.
- CLI and `run --input-json` eligibility probes: Codex and OpenCode both rejected
  with exit 2 / `unsupported_pure_call`; next action named only Claude.
- Help probes: all eight per-engine help surfaces and the top-level overview
  limit `--pure` to Claude; focused dry-run ambiguity noted above.
- `python3 -m compileall -q src tests bin`: passed.
- `env HOME=$(mktemp -d) python3 -m unittest discover -s tests`: 1,272 passed,
  7 skipped.
- `ruff check --no-cache .`: passed.
- `ruff format --check .`: passed (106 files already formatted).
- `git diff --check`: passed.
