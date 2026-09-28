# F5: Lane environment and isolation

## Bottom line

A lane's environment is the caller's environment with a few overrides. Five separate places in the code decide it, and neither the lane nor the caller ever sees it written down as one thing. The biggest gap is that work lanes have no write boundary owned by Delegate, on either platform. HOME, sibling checkouts, and anything else the user can write stay writable. Meanwhile the estate config (`policy.profile: "external-sandbox"`, on the Mac and the devbox) switches off the engines' own sandboxes on the assumption that some outside sandbox exists. None does. The one Delegate boundary that hides HOME, the bwrap safe backend, is opt-in, Linux-only, and safe-mode-only, and it is not enabled on either cell.

Three changes would remove most of the pain:
1. A **work write guard**: HOME read-only except a declared set of writable roots. On Linux this reuses the existing bwrap `SandboxPlan` machinery; the Mac would use Seatbelt.
2. **One resolved lane-environment record**, shown in dry-run output and the manifest and told to the child.
3. **Placeholder substitution limited to the files the sync itself wrote.** Today it can change tracked content in edit-capable worktrees.

Most of the TMPDIR and socket-path cuts are already fixed. That fix only reaches safe and isolated runs.

## Root causes

### RC-1: Work lanes have no write boundary, and the estate turns the engines' own boundaries off

- **Symptoms:** 12 cuts, 2026-07-12 to 2026-09-24.
  - pc2_39213c72b0f39c2a and pc2_6daa169c5f8b2caf (blockers, open): the `rm -rf ~` incident.
  - pc2_2a95b436622c6280 (a lane paged Trey through `ask`; resolved).
  - Writes outside the workspace: pc2_50d7a835ebaf862e, pc2_58a435c5c45ef360, pc2_585c3bcffc065bc5, pc2_adf3b6c88322bee2.
  - pc_42a766f4dfcc (a lane moved the checkout to the Trash).
  - pc_af12bf25680f (npm link): only partly this cause.
  - pc2_e5e298950b9b17c6 (kills of other processes): not covered by any filesystem fix.
- **Verified: what HOME and the write boundary are today, per mode.** The code path is the same on the Mac and Linux except where the table says otherwise.

| Mode / backend | HOME seen by the child | Delegate write boundary | Engine boundary under the estate config |
|---|---|---|---|
| safe, copy backend (default; also the live state on both cells) | Real, writable at the OS level | cwd is a temporary copy. This only stops relative-path edits. | Codex: read-only plus writes to scratch and temp only (a real boundary). Claude: plan mode plus a tool allowlist. Grok: `--sandbox read-only`. Pi, Omp, OpenCode, Droid: tool or tier limits. Cursor and Kimi: prompt only. Devin: rejected. |
| safe, bwrap (Linux, opt-in) | tmpfs. Only the selected engine home and configured binds are visible. | Workspace read-only | Engine controls stay on as a second layer |
| work, `--isolation none` (the default) | Real | **None.** TMPDIR is also inherited from the caller (`runner.py:2248` allocates temp only when `mode == "safe" or effective_isolation != "none"`). | Codex: `--dangerously-bypass-approvals-and-sandbox` (`config.py:340-346`, `argv_builders.py:708-712`). Grok: bypass. Devin: `--permission-mode dangerous` (`argv_builders.py:544`). Cursor: force. Claude: `workPermissionMode` (default `auto`). |
| work, `--isolation worktree` | Real | **None** outside the worktree. `docs/security-model.md` says it does not prevent absolute-path writes. | Same as the row above |
| call (including `--pure`) | Real. `PURE_ENV_ALLOWLIST` includes HOME (`profiles.py:22`). | Empty temporary cwd | — |

  Supporting code:
  - `profiles.child_environment` (`profiles.py:310-338`) copies all of `os.environ` and strips only the Delegate root, mail, pin, and Memorum variables.
  - `seatbelt.py` is reachable only from Codex pure. Codex pure is rejected before launch (`argv_builders.py:211-215`), so no Mac path uses Seatbelt today.
  - On the devbox, read-only: no config layer sets `isolation.safeBackend`, and `DELEGATE_SAFE_BACKEND` is unset. Yet `bwrapBinds` is fully populated in `config.work.json` and `config.personal.json`, so those binds are configured for a backend nothing uses.
  - `docs/configuration.md:769` says `external-sandbox` is to be used "only inside a separate sandbox you control". On the Mac, no such sandbox exists. On the devbox, the only boundary is the per-cell Unix user, and the incident destroyed that user's home.
