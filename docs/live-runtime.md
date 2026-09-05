# Development and installed runtime separation

A development checkout and an installed `delegate` command can coexist. They may not have the same code or config.

## Development checkout

When working inside this repository, use the repo-local entrypoint:

```bash
python3 bin/delegate.py --json describe
python3 bin/delegate.py --json dry-run codex safe "Review only."
```

This avoids accidentally calling an older installed shim from `PATH`.

## Installed runtime

When Delegate is installed, `delegate` resolves through your shell `PATH`:

```bash
command -v delegate
delegate --json describe
```

The installed command may use user config from `~/.delegate/config.json` unless `DELEGATE_CONFIG` points elsewhere. `describe` reports the active `configSource`.

When `AI_PROFILE=work|personal` is set and the matching
`~/.delegate/config.<profile>.json` overlay is missing, Delegate blocks launch
and mutation commands but allows read-only diagnostics (`profiles`, `runs`,
`run-output`, `snapshot`, cached `capabilities`, `worktree show`,
`worktree list`, `workflow check|status|watch|events|result|wait|list`,
`describe`, `models`, `doctor`) with a warning. This check runs inside
`delegate_agent.cli:main` (`src/delegate_agent/profile_guard.py`), so it applies
regardless of entrypoint: the installed pip console script, `python -m
delegate_agent.cli`, or `bin/delegate.py`. Some local installs additionally put
a profile-aware shell shim in front of the Python entrypoint -- the tracked
source for that shim is `bin/delegate-profile-shim` -- which applies the same
check even earlier, before Python starts. `AI_PROFILE` values other than
`work`/`personal` are not recognized profiles; Delegate warns and runs on the
base account rather than failing closed, since there is no config filename
convention to check against. Fix a half-configured install with
`env -u AI_PROFILE delegate config sync-profiles`, or bypass profile selection
once with `env -u AI_PROFILE delegate ...`.
In profile-aware shell installs, profile selection loads credentials; an
incoming explicit `DELEGATE_CONFIG` remains the runtime-policy config only
after the selected profile overlay passes validation.

## Do not promote implicitly

Repository development should not overwrite an installed runtime, user config, or local worktree store as a side effect. Promote a checkout to an installed command only through an explicit install/update step after review and tests.

## Promotion ritual and `delegate doctor`

Several agents can share one `~/.delegate/src`; when one of them installs a new runtime, every child the others launch from that moment runs the new code, and nothing in the launch path says so. The stamp is how that becomes visible:

1. Install the reviewed checkout into `~/.delegate/src` (rsync or tarball).
2. `delegate promote --actor <who> --source <commit or branch>` -- run through the installed command so the recorded digest is the live one. This writes `~/.delegate/last-promotion.json` and lists workflow supervisors still pinned to the previous runtime.
3. `delegate doctor` -- confirm `promotionMatchesRuntime: true`. This requires the
   executing import root to be the installed root, matching package bytes,
   present launchers, and an unchanged artifact manifest from a verified stamp.

If `AI_PROFILE` is set and its overlay is missing mid-upgrade, the profile guard blocks `promote` like any other mutation; run it as `env -u AI_PROFILE delegate promote ...`.

`doctor` is offline and read-only: it does not run launchers, install code, repair
permissions, or prune the supervisor index. Its identities are separate:

- `runtimeDigest` remains the executing package's workflow-snapshot digest;
  `executingRuntimeDigest` is its explicit alias. This digest includes the
  generated pin launcher and persona hook, not the installed launcher's bytes.
- `executingImportRoot` and `executionMode` distinguish an installed package
  from a checkout or pinned snapshot. Equal package bytes do not make distinct
  roots the same installation.
- `installedRuntimeDigest` describes `~/.delegate/src`, or the executing
  `site-packages`/`dist-packages` root for a wheel installation without that
  HOME-level payload.
- `installedArtifact` lists package-file hashes and identities of the installed
  entrypoint and current PATH `delegate` launcher, including symlink targets.
  `installedArtifactDigest` binds that manifest. Missing launchers prevent a
  verified match. Arbitrary shell launchers are recorded, not interpreted: this
  is an observed artifact manifest, not proof of every command a shim may run.
- `promotion` is the last recorded stamp. Old stamps without the manifest remain
  readable but cannot certify parity. A missing stamp or changed package,
  entrypoint, or outer shim makes `promotionMatchesRuntime` false.

`promote` records the executing and installed observations under its stamp lock.
`source` remains an operator label: `sourceVerified: false` explicitly means it
is not proof of a commit or reviewed build. A checkout invocation can record a
stamp but cannot certify the installed artifact. `--runtime-digest` remains
available for backfills; its supplied value is preserved with
`runtimeDigestOverride: true` and `artifactVerified: false`, even when it happens
to equal the observed digest. Run a normal promotion through the installed
command to establish a new verified observation.

The promotion lock serializes stamp writers, not external installers. These are
point-in-time filesystem observations, not protection against concurrent payload
replacement or proof of already-loaded Python bytecode. Installer changes and
live promotion require separate authorization. Existing pinned supervisors
remain untouched and are reported separately.

## Run metadata

Tracked runs may write workspace-local metadata under `.delegate/`. That metadata is for inspection commands such as:

```bash
delegate runs
delegate snapshot <alias-or-runId>
delegate run-output <alias-or-runId> --completion-report
```

Do not commit `.delegate/` run state. Do not tail raw logs as the normal integration path; use Delegate's bounded inspection commands.

For persistent worktree behavior, see [Worktrees](worktrees.md).
