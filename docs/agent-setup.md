# Agent setup guide

This guide covers both human setup and non-interactive setup for agents or CI jobs that need to call Delegate.

## Human setup

1. Install Delegate from the source repository:

   ```bash
   python3 -m pip install "delegate-agent @ git+https://github.com/treygoff24/delegate-agent.git"
   ```

   For a development checkout, prefer:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install -e .
   python3 bin/delegate.py --json describe
   ```

2. Confirm Delegate is on `PATH` if you installed it:

   ```bash
   command -v delegate
   delegate --json describe
   ```

   In this repository's development checkout, use `python3 bin/delegate.py` to avoid accidentally calling an older installed shim.

3. Install and authenticate the child runtimes you plan to use:

   ```bash
   command -v agent || echo "Cursor Agent CLI missing"
   command -v droid || echo "Factory Droid CLI missing"
   command -v codex || echo "Codex CLI missing"
   ```

   Delegate does not manage runtime login. Run each runtime's own login/status command before real launches. A missing child binary causes Delegate to fail with exit code `3` for real runs.

4. Copy and edit config:

   ```bash
   mkdir -p ~/.delegate
   cp config.example.json ~/.delegate/config.json
   $EDITOR ~/.delegate/config.json
   ```

   Replace placeholder Droid model IDs. Use local aliases such as `reviewer` and `implementer`; the alias names are yours and do not need to reveal the provider.

5. Inspect loaded config and aliases:

   ```bash
   delegate --json describe
   delegate --json models
   delegate --json capabilities
   ```

   `delegate --json describe` also returns a `commands` catalog (each entry has `command` and `summary`), so one call lists the entire command surface. To learn how to invoke any specific command, introspect it with `delegate --json <command> --help`, which returns a structured spec of its usage, arguments, options, and examples:

   ```bash
   delegate --json cursor --help
   delegate --json worktree remove --help
   ```

   `delegate --json capabilities` reports reasoning-effort support from config, workspace cache, and bundled fallback data without launching a child runtime. Run `delegate --json capabilities refresh` only when you explicitly want Delegate to call child CLIs and update the workspace-local `.delegate/capabilities/reasoning.json` cache.

6. Run a dry-run smoke test. Dry-run does not require the real child binary and does not launch the runtime:

   ```bash
   delegate --json dry-run codex safe "Review only. Do not edit files."
   delegate --json dry-run codex safe --reasoning-effort high "Review only. Do not edit files."
   delegate --json dry-run cursor safe "Review only. Do not edit files."
   delegate --json dry-run droid reviewer safe "Review only. Do not edit files."
   ```

   The Codex and Cursor dry-runs succeed with the unedited example config when no reasoning effort is requested. The Codex reasoning-effort dry-run requires a resolved Codex model from config or JSON input. The Droid dry-run validates the alias, so it returns `unconfigured_model` until you replace the `reviewer` placeholder in `config.json` with a real model ID.

## Non-interactive agent setup

For an orchestrating agent, script, or CI job:

1. Use a known Python interpreter, preferably Python 3.11 or newer.
2. Decide whether the process should use an installed `delegate` or a checkout-local entrypoint. In a repository checkout, prefer:

   ```bash
   python3 bin/delegate.py --json describe
   ```

3. Use `DELEGATE_CONFIG` for an explicit config overlay. If you need a clean,
   deterministic config with no user-level aliases, run the process with a
   temporary `HOME` as well:

   ```bash
   clean_home="$(mktemp -d)"
   HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json describe
   HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json models
   ```

   `DELEGATE_CONFIG` has highest precedence, but config objects are deep-merged;
   a temporary `HOME` avoids inheriting nested maps such as user-level Droid
   aliases.

4. Start with dry-run JSON:

   ```bash
   HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json dry-run codex safe "Review only."
   HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json capabilities
   ```

   Dry-runs do not create Delegate runs, do not create branches or worktrees, and do not require the real child binary. They still validate config shape and requested aliases.

5. For real child launches, check the child runtime explicitly before calling Delegate:

   ```bash
   command -v codex >/dev/null || exit 3
   ```

6. Keep prompts bounded and machine-readable where possible. For long tasks, use `--prompt-file` or `delegate --json run --input-json FILE`.

7. Inspect tracked runs with Delegate commands instead of tailing raw logs:

   ```bash
   delegate snapshot <alias-or-runId>
   delegate run-output <alias-or-runId> --completion-report
   delegate runs --active
   ```

   Agent loop: save the returned `alias` or `runId`, poll `delegate snapshot`
   until the run leaves `running`. A `stale` status means the child process is
   gone and will not resume, so stop polling and inspect the snapshot. Once the
   run is finished, read
   `delegate run-output <alias-or-runId> --completion-report`. If the original
   report is missing, Delegate may synthesize one from completed stdout events,
   but that recovery is bounded and best-effort. If no completion report is
   available, inspect bounded tails with `--stdout --tail N` and
   `--stderr --tail N`; raw `.delegate/` files are a last resort.

## CI expectations

The required test suite does not need real Cursor, Droid, or Codex binaries. Tests use dry-run paths and fake binaries where needed:

```bash
python3 -m compileall -q src tests bin
python3 -m unittest discover -s tests
```

Real runtime authentication is only required for integration smoke tests that intentionally launch a child agent.