- **Verified (partial fixes shipped):**
  - Outside-cwd detection landed 2026-09-25 (`outside_cwd_changes.py`, called at `runner.py:4440`). It is best-effort and only runs when cwd is strictly inside a repo. pc2_58a435c5 (`--cwd <worktree>`, where the worktree is its own top level) and pc2_50d7a835 (a worktree lane writing to `delegate-worktrees/<fp>/`) are both invisible to it.
  - The no-page `ask` stub (`runner.py:2379-2402`, commit b7a094e9, 2026-09-25) is opt-in through `DELEGATE_CHILD_NO_PAGE_ASK`.
- **Inferred from the cut text:** the incident's test fixture copied HOME from the process env, so a scratch HOME would have defused that particular test. I have not verified which isolation mode the Devin lane used.
- **Still live at 0.31.0?** Yes, for every work lane on both machines.
- **Why it keeps recurring:** isolation was designed as *workspace* isolation. Its job is to keep relative-path edits out of the source checkout. Protecting the host was left to the engines, and the estate's policy profile turns the engines' protection off. Each incident gets a narrow, post-hoc, or opt-in patch (the ask stub, outside-cwd detection) instead of a boundary.
- **Recommended fix: a work write guard (lane host boundary).**
  - When a work lane launches, wrap the engine the way the safe bwrap path already does, with a different plan:
    1. bind `/` as-is;
    2. bind `$HOME` read-only;
    3. re-bind these as writable: the execution root, the git common dir (needed for worktree commits), the run's scratch and compact temp, the selected engine home, the `rw` entries in `isolation.bwrapBinds`, and a new declared list of writable home paths (caches, `~/.claude.json` and similar).
  - `rm -rf ~`, writes into sibling checkouts, moving the checkout to `~/.Trash`, and writes into `delegate-worktrees/<fp>/` would all then fail with EROFS or EPERM, loudly.
  - Files touched:
    - `sandbox_bwrap.py`: a second builder beside `build_bwrap_argv`.
    - `runner._launch_tracked_process`: today it treats `sandbox` as meaning safe mode.
    - Plan resolution in `cli.py`/`isolation.py`.
    - A config key in `config.py`.
    - `docs/security-model.md`.
    - On the Mac, a Seatbelt write profile shaped like `build_codex_pure_profile`: `(allow default)`, deny `file-write*` under HOME, then allow the writable roots.
  - Record the plan in the manifest and in dry-run output.
  - Proof: a fake-engine test whose script runs `rm -rf "$HOME/canary"` and `echo x > ../sibling/f`, with HOME set to a temporary home. Today both writes succeed. With the guard, both fail and the canary survives.
  - This tightens safety. It does not relax `argv_builders`/`safe_workspace` constraints.
- **Alternatives:**
  - A scratch HOME for the child is simpler on paper, but it breaks engine auth, git identity, mise and cargo toolchains, and nested `delegate`. HOME is part of the workflow pin identity (`workflow_identity.py:84-86`). A HOME made of symlinks back to the real entries protects only a whole-home wipe; `rm -rf ~/.ai-profiles/` still reaches through the link.
  - A brief clause ("fixture HOME = tempdir") is the band-aid the cuts propose. It is prompt-level, so it will fail the same way the Devin "do not commit" instruction did (RC-4).
  - Dropping `external-sandbox` for Codex restores Codex's own sandbox but does nothing for Devin, Cursor, Grok, or Droid.
- **What the guard does not do:** it does not protect the writable roots themselves, stop signals to other same-UID processes (pc2_e5e29895), or stop writes outside HOME such as `/opt/homebrew` (pc_af12bf25).
- **Beads and rulings:** no bead is open for this. Closed bead dlg-1vj.7 ("Typed isolation and sandbox plans") is the machinery to reuse.

### RC-2: The lane's environment contract is spread across the code and never stated to the lane

