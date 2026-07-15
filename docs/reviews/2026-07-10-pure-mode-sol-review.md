# Review: `call --pure`

## Executive summary

Do not ship `--pure` as a documented, supported feature in its current form.

The product idea is coherent. Delegate already turns several coding harnesses into bounded execution lanes, and a stateless completion lane for hostile input is a natural extension of `call`. Fail-closed eligibility is also the right design: Delegate does not need parity across every engine before it can support one engine honestly.

The current implementation does not yet earn the uniform `pure` claim. The Codex adapter makes its copied `auth.json` readable to the whole Seatbelt-confined process tree, including any model-driven tool subprocess, and it may hardlink that file to the live credential. The Claude permission-denial tripwire succeeds when its evidence field is missing or malformed. The new timeout can block indefinitely while writing stdin. OpenCode is advertised as pure even though its native `--pure` flag only disables external plugins, sessions still accumulate in global state, and it has neither schema output nor a boundary tripwire.

For Probita dogfood, keep the integration on Claude only and treat it as experimental. Fix the Claude tripwire and timeout before relying on it as a daemon trust boundary. Disable Codex and OpenCode eligibility rather than papering over their gaps. A public feature can follow once each eligible adapter has a written threat model, adversarial boundary tests, explicit upstream compatibility checks, and discovery output that states the actual boundary instead of implying cross-engine equivalence.

## Findings

### Critical: Codex exposes the authentication credential inside the boundary

`execute_call()` resolves the real Codex credential, creates an ephemeral `CODEX_HOME`, places `auth.json` there, and passes that directory as an explicit Seatbelt read exception (`src/delegate_agent/runner.py:2025-2047`). The profile then allows `file-read-data` for the entire ephemeral directory (`src/delegate_agent/seatbelt.py:143-168`). Seatbelt applies to Codex and all descendants. It cannot distinguish a read performed by the Codex parent from a read performed by a model-driven shell command.

The path is discoverable even if Codex's inner shell environment omits `CODEX_HOME`: the call cwd and ephemeral Codex home are sibling `tempfile` directories under the same readable `$TMPDIR`, while the profile leaves `/var/folders/...` readable by default. Codex's inner `read-only` sandbox prevents writes; it does not prohibit reads.

A local probe with a dummy credential and the generated profile confirmed that `/usr/bin/sandbox-exec ... /bin/cat <ephemeral-CODEX_HOME>/auth.json` exits 0 and returns the dummy token. No model call was used. This defeats the stated purpose of preventing hostile prompt content from reading local credentials and exfiltrating them through the model channel.

The same credential is not necessarily an independent copy. `_copy_or_link_auth()` prefers `os.link()` (`src/delegate_agent/runner.py:1979-1985`). A second dummy-file probe confirmed that the source and ephemeral path share an inode and that writing through the ephemeral path changes the source. The profile begins with `(allow default)` and adds no file-write deny (`src/delegate_agent/seatbelt.py:159-168`), so a Codex runtime credential refresh or other parent-process write can mutate the live credential despite the ephemeral-home claim.

The immediate fix is to remove Codex from `pureCall`. Replacing the hardlink with `shutil.copy2()` is necessary for credential integrity, but it does not fix confidentiality. A correct Codex design needs a credential transport that the Codex parent can use but model tools cannot read or discover. A single inherited Seatbelt profile cannot provide that process-level distinction. The boundary should not be re-enabled until an adversarial test asks Codex to locate and return the ephemeral credential itself.

### High: Claude's tripwire fails open when its evidence disappears

The parser treats a non-empty `permission_denials` list as a violation, but every other shape, including a missing field, `null`, or an object, falls through to success (`src/delegate_agent/runner.py:1935-1955`). A direct parser probe returned exit 0 and no error for both a missing field and an object-valued field.

That contradicts the stated fail-closed tripwire and Delegate's honest-envelope rule. An upstream Claude Code output change could silently turn `pureTripwire: true` into an unverified success. The tests reinforce the gap: the no-usage fixture omits `permission_denials` under `pure=True` and does not assert failure (`tests/test_pure_call.py:231-238`).

