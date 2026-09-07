# Devin compatibility audit
Latest version: 3000.6.14 (2026-09-03, https://docs.devin.ai/cli/changelog/stable.md · https://static.devin.ai/cli/current/manifest.json) · Installed here: not installed
Delegate assumptions checked: 30 · BROKEN: 1 · LATENT: 7 · SIMPLIFY: 2 · OK: 20

The Devin CLI is a Rust binary distributed by `curl -fsSL https://cli.devin.ai/install.sh | bash` or
`brew install --cask devin-cli` — not npm and not PyPI (`@devin/cli`, `@cognition/devin`,
`@cognition-ai/devin`, `@devin-ai/cli` all return `{"error":"Not found"}` from the npm registry; the
npm names `devin`/`devin-cli` are unrelated squatted stubs). Delegate's `devin.binary` default of the
bare name `devin` is correct: the installer symlinks a `current` version into the user's data dir and
onto PATH.

Delegate's flag transport is, with one exception, exactly right for 3000.6.14. Almost every flag it
emits appears verbatim in the current Commands & Flags reference. The exception is a version pin that
has aged out and now hard-fails `devin call --read-only`.

## BROKEN

- **[B1] `delegate devin call --read-only` refuses to run on every Devin release since 3000.4.x.**
  delegate: `src/delegate_agent/cli.py:510` pins the read-only transport to
  `re.compile(r"(?<!\d)3000\.4\.\d+(?!\d)")` and `cli.py:542` raises
  `devin_read_only_transport_unverified` when `devin --version` does not match; harness: the current
  stable release is **3000.6.14** (2026-09-03), and the 3000.4.x series ended at 3000.4.25 on
  2026-08-13 (https://docs.devin.ai/cli/changelog/stable.md,
  https://static.devin.ai/cli/current/manifest.json, https://formulae.brew.sh/api/cask/devin-cli.json).
  Any operator on a current or auto-updated Devin (`auto_update` defaults to `true`,
  https://docs.devin.ai/cli/reference/configuration/config-file.md) loses read-only call entirely.

  Repro, no paid call and no binary needed:
  ```
  $ python3 -c "
  import sys; sys.path.insert(0,'src')
  from delegate_agent.cli import _DEVIN_READ_ONLY_TRANSPORT_VERSION as P
  for b in ['devin 3000.4.16 (123)','devin 3000.6.14 (18033302)','devin 3000.5.0']:
      print(repr(b), '->', 'ACCEPTED' if P.search(b) else 'REFUSED')"
  'devin 3000.4.16 (123)' -> ACCEPTED
  'devin 3000.6.14 (18033302)' -> REFUSED
  'devin 3000.5.0' -> REFUSED
  ```
  The argv the gate guards is still valid at 3000.6.14 — `--config`, `--sandbox`, and
  `--permission-mode autonomous` are all in the current reference
  (https://docs.devin.ai/cli/reference/commands.md), and `autonomous` is documented as "requires
  `--sandbox`", which delegate satisfies.

  Smallest fix: widen the pattern to the verified-good range rather than one minor series, e.g.
  `r"(?<!\d)3000\.(?:4|5|6)\.\d+(?!\d)"`, and re-run the gated
  `DELEGATE_DEVIN_BEHAVIOR_TEST` smoke (`tests/test_devin_read_only_behavior.py`) against 3000.6.14
  before widening. Do not remove the gate: the behavioral denial is not proven by argv shape, and
  L6 below is a live silent-ignore path in exactly this code path.

## LATENT

- **[L1] `"write"` is not a documented Devin tool name.** `src/delegate_agent/request_build.py:147`
  denies `["edit", "write", "exec", "Write(/**)", "mcp__*"]`. The permission reference lists
  **"Available tool names: `read`, `edit`, `grep`, `glob`, `exec`"** — `write` is absent
  (https://docs.devin.ai/cli/reference/permissions.md). The write boundary is still carried by the
  scope rule `Write(/**)`, so denial is not lost. The risk is the unknown: the docs never say whether
  an unrecognized rule string is ignored, logged, or rejects the config outright. If Devin ever hard-
  errors on unknown rules, every read-only call fails at startup. Cannot be settled without the binary.

- **[L2] Read-only call cannot start on a Linux host without `bwrap` and `socat`.** Delegate makes
  `--sandbox` mandatory for read-only (`argv_builders.py:443-455`). Devin documents: "**Linux**:
  requires bubblewrap (`bwrap`) and `socat`. A sandbox session fails to start with install
  instructions if either is missing — including in a fresh WSL distribution."
  (https://docs.devin.ai/cli/reference/commands.md). Delegate surfaces this only as a generic non-zero
  child result. A preflight `command -v bwrap socat`, or naming the dependency in the failure text,
  would turn a confusing child error into an actionable one.

- **[L3] The `devin models list --format json` schema delegate parses is undocumented.**
  `harness_discovery.py:745` requires a top-level `families` array and reads `slug`, `family_label`,
  `aliases`, `variants[].model_uid`, and `variants[].label`. The subcommand and its `--format json`
  are documented (https://docs.devin.ai/cli/reference/commands.md, "Output the model list as JSON
  (for scripts)"), but **no published document describes the JSON shape**. Every field name above is
  an undocumented dependency. Devin ships no schema guarantee for it, so a field rename degrades live
  discovery to version-only metadata (`harness_discovery.py:1538`) with no other signal. The fallback
  is graceful, which is why this is latent rather than broken.

- **[L4] Bundled fallback model IDs use a dotted form Devin's own docs do not.**
  `src/delegate_agent/bundled_models.py:26` lists `swe-1.5`, `swe-1.6`, `swe-1.6-fast`, `swe-1.7`,
  `swe-1.7-lightning`, and `config.example.json:90` sets `"defaultModel": "swe-1.7"`. Devin's
  configuration reference and Models page both use the **hyphenated** form in their examples:
  `"agent": { "model": "swe-1-6-fast" }` (https://docs.devin.ai/cli/models.md,
  https://docs.devin.ai/cli/reference/configuration/config-file.md). Devin matching is explicitly
  fuzzy — "Short names like `opus`, `sonnet`, `swe`, `codex`, and `gemini` always resolve to the
  latest version in that model family", and `--model` accepts "family slug, alias, or partial name" —
  so `swe-1.7` may well resolve. But there is no published ID list to check the bundled set against,
  and the bundled IDs only matter when live discovery is unavailable, which is exactly when a wrong
  ID has no fallback. Inferred, not confirmed: cannot verify without the binary or an account.

- **[L5] A truncated-but-useful Devin response is recorded as a failed run.** `runner.py:325`
  `status_from_exit` maps any non-zero exit to `STATUS_FAILED`. Devin's changelog, v3000.5.x:
  "Responses silently truncated when the model hits its max output token limit now show a warning and
  **exit non-zero in pipe mode** instead of returning partial output as if complete."
  (https://docs.devin.ai/cli/changelog/stable.md). Delegate's assistant-text recovery
  (`harness_events.py:907`) will still have the partial text, but the run envelope reports failure
  with no distinction between "truncated at the token limit" and "crashed". Devin publishes no
  exit-code table at all, so delegate is right not to switch on values; the gap is only that
  truncation is indistinguishable.

- **[L6] Devin silently ignores sandbox glob rules it cannot expand.** Changelog v3000.4.16
  (2026-08-10): "Linux sandbox startup no longer hangs while expanding filesystem globs; **unsupported
  glob rules are ignored and logged**, while trailing `/**` continues to cover a directory tree."
  (https://docs.devin.ai/cli/changelog/stable.md). Delegate's read-only document leans on exactly two
  glob rules, `Read(/**)` and `Write(/**)`. The trailing-`/**` form is the one the entry says still
  works, so today this is safe — but the mechanism means a future glob-handling change drops a deny
  rule into a log line rather than an error, and delegate would see a clean exit and a satisfied
  boundary. This is the strongest argument for keeping the B1 gate as a gate.

- **[L7] Devin sessions accumulate on disk after every delegate run.** `constants.py:87` sets
  `noSessionPersistence` for `codex`, `claude`, `pi`, `omp` only, so delegate emits no session
  suppression for Devin and never cleans up. Devin persists a session per turn — "sessions are saved
  once you send your first message" (changelog v3000.4.x) — and `devin list` enumerates "sessions in
  the current directory" (https://docs.devin.ai/cli/reference/commands.md). Devin offers no
  `--no-session` equivalent, so there is nothing delegate could pass; the cost is unbounded session
  growth in every workspace delegate drives, which is housekeeping rather than a correctness defect.

## SIMPLIFY

- **[S1] Devin has first-class session resume; delegate declines to use it.** `constants.py:88`
  leaves `nativeSessionResume` false for Devin, and `followup_command.py:281` rejects
  `delegate followup` on a Devin run (asserted at `tests/test_resume_parser.py:78`). Devin documents
  `-c, --continue` ("Resume the most recent session in the current directory") and
  `-r, --resume <SESSION_ID>` ("Resume a specific session by ID"), plus
  `devin list --format json` to enumerate session IDs
  (https://docs.devin.ai/cli/reference/commands.md). A Devin followup is therefore buildable today:
  run with the workspace as cwd, then `devin -r <id> --prompt-file <f> -p`. This adds a capability
  rather than deleting code, so it is an opportunity, not a deletion. The blocker is that `-p` emits
  no machine-readable session ID — it prints the resume hint as prose into the same stdout stream —
  so the ID has to come from a follow-on `devin list --format json` scoped to the run's cwd. Note
  `-r` with **no** argument opens an interactive picker and would hang in a non-TTY; always pass an
  explicit ID.

- **[S2] `--export` could replace the line-by-line assistant-text reconstruction.**
  `harness_events.py:496-501` routes every Devin stdout line through the text fallback, and
  `harness_events.py:907` `_record_devin_assistant_text` re-merges those lines into one chunk because
  Devin's plain-text stdout carries no message boundaries. Devin now offers
  `--export [PATH]` — "Export conversation to a file after each turn (ATIF format)"
  (https://docs.devin.ai/cli/reference/commands.md) — which would give a structured per-turn record
  instead of scraping. Estimated deletion if adopted: the Devin branches in `harness_events.py`
  (roughly 25 lines across the two sites above) plus the `_TEXT_STREAM_HARNESSES` special case in
  `stall_watchdog.py:86`. **Do not act on this yet**: the ATIF schema is not published in any Devin
  document, so adopting it trades a documented-stable plain-text path for an undocumented one. It is
  worth revisiting only if Cognition publishes the format.

## OK (verified)

Every item below was checked against https://docs.devin.ai/cli/reference/commands.md unless noted.

- `devin` binary name and PATH install — `config.py:131`, `config.example.json:89`; installer
  symlinks `current` (https://docs.devin.ai/cli).
- `-p` / `--print` as the headless transport — "`--print [PROMPT]` / `-p` — Print response and exit
  (non-interactive mode)". There is no `devin exec`, `devin run`, `--headless`, or
  `--non-interactive`.
- `--prompt-file <FILE>` — "Load the initial prompt from a file". `argv_builders.py:461`.
- `--prompt-file F -p` with `-p` emitted **last and bare** — `argv_builders.py:461`. Correct: `-p`
  takes an *optional* inline prompt, so a trailing bare `-p` cannot swallow a following token,
  and the file supplies the prompt.
- File-only prompt transport, stdin rejected — `argv_builders.py:459` raises
  `invalid_prompt_transport` for anything but `PROMPT_TRANSPORT_FILE`, and
  `constants.py` omits Devin from `promptStdin`. Devin documents no stdin prompt path.
- `--respect-workspace-trust false`, space-separated — `argv_builders.py:436`. Documented verbatim:
  "Pass `--respect-workspace-trust false` to skip the check in scripts and CI."
- `--model <MODEL>` — documented global flag, env var `DEVIN_MODEL`.
- `--permission-mode dangerous` for work and default call — `argv_builders.py:454`. Documented mode
  value: "`dangerous` (aliases `yolo`, `bypass`)". Delegate's stated reason (print mode rejects
  unapproved edit/exec tools) matches the permission table at
  https://docs.devin.ai/cli/reference/permissions.md.
- `--config <PATH>` for the read-only permission document — documented global flag,
  "Configuration file path".
- `--sandbox` plus `--permission-mode autonomous` — documented, and `autonomous` is documented as
  "requires `--sandbox`", which delegate always pairs.
- Read-only document shape `{"permissions": {"allow": [...], "deny": [...]}}` —
  `request_build.py:147`. Matches the documented `permissions` section with `allow`/`deny`/`ask`
  (https://docs.devin.ai/cli/reference/configuration/config-file.md).
- Tool-name rules `read`, `grep`, `glob`, `edit`, `exec` — all five are in the documented
  "Available tool names" list (https://docs.devin.ai/cli/reference/permissions.md).
- Absolute-prefix globs `Read(/**)` and `Write(/**)` — documented, with an explicit note that the
  leading `/` is required: "A bare `Read(**)` without a leading `/` is resolved relative to your
  current working directory". Delegate gets this right.
- `mcp__*` wildcard deny — documented as "All MCP tools everywhere".
- Deny beats allow — documented precedence is deny → ask → allow, "a deny rule always wins", so
  delegate's deny list is not defeated by its own `Read(/**)` allow.
- Defense in depth on read-only writes — in Autonomous mode "Direct file edits via the `edit` and
  `write` tools still prompt", and in print mode a confirmation-required tool call is rejected rather
  than queued. Write denial therefore holds even before the deny rules are consulted.
- `devin models list --format json` as the prompt-free catalog — `harness_discovery.py:1387`.
  Documented subcommand: "`devin models list --format json` — Output the model list as JSON (for
  scripts)". It spends no prompt, matching `docs/security-model.md:432`.
- `devin --version` banner parsing — `harness_discovery.py:92`
  `re.compile(r"^devin\s+[0-9][0-9A-Za-z.+-]*", ...)`. `devin version` is documented as "equivalent
  to `devin --version`", and the banner is `devin <version> (<build>)`, which the pattern matches.
- No working-directory flag; delegate sets the child cwd instead — `runner.py:2053-2055` passes
  `cwd=` to `Popen`. Correct: Devin has no `--cwd`/`-C`/`--dir`, and its `[PATH]` positional opens
  Devin Desktop rather than setting a working directory, so passing a path there would be a bug.
- Reasoning effort unsupported — `reasoning.py:121`, rejected at `request_build.py:3063`. Verified:
  ```
  $ python3 bin/delegate.py --json dry-run devin work --reasoning-effort high "t"
  unsupported_reasoning_effort | reasoning effort is not supported (harness devin) ...
  ```
  Devin exposes thinking levels only as an in-session `Alt+T` toggle, with no CLI flag
  (https://docs.devin.ai/cli/models.md).
- No native structured output — `constants.py:86`. Verified:
  ```
  $ python3 bin/delegate.py --json dry-run devin call --output-schema /dev/null "t"
  "error": "unsupported_output_schema", "message": "--output-schema/outputSchema is only supported by
  codex and claude; devin has no native schema enforcement."
  ```
  Correct: Devin has no `--output-format`, no `--json` for agent turns, and no response-shape control.
- Plain-text stdout, never parsed as stream-json — `harness_events.py:496-501`. Correct and load-
  bearing: Devin emits no JSON event stream, so a JSON-looking line inside a code block would
  otherwise be silently routed away from the recovered assistant text.
- Exit status treated as binary — `runner.py:325`. Correct given that Devin publishes no exit-code
  table; see L5 for the one documented nuance.
- Safe mode rejected with `unsupported_mode` — `argv_builders.py:427`, `constants.py:37`. Verified by
  dry-run. This is delegate's own security policy (`docs/security-model.md:119`), not a Devin
  compatibility fact, and remains defensible: Normal mode auto-approves read-only tools but Devin
  offers no argv-level way to bound the generic `exec` tool without the sandbox.

Dry-run argv, both supported modes, no paid call:
```
$ python3 bin/delegate.py --json dry-run devin work --model swe-1.7 "test prompt"
devin --respect-workspace-trust false --model swe-1.7 --permission-mode dangerous --prompt-file <prompt file> -p

$ python3 bin/delegate.py --json dry-run devin call --read-only "test"
devin --respect-workspace-trust false --config <devin agent config> --sandbox --permission-mode autonomous --prompt-file <prompt file> -p
```

## Could not verify

The binary is not installed on this machine, so nothing below could be executed. All of it is
runtime behavior that docs do not settle.

- The exact `devin --version` / `devin version` output string. The docs say the two are equivalent
  but never print the format. The banner shape `devin 3000.6.14 (18033302)` comes from the man page
  inside the official 3000.6.14 release tarball, extracted by a research subagent — second-hand
  relative to this audit, though consistent with delegate's existing test fixture
  (`tests/test_harness_discovery.py:734`, `print('devin 3000.3.27')`).
- The JSON schema of `devin models list --format json` (L3). Needs one run against an authenticated
  account.
- Whether Devin accepts, ignores, or rejects the unknown permission rule string `"write"` (L1).
- Whether `--config <PATH>` **replaces** the user config or merges with project `.devin/config.json`
  and `.devin/config.local.json`. The documented precedence list covers the three standard config
  locations but never says where a `--config` file lands in that order. This matters: if `--config`
  merges *below* a project config, a workspace's own `.devin/config.json` allow rules would outrank
  delegate's read-only document. Delegate's read-only boundary depends on the answer.
- Devin's actual process exit codes. No table exists in the docs, the changelog, or the shipped man
  pages. The only documented exit-code table on docs.devin.ai governs **hook scripts**
  (0 = success, 2 = block), not the CLI process — do not conflate them.
- Whether the bundled dotted model IDs (`swe-1.7`) resolve under Devin's fuzzy matcher (L4).
- Whether the read-only deny rules actually block write and exec at 3000.6.14. This is precisely what
  `tests/test_devin_read_only_behavior.py` exists to answer, and it needs one live Devin call
  (`DELEGATE_DEVIN_BEHAVIOR_TEST=1`). Argv shape alone does not prove it, and L6 shows a documented
  silent-ignore path in the same mechanism.

## Sources

- https://docs.devin.ai/cli/reference/commands.md — global flags (`--model`, `--permission-mode`,
  `--sandbox`, `-c/--continue`, `-r/--resume`, `-p/--print`, `--prompt-file`, `--config`, `--export`,
  `--respect-workspace-trust`), the workspace-trust CI note, `devin models list --format json`,
  `devin list --format json`, `devin version`, Linux sandbox `bwrap`+`socat` requirement, absence of
  any `--cwd`/`--output-format`.
- https://docs.devin.ai/cli/reference/permissions.md — permission-mode table, `autonomous` behavior,
  scope-based rules (`Read`/`Write`/`Exec`/`Fetch`), the "Available tool names" list, `mcp__*`
  patterns, the leading-`/` glob note, and the deny → ask → allow precedence.
- https://docs.devin.ai/cli/reference/configuration/config-file.md — config file locations,
  `permissions` section shape, `auto_update` default, `agent.model` example (`swe-1-6-fast`).
- https://docs.devin.ai/cli/models.md — fuzzy model matching, short-name resolution, thinking levels
  as an in-session toggle only, absence of a published model-ID list.
- https://docs.devin.ai/cli/essential-commands.md — `-p` usage forms, the five permission modes.
- https://docs.devin.ai/cli/changelog/stable.md — v3000.6.14 dated September 3 2026; the
  non-zero-exit-on-truncation change; the v3000.4.16 "unsupported glob rules are ignored and logged"
  entry; session-persistence behavior.
- https://static.devin.ai/cli/current/manifest.json — live release manifest, `"version":"3000.6.14"`,
  platform tarballs and sha256s.
- https://formulae.brew.sh/api/cask/devin-cli.json — independent confirmation of 3000.6.14.
- https://docs.devin.ai/cli — install commands (`curl … cli.devin.ai/install.sh`, Homebrew cask,
  PowerShell).
- npm registry (`https://registry.npmjs.org/<pkg>`) — established that no official npm package
  exists for the Devin CLI.