- **Symptoms:** 17 cuts, 2026-07-15 to 2026-09-18, mostly resolved in the ledger with only a workaround.
  - Safe-mode capability surprises: pc2_534c55a1 (/tmp deliverable), pc2_90881f72 (DNS for exa), pc_e2d78865, pc_140e44ed, pc_b0903736, pc_03ebcbe3 (the caller believed safe mode blocks reads; it does not), pc_342719735, pc2_d1d408af.
  - Fresh worktrees: pc2_f57d54c5, pc2_c943875b, pc2_87bd0ce9.
  - Lane runtime vs docs: pc2_5b086b71, pc2_ce6de4e7, pc_f9fad38b, pc_93a32276.
- **Verified: where the environment gets decided.**
  1. `profiles.child_environment`: ambient inheritance.
  2. `cli._set_child_root_env` (`cli.py:680-698`): the Delegate root variables.
  3. `runner._launch_tracked_process` (`runner.py:2408-2469`): TMPDIR, the `ask` stub, the bwrap wrap.
  4. `workspace_spec` `--env`/`--setup` (worktree only; 2a612a37, 2026-09-24).
  5. Profile env and engine-home overrides.
  
  What the child is told:
  - The safe-review prefix (`argv_builders.py:33-40`) says only "do not mutate". It never mentions the writable scratch or TMPDIR, the lack of network (Codex safe restricts tool network access), or which files are missing.
  - `PERSISTENT_WORKTREE_CONTEXT_NOTE` (`isolation.py:491`) never says that gitignored state (`node_modules`, `.env`, build output) is absent.
  - `describe` `childEnvironment` (`describe_payload.py:1112-1116`) lists three variables.
- **Inferred:**
  - PATH loss inside Codex lanes comes from the harness running `bash -lc`, which re-sources dotfiles (pc2_a1ca654d). Delegate's PATH is replaced, not used.
  - The skill's bwrap claim (pc2_d1d408af) has since been corrected in `references/runtime.md:29`.
- **Still live at 0.31.0?** Partly. `--setup` now gives worktrees a supported provisioning hook, and TMPDIR is writable in Codex safe. No lane is told its effective capabilities.
- **Why it keeps recurring:** each engine and mode was hardened separately, and the contract lives in the code and in `security-model.md` prose. Callers and lanes rediscover it one failure at a time (the DNS limit has been rediscovered three times).
- **Recommended fix:**
  - Resolve one lane-environment record at launch: execution root, writable roots, HOME status, TMPDIR, network, shell availability, excluded state.
  - Emit it in dry-run output, the manifest, and `describe`, and render it as a short framing block after the safe prefix or worktree note.
  - Proof: a snapshot test asserting that the child prompt for Codex safe names the writable temp path and "no network", and that the prompt for a worktree run says gitignored paths are absent. Today neither appears.
- **Alternative:** docs and skill text only. This is cheaper, but lanes do not read the docs, so pc_140e44ed-style guesses continue.

### RC-3: Persistent-worktree dirty sync turns tracked symlinks into placeholder files that git can commit

- **Symptoms:** pc_973c6d499bf6 (open; placeholders committed at HEAD and pushed to public GitHub), pc_342719735fcd (unloadable skill). 2026-07-18 to 2026-07-27.
- **Verified:**
  - `worktree_execution.py:571` calls `safe_workspace.sync_git_dirty_snapshot` whenever the source is dirty; it is auto-included by default.
  - That function ends with `block_external_symlinks(worktree_path, git_root)` (`safe_workspace.py:817`).
  - `block_external_symlinks` walks the *entire* worktree (`os.walk`, lines 578-596). It replaces every symlink that resolves outside the source, tracked ones included, with `SAFE_BLOCKED_SYMLINK_PLACEHOLDER` files.
  - In an edit-capable worktree git sees those as typechanges, so a lane or coordinator running `git add -A` commits them.
  - The existing test (`tests/test_wave4_launch_features.py:132`) covers only an *untracked* external link.
  - I verified this by reading the code, not by running it.
