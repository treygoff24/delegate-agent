# Harness compatibility audit brief (2026-09-07)

You are auditing `delegate-agent` (this repo, version 0.30.0, branch `feat/harness-compat-audit`)
against the **latest released version** of ONE harness CLI assigned to you. Your output is a single
Markdown report at the path you were given. Do not edit any other file. Do not run `delegate` against
live paid models unless the invocation is a dry-run (`python3 bin/delegate.py --json dry-run ...`) or
`--help`/`--version`/model-listing on the installed binary.

## What "audit" means here

1. **Learn how delegate drives your harness.** Read, at minimum: `src/delegate_agent/argv_builders.py`,
   `harness_discovery.py`, `harness_events.py`, `model_discovery.py`, `prompt_transport.py`,
   `structured_output.py`, `stream_capture.py`, `reasoning.py`, `bundled_models.py`, `constants.py`,
   `request_build.py`, `runner.py` (grep for your harness name), `workflows/runtime.py`
   (`_native_schema` and engine-specific branches), `config.example.json`, `docs/cli-reference.md`,
   `docs/configuration.md`, `docs/agent-setup.md`, and the tests/fixtures that mention your harness
   (`rg -l <name> tests/`). Record every flag, subcommand, env var, output format, event type, exit
   code convention, model id, reasoning-effort value, and install path that delegate assumes.
2. **Establish the harness's current truth.** Find the latest released version and its official CLI
   documentation and changelog/release notes (primary sources only: vendor docs, the vendor's GitHub
   repo, release pages). Tools: `exa-agent` for search, `firecrawl` for bot-walled or JS docs and
   GitHub release pages, `WebFetch` for plain pages. If the binary is installed locally, run
   `<bin> --version` and `<bin> --help` (and subcommand help) and treat that as the ground truth for
   the installed version; still check whether a newer release exists. Cite URLs.
3. **Diff assumption vs truth.** For every assumption in (1), state whether it is still correct at the
   latest version. Classify each mismatch:
   - **BROKEN** — delegate will fail or misbehave today (removed/renamed flag, changed output
     format, changed exit codes, changed model ids, API constraints like the Claude `--json-schema`
     object-root rule in `docs/issues/2026-09-07-claude-json-schema-non-object.md`).
   - **LATENT** — works now but relies on deprecated/undocumented behavior, or breaks on a documented
     edge (e.g. a schema shape, a long prompt, a missing env var, a non-zero exit on partial success).
   - **SIMPLIFY** — the harness now offers a first-class feature (stdin prompt transport, prompt file,
     native structured output, JSON event stream, model listing) that would let delegate delete or
     collapse code it currently carries for this harness.
   - **OK** — verified correct; list these too, briefly, so the synthesizer knows what was checked.
4. **Reproduce where you can.** For BROKEN/LATENT items, give a reproduction that needs no paid model
   call when possible (dry-run argv, `--help` output, a unit-level Python snippet against the module).
   Say plainly what you could not verify (binary not installed, docs paywalled, needs a live call).

## Report format (keep it tight; the synthesizer reads eleven of these)

```
# <Harness> compatibility audit
Latest version: <x.y.z> (<date>, <url>) · Installed here: <version or "not installed">
Delegate assumptions checked: <n> · BROKEN: <n> · LATENT: <n> · SIMPLIFY: <n> · OK: <n>

## BROKEN
- [B1] <one-line defect> — delegate: `<file>:<line>` assumes <x>; harness: <truth> (<url>). Repro: <...>. Smallest fix: <...>.
## LATENT
- [L1] ...
## SIMPLIFY
- [S1] <what delegate carries> → <what the harness now provides>; estimated deletion: <files/lines>.
## OK (verified)
- <flag/behavior> — <where verified>
## Could not verify
- ...
## Sources
- <url> — <what it established>
```

Rules: every claim about the harness cites a URL or a local command output. Quote flags verbatim.
No speculation dressed as fact — if you are inferring, say "inferred". No code changes, no tests
edited, no new files beyond your report. A report that says "checked N things, found nothing broken"
is a good report if it is true.