Require the field to be present and exactly a list for pure calls. Missing or malformed evidence should return a stable failure such as `pure_boundary_unverified`. Keep the non-empty-list case as `pure_boundary_violation`. This is also a place for a tested minimum or known-compatible Claude Code version because the boundary depends on upstream output shape.

### High: `--timeout` does not cover a blocking stdin write

`_bounded_call_communicate()` writes and flushes all stdin synchronously before starting its timeout clock (`src/delegate_agent/runner.py:1831-1845`). If the child stops reading, a large hostile-content prompt can fill the pipe and block the parent forever. Neither the timeout loop nor the overflow kill path can run while that write is blocked.

A fake-pipe probe made `stdin.write()` sleep for 1.2 seconds and passed a one-second timeout. The helper returned successfully after 1.2 seconds, proving that time spent writing stdin is outside the deadline. A real full pipe can block without a bound.

Start one deadline immediately after `Popen`. Feed stdin concurrently, or use a selector-based communicate loop, and apply the same deadline to stdin, stdout, stderr, and process exit. On expiry, close stdin, terminate the process group, and join every drain/writer thread.

### High: OpenCode does not meet the feature's stated contract

OpenCode is included in `pureCall` and `promptStdin`, but its own capability row says `structuredOutput: false`, `noSessionPersistence: false`, and `pureTripwire: false` (`src/delegate_agent/constants.py:27-48`). Its adapter maps Delegate pure to OpenCode's native `--pure` plus deny-all permissions (`src/delegate_agent/argv_builders.py:426-453`, `src/delegate_agent/request_build.py:1863-1915`). The installed OpenCode help defines `--pure` only as "run without external plugins" and exposes no ephemeral/no-persistence option. Delegate's existing `describe` notes also admit that OpenCode sessions accumulate in global state (`src/delegate_agent/describe_payload.py:1088-1102`).

This may be a useful no-tool OpenCode call, but it is not the promised hostile-content, schema-bound, non-persistent completion boundary. Its inclusion also conflicts with the task's intended fail-closed rule that engines without the boundary reject `--pure`.

Delete OpenCode from pure eligibility and retain its existing `safe` and `call --read-only` behavior. Reconsider it only if OpenCode gains verifiable non-persistence, structured output, and a stable denial/result signal, or if Delegate adds an external boundary that supplies those properties.

### Medium: the ordinary unit suite can make seven real Codex calls

The live test gate checks only for macOS, `sandbox-exec`, a Codex binary, an auth file, and a successful `codex login status` (`tests/test_codex_pure_sandbox.py:22-37`). On the intended developer machine, those conditions are routine. The seven tests at `tests/test_codex_pure_sandbox.py:384-501` then make real network model calls during `python3 -m unittest discover -s tests`.

That makes the documented local unit gate costly, slow, network-dependent, and unsafe for a review whose explicit rule is not to call a model. Put live tests behind an affirmative environment flag such as `DELEGATE_RUN_LIVE_CODEX_PURE_TESTS=1` and expose a separate integration-test command. Authentication should be necessary but not sufficient consent.

### Medium: help, docs, and capability discovery disagree about the public contract

Several machine-facing and human-facing surfaces now tell different stories:

1. README says pure is available on Claude and OpenCode, omitting the newly added Codex adapter, then says the feature has no session persistence even though OpenCode's capability is false (`README.md:194-200`).
2. The JSON-input reference still says `outputSchema` is Codex-only (`docs/cli-reference.md:549-560`), while request validation accepts it for Claude call mode (`src/delegate_agent/request_build.py:320-360`).
3. `describe` reports legacy `outputSchema: false` and new `structuredOutput: true` for Claude because the two keys are derived separately (`src/delegate_agent/describe_payload.py:595-603`). Consumers cannot know which capability is authoritative.
4. `_call_help_spec()` injects `--pure` into every engine's help (`src/delegate_agent/describe_payload.py:132-171`). For example, `delegate cursor --help` advertises `cursor call --pure`, while the parser rejects it with `unsupported_pure_call`.
5. Pure dry-run payloads do not include `pure`, `timeout`, `structuredOutput`, or the planned Seatbelt boundary (`src/delegate_agent/cli.py:296-308`). A Codex pure dry-run shows the inner Codex argv only, not the outer `sandbox-exec` wrapper that defines the security claim.