- **Still live at 0.31.0?** Yes, per the code.
- **Why it keeps recurring:** one safe-mode secrecy primitive is reused for work-mode worktrees, which have a different threat model and are meant to be committed.
- **Recommended fix:**
  - In persistent worktrees, apply placeholder substitution only to the untracked paths the sync mirrored (the `leak_blocked` set, already computed).
  - Leave tracked entries exactly as git checked them out. The source already has those links, and the child could read their targets by absolute path anyway.
  - Proof: a repo with a *tracked* absolute symlink plus one dirty file, launched with `--isolation worktree`. Assert that `git status --porcelain` in the worktree does not list the link. Today it shows `T`.
- **Alternative:** keep the substitution but mark the paths `--skip-worktree`. That hides the typechange, but lanes still get unusable links.

### RC-4: "Do not commit" is prompt-only unless the lane uses a worktree

- **Symptoms:** pc_27499ee55b61, pc_f4a05f546b6c, pc_107229116c4b (all open, 2026-07-16 to 2026-07-17; four occurrences).
- **Verified:**
  - `--forbid-commit` has existed since 71caa2c5 (2026-06-22). It implies worktree isolation and refuses `--isolation none` (`cli_parser.py:1279-1297`).
  - It is *detection* after the child exits ("mark the run failed"), not prevention (`isolation.py:511-516`).
  - In-place lanes have no enforcement at all.
- **Still live at 0.31.0?** Yes, for in-place work. Worktree plus `--forbid-commit` would have caught all four occurrences, but only after the fact.
- **Recommended fix:**
  - Under `--forbid-commit`, inject `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0=core.hooksPath` pointing at a run-owned hooks directory whose `pre-commit` and `pre-merge-commit` hooks exit non-zero.
  - Allow this in every isolation mode, and keep the post-exit check.
  - Proof: a fake engine that runs `git commit` in place. Today the commit exists; with the fix it is refused.
- **Band-aid caveats:** `--no-verify` bypasses the hooks, and git older than 2.31 ignores the variables. It is still the right cost level here. Once RC-1's guard exists, making the git common dir read-only for forbid-commit lanes would be the stronger version.

### RC-5: Detached launches lose the ambient identity Delegate depends on

- **Symptoms:** pc2_efa65e8f52df2048, pc2_18c75f5e, pc2_df2a30ed, pc2_de5e2649, pc2_152e056a (all open, 2026-09-13 to 2026-09-28).
- **Verified:**
  - The workflow pin records HOME and the credential namespaces (`workflow_identity.py:84-86`).
  - `validate()` can only refuse with `workflow_profile_drift` (`workflow_identity.py:110-134`); it cannot restore them.
  - PATH and mise under systemd belong to the launch recipes, not to Delegate.
- **Still live at 0.31.0?** Yes.
- **Recommended fix:** dlg-jyb's `--unit` (Delegate runs `systemd-run` itself, taking env from the pin plus PATH). Otherwise, let workflow commands re-derive profile env from the pin when the ambient env is empty rather than conflicting.
- **Alternative:** recipe docs only. That is what exists now, and it keeps failing.
- **Beads:** dlg-jyb (open) and dlg-87d.

## Cuts that are not this family's problem

