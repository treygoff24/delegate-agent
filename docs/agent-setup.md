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
   command -v claude || echo "Claude Code CLI missing"
   command -v grok || echo "Grok Build CLI missing"
   command -v devin || echo "Devin CLI missing"
   command -v opencode || echo "OpenCode CLI missing"
   command -v pi || echo "Pi coding agent missing"
   command -v omp || echo "Oh My Pi missing"
   command -v kimi || echo "Kimi Code CLI missing"
   ```

   Delegate does not manage runtime login. Run each runtime's own login/status command before real launches. A missing child binary causes Delegate to fail with exit code `3` for real runs.

   The Claude harness requires Claude Code 2.1.x or newer (verified on 2.1.181) for `--effort`, `--permission-mode auto`, and `--no-session-persistence`.

4. Discover installed harnesses and initialize Delegate:

   ```bash
   delegate --json setup
   ```

   Setup checks all supported harnesses, records normalized model and reasoning
   metadata in the active auth profile's private cache, and reports which
   harnesses are launchable. If no config exists, it creates a minimal config
   containing safe absolute binary selectors. It never changes an existing
   config. Setup does not install or authenticate a harness, edit harness-native
   config, or submit a model prompt.

   Use `delegate config init` when you want the editable example config and
   profile overlays instead. Replace its placeholder Droid model IDs. Local
   alias names such as `reviewer` and `implementer` are yours and need not reveal
   the provider. For an existing install, run
   `env -u AI_PROFILE delegate config sync-profiles` to create missing overlays
   without overwriting existing ones.

   In a development checkout, `cp config.example.json ~/.delegate/config.json`
   is still fine. Installed users should prefer `delegate config init`.

### WSL setup

When running on Windows through WSL, treat Delegate as a Linux CLI:

- Install Python, Git, Delegate, and child CLIs inside the WSL distro.
- Keep repos under `/home/<user>/...` for best performance and private-file semantics.
- Use POSIX paths. Convert copied Windows paths with `wslpath -u` before using them in `--cwd`, `DELEGATE_CONFIG`, `CODEX_HOME`, or `worktrees.dataHome`.
- If `command -v git` points to Windows `git.exe`, install WSL-native Git (`sudo apt install git`) or put it earlier in `PATH`.

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

   `delegate --json capabilities` reports reasoning-effort support from config,
   the active profile's discovery cache, the legacy workspace cache, and bundled
   fallback data without launching a child runtime.

   Rediscover after a harness upgrade, account change, or new model release:

   ```bash
   delegate --json models --summary
   delegate --json models <engine> --live
   delegate --json capabilities refresh
   delegate --json capabilities
   ```

   `models <engine> --live` is a one-off view and never writes Delegate config or
   cache. `capabilities refresh` and `setup` update the selected profile's cache,
   preserving the last good record for a harness whose current probe fails.

6. Run a dry-run smoke test. Dry-run does not require the real child binary and does not launch the runtime:

   ```bash
   delegate --json dry-run codex safe "Review only. Do not edit files."
   delegate --json dry-run codex safe --reasoning-effort high "Review only. Do not edit files."
   delegate --json dry-run claude safe "Review only. Do not edit files."
   delegate --json dry-run claude safe --reasoning-effort high "Review only. Do not edit files."
   delegate --json dry-run grok safe "Review only. Do not edit files."
   delegate --json dry-run grok safe --reasoning-effort high "Review only. Do not edit files."
   delegate --json dry-run devin work "Describe the planned work invocation."
   delegate --json dry-run opencode safe "Review only. Do not edit files."
   delegate --json dry-run opencode safe --reasoning-effort high "Review only. Do not edit files."
   delegate --json dry-run pi safe --reasoning-effort high "Review only. Do not edit files."
   delegate --json dry-run omp safe --reasoning-effort high "Review only. Do not edit files."
   delegate --json dry-run cursor safe "Review only. Do not edit files."
   delegate --json dry-run droid reviewer safe "Review only. Do not edit files."
   delegate --json dry-run kimi safe "Review only. Do not edit files."
   ```

   The Codex, Claude, Grok, Devin work, OpenCode, Pi, Oh My Pi, Cursor, and Kimi dry-runs succeed with the unedited example config when no reasoning effort is requested. Devin safe mode is intentionally rejected because filesystem surveys may require generic `exec`, which cannot be allowed without weakening the read-only boundary. Explicit Codex reasoning-effort dry-runs can target the harness default model when no default model is configured. Claude and Grok reasoning effort map to native `--effort` flags; OpenCode maps to `--variant`; Pi and Oh My Pi map to `--thinking`. The Droid dry-run validates the alias, so it returns `unconfigured_model` until you replace the `reviewer` placeholder in `config.json` with a real model ID.

### OpenCode

Install OpenCode with the curl installer from [opencode.ai](https://opencode.ai).
The installer normally writes the binary to `~/.opencode/bin`, which is often
not on `PATH` in non-interactive shells. Add that directory to `PATH`, or set an
absolute path in Delegate config:

```json
{
  "opencode": {
    "binary": "/home/<user>/.opencode/bin/opencode"
  }
}
```

Run `opencode auth login` before the first real launch. Delegate uses OpenCode's
existing global authentication state and does not manage login itself.

### Pi

Install [Pi](https://github.com/badlogic/pi-mono) and authenticate its selected
provider. Delegate uses Pi's existing `~/.pi/agent/auth.json`; it never reads,
prints, or forwards that file and never emits Pi's `--api-key` flag. Configure
an absolute `pi.binary` when `pi` is not on the non-interactive `PATH`.

### Oh My Pi setup

Install Oh My Pi and authenticate its selected provider. Delegate uses the
runtime's existing profile without reading or forwarding credentials and never
emits `--api-key` or Oh My Pi's role flags. Configure an absolute `omp.binary`
when `omp` is not on the non-interactive `PATH`.

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

   For a persistent user installation, run `delegate --json setup` once before
   the first launch. Do not add setup to every CI invocation: it calls installed
   harness metadata commands and writes the selected profile's user cache.

4. Start with dry-run JSON:

   ```bash
   HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json dry-run codex safe "Review only."
   HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json capabilities
   ```

   Dry-runs do not create Delegate runs, do not create branches or worktrees, and do not require the real child binary. They still validate config shape and requested aliases.

5. For real child launches, check the child runtime explicitly before calling Delegate:

   ```bash
   command -v codex >/dev/null || exit 3
   command -v claude >/dev/null || exit 3
   command -v grok >/dev/null || exit 3
   command -v devin >/dev/null || exit 3
   command -v opencode >/dev/null || exit 3
   command -v pi >/dev/null || exit 3
   command -v omp >/dev/null || exit 3
   command -v kimi >/dev/null || exit 3
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

The required test suite does not need real Cursor, Droid, Codex, Claude, Grok,
Devin, OpenCode, Pi, Oh My Pi, or Kimi binaries. Tests use dry-run paths and fake binaries
where needed:

```bash
python3 -m compileall -q src tests bin
python3 -m unittest discover -s tests
```

Real runtime authentication is only required for integration smoke tests that intentionally launch a child agent.