Use one capability source for parser eligibility, focused help, docs generation, and `describe`. Pick either `outputSchema` or `structuredOutput` rather than exposing contradictory booleans. Add explicit dry-run fields such as `pure`, `timeoutSeconds`, and `boundaryKind`; for Codex, show the planned outer wrapper without materializing credentials.

### Medium: the Seatbelt runtime allowlist ignores configured Codex binaries

Delegate launches `codex.binary` (`src/delegate_agent/argv_builders.py:473-476`), but the Seatbelt profile always resolves the literal names `codex` and `node` from `PATH` (`src/delegate_agent/seatbelt.py:79-117`, `src/delegate_agent/seatbelt.py:136-141`). A configured absolute wrapper or renamed binary under the denied home tree can pass `ensure_binary()` and then fail at launch because the profile allowed a different executable, or none.

Pass the resolved `launch_argv[0]` into the profile builder. Do not perform a second, name-based binary resolution inside the security boundary code.

### Low: stderr overflow is reported as stdout overflow

The stderr drain has its own message (`src/delegate_agent/runner.py:1818-1825`), but either stream crossing its cap raises the error code `call_stdout_overflow` (`src/delegate_agent/runner.py:1863-1868`). That makes the JSON envelope materially less diagnostic. Use `call_output_overflow` with a stream field, or distinct stdout/stderr error codes.

## Design assessment in product context

### The capability belongs in Delegate, with a narrower promise

Delegate is already the layer that knows which harness flags, prompt transports, output parsers, and isolation mechanisms are trustworthy enough for a mode. A hostile-input completion profile therefore fits better here than in Probita. Extracting it into Probita would duplicate harness knowledge and make the boundary harder to maintain.

The mistake is treating `pure` as one cross-engine property. It is a requested contract backed by different mechanisms:

- Claude can currently approach the contract through upstream `--safe-mode`, an empty tool set, strict MCP configuration, no session persistence, stdin transport, and a result tripwire. Local Claude Code 2.1.206 help confirms those flags and says safe mode disables CLAUDE.md, skills, plugins, hooks, MCP servers, commands, agents, and other customizations. Admin-managed policy remains in scope and should be named in the threat model.
- Codex needs external confinement because Codex remains a tool-using coding agent. The Seatbelt direction is reasonable for local dogfood, but the credential must be outside the model-tool read boundary. `sandbox-exec` is deprecated on macOS, so it is a maintenance risk rather than a durable cross-platform foundation.
- OpenCode's plugin-only `--pure` flag is not equivalent. Denying tools is useful defense in depth, but persistence and verification remain missing.

Fail-closed eligibility is durable. The correct public matrix may contain only Claude for a while. Platform-specific Codex support can be honest later if `describe` reports it dynamically and CI exercises the actual supported macOS versions. Delegate should not manufacture weak versions for Cursor, Droid, Grok, Devin, or Kimi merely to make the table look complete.

### The threat model needs to be explicit

The security model should identify assets and trust assumptions instead of describing flags alone. At minimum:

- Protected assets: ambient environment secrets, home-file contents and metadata, harness credentials, prompt confidentiality at rest, and credential integrity.
- Trusted components: Delegate's installed code and user config, the selected harness binary, the OS sandbox implementation, and the model provider. Repository content and prompt content are untrusted.
- Allowed effects: provider network traffic required for inference and bounded stdout/stderr returned to the caller.
- Out of scope: provider-side misuse, malicious replacement of the configured harness binary, admin-managed policy outside Delegate's control, and files outside any documented path boundary.
- Per-engine proof: exact flags, environment policy, persistence behavior, credential path, tool visibility, denial evidence, supported harness versions, and what causes a fail-closed refusal.