- **linux-devbox, estate-build-cache keying:** pc2_16aa54c078fa33c0. The cache keys on `DELEGATE_RUN_ID`, which is per run. `DELEGATE_SOURCE_ROOT` is the stable key Delegate already exports.
- **linux-devbox launcher:** pc2_13ab005a44cab87a and pc2_26d40dbe2653ced3. Still live: the devbox `~/.local/bin/delegate` re-exports the incoming `DELEGATE_CONFIG` after `--auth-profile` (around lines 88-90). The repo shim warns instead (`bin/delegate-profile-shim:295-303`).
- **linux-devbox dotfiles:** pc2_a1ca654d83f7f173, fixed there (verified: `~/.bash_profile` now sources `~/.profile`). pc2_87bd0ce9's PATH half was fixed there too.
- **writing-plans / Atlas plan templates:** pc2_53ede516, pc2_9bb6216e, pc2_74e88320, pc2_72b03ae1. The `node_modules` symlink is the caller's recipe; Delegate never creates one (grep shows no `node_modules` code). The fix is `--setup 'pnpm install --prefer-offline'` or `npm ci`.
- **Veritas lane launcher:** pc2_152e056a (mise under systemd).
- **Profile/auth error text:** pc2_c635829c999afd17.
- **Grok harness and brief:** pc2_ce6de4e7a8733c35.
- **Codex's own JS kernel tool:** pc_6c3d3066bbe3 and pc_aab21f7d9b3c. Delegate ships no `kernel.js`. The "no shell" rule came from the caller's brief (the same goes for pc_e2d7886544e4's premise).
- **fleet:** pc_ae48133990d5.
- **Report formatting:** pc_c90b4b110800.
- **Dev-checkout tooling:** pc_592a2b7ad28f and pc_ede8cf080731.
- **Worktree lifecycle:** pc2_690c4b6e (stale base). `--base REF` and `aheadBehind` now exist.
- **Workflow close-agent prompt:** pc2_e5e298950b9b17c6 (see RC-1 for why the guard does not cover it).
- **Hardlink snapshot refusal:** pc2_327e1e378f59f49b is a deliberate single-link reader guard. The error text could suggest excluding `.delegate` from `cp -al`.

## Cuts that are stale / already fixed

- **TMPDIR socket length and TMPDIR inside git:** pc2_67e78ae2, pc2_0048cfc6, pc_33dadb54, pc2_6649a296, pc2_bf100359, pc2_f766916b (inferred).
  - Children of safe and isolated runs now get `/var/tmp/dlg-<uid>/<24 hex>` (`run_scratch.py:144-201`; 0f7ff641, 2026-09-22), outside any git tree, with neutral scratch at `~/.delegate/run-scratch` (a555c7b6, 2026-09-06).
  - The deferred.md ruling on TMPDIR/tsx is superseded by that code, and troubleshooting.md:589 is slightly stale.
  - Caveat: `--isolation none` work runs still inherit the caller's TMPDIR.
- **Codex safe TMPDIR, uv, heredoc:** pc2_9507da33, pc2_d09ba73a, pc_140e44ed (inferred). The named permissions profile grants scratch and temp writes (`runner.py:2327-2360`; eb864373, 2026-09-05).
- **Safe copy recursion:** pc2_2774e642 (e361aff4 `safe_workspace_temp_base`). pc2_3cbe1b61 is likely fixed by 3890e42f and 3959a890 (2026-08-31); not runtime-verified.
- **Devin safe:** pc_d741782a. Now rejected at preflight (`argv_builders.py:515-522`).
- **Isolation flip:** pc_b550dc8c and pc_e29dff83. `_map_auto_isolation` maps work to none (`isolation.py:327-331`). pc2_11d38522 is that ruling working as designed.
- **Root variables:** pc_89e4c9f0, pc_f48a6fc2, pc_0e62294d. Stripped in `profiles.py:322-324` and re-set as resolved paths in `cli.py:680-698`.
- **Worktree provisioning ask:** pc2_f57d54c5, covered by `--setup` (2026-09-24). The prompt note is still missing (RC-2).
- **`ask` paging:** pc2_2a95b436. An opt-in stub now exists.
- **Lint picking up run scratch:** pc_e7ea0a74 and pc_8f729e3d. Scratch left `.delegate/runs` on 2026-09-06. pc_ff6c0556 is unknown: Delegate writes no `.delegate/.gitignore`.
- **Non-git cwd:** pc2_585c3bcf. `workspace_for` pins a non-git directory to itself (`request_build.py:404-413`). The `~/Code` result was most likely the shell's cwd reset (inferred).
- **pc_8407f21c51cb:** cannot determine. It may be a `.git/info/exclude` omission, which has been disclosed since f44bfdaf.

## Open questions for Trey

1. **Work guard default and scope.** Should it be on by default for work lanes, or opt-in first? On the Mac, is Seatbelt acceptable? It is deprecated, and nesting Seatbelt sandboxes is typically refused, which would conflict with any engine-native Seatbelt (not verified). Or should the guard ship Linux-only first?
2. **Cells' safe backend.** Should the cells set `isolation.safeBackend: "bwrap"`? Their `bwrapBinds` are already configured for it.
3. **`external-sandbox` on the Mac.** Should the Mac config keep `policy.profile: "external-sandbox"` when no outside sandbox exists? If RC-1 lands, the guard becomes that sandbox.