The current security-model sentence that the Codex boundary prevents reading "other local files" is too broad (`docs/security-model.md:112-135`). The profile is a deny list for one home tree, `/Users/Shared`, `/tmp`, and `/private/tmp`; it leaves other system paths, mounts, and `/var/folders` readable. The document notes some of this later, but the earlier claim should name the protected prefixes precisely.

### Schema and security should remain separate capabilities

The implementation allows `--pure` without `--output-schema`, despite the commit title and product framing calling it schema-bound. That separation is defensible: hostile-input isolation and response shape solve different problems. Public wording should call pure a hostile-input boundary that is schema-capable on eligible engines, not schema-bound. If Probita specifically requires both, its caller should require `pureCall && structuredOutput` and always provide a schema.

The completion envelope should report both facts independently and state which boundary ran. A successful process exit is not proof of a verified boundary unless every required evidence field was present and valid.

## Recommendation for shipping

### Now

Keep the feature experimental and unsupported. Do not publish the current cross-engine guarantee.

For local Probita dogfood, use Claude only. Fix the fail-open tripwire and stdin timeout first. Continue to prohibit live model calls in normal tests. Remove or hide OpenCode pure, and disable Codex pure until its credential is inaccessible to model tools.

### Before public support

Shipping properly requires:

1. A threat-model section with protected assets, trusted components, allowed effects, and explicit exclusions.
2. Per-engine eligibility derived from one capability registry, with no help or docs advertising rejected combinations.
3. Fail-closed parsing for every security-relevant upstream result field and an explicit compatibility policy for Claude Code and Codex CLI versions.
4. A credential-safe Codex architecture, plus adversarial tests for the real ephemeral auth path, home files, metadata, temp siblings, mounted paths, environment leakage, schema exfiltration, and writes through credential aliases.
5. macOS CI for the Seatbelt adapter and Linux CI that proves Codex pure is rejected. Any live provider tests must be explicit opt-in jobs with scoped credentials.
6. Honest dry-run and completion envelopes that report the requested contract, actual boundary kind, platform eligibility, structured-output status, and whether any tripwire was verified.
7. Documentation generated or checked against the same capability source, including the platform and upstream-maintenance burden.

I would then ship a narrower feature: Claude first, Codex only after the credential problem is solved, and no promise that all Delegate engines will ever qualify.

## What to delete or simplify

1. Delete OpenCode pure eligibility and its hostile-content claims. Existing OpenCode safe/read-only paths already cover its honest capability.
2. Delete the auth hardlink optimization. `auth.json` is tiny; an unconditional private copy is simpler and avoids inode aliasing.
3. Delete one of the duplicate `outputSchema`/`structuredOutput` capability keys, or preserve the old key only as a documented alias with identical values.
4. Stop rewriting every command's help with unconditional string replacement. Filter the existing option spec through the existing engine capability map.
5. Move live Codex boundary tests out of the default unit-discovery path.

## Verification

- Reviewed `git diff 49f36fd~1..3e42f87` and the surrounding call, profile, argv, parser, execution, capability, help, and documentation paths.
- Read README, CLI reference, security model, repository standards, and `delegate agent-help` output.
- Confirmed installed help contracts without launching models: Claude Code 2.1.206, Codex CLI 0.144.1, OpenCode, and macOS `sandbox-exec`.
- Ran 211 targeted parser/help/pure/Seatbelt unit tests: passed.
- Ran the full suite under an isolated empty `HOME` so the seven live Codex tests were skipped: 1,268 tests passed, 7 skipped.
- Ran `python3 -m compileall -q src tests bin`, `git diff --check 49f36fd~1..3e42f87`, `ruff check --no-cache .`, and `ruff format --check .`: passed.
- Ran local dummy-data probes for Seatbelt credential readability, hardlink aliasing, timeout accounting, and Claude tripwire schema drift. No live network model calls were made.
