# Mail seam recon — agent-to-agent messaging (Lane M input)

Scope: what the mail seam is today, what "always on" changes, what is cruft, and the
smallest simplification. Read-only. Line numbers are at `637475e`.

**Two corrections to the brief's premises, both load-bearing.**

1. **The mail seam does not talk to `post` at all.** `grep post src/delegate_agent/mail*.py`
   returns nothing. The only `post` caller in the tree is `notify.py:131,145,162`, reached
   only by the explicit global `--notify` flag. Delegate mail is a *workspace-local file
   mailbox* under `.delegate/mail/`, parent-owned, no daemon and no network. A machine
   without `post` is already completely unaffected by mail and stays so after any flip.
   Nothing in Lane M needs `post doctor` semantics.
2. **Mail is already reachable with `mail.enabled: false`.** `mail emit` never reads config
   (`mail_core.py:1306-1354`), and `bind_mail_identity` runs for every work run regardless of
   the flag (`cli.py:796-797`, `worktree_execution.py:353-354`). A lane can send mail today
   with the flag off; `tests/test_mail_gating.py:329-384` asserts exactly that. So
   `mail.enabled` is not an on/off switch for messaging — it is a switch for four narrower
   things listed in §2.

---

## 1. Map

| Surface | file:line | What it does |
| --- | --- | --- |
| Config key `mail.enabled` (default `False`) | `config.py:202-204` | Embedded default; `load_config` deep-merges from it (`config.py:1508`), so an operator config that omits `mail` inherits it |
| Validation / reader | `config.py:582-595`, `1684-1686` | Strict object, only key `enabled`, must be bool; **an absent section reads False** |
| Shipped example | `config.example.json:136-138` | `"mail": {"enabled": false}` |
| Prompt suffix `MAIL_PROMPT_SUFFIX` | `mail_core.py:77-83` | 301 chars, one `## Delegate mail` block naming `mail inbox` / `mail read` |
| Suffix injection | `request_build.py:3523-3529`, slot at `695-696` | Gate: `mail_enabled and mode == "work" and instruction_mode != slash and not skip_skill_preamble`; sits after the completion-report suffix |
| Storage prep | `mail_core.py:204-211` | Creates `mail/{boxes,sent}`, `meta.json`, `rules.json`; **raises** on OSError |
| Storage call sites | `cli.py:770-771`, `cli.py:1338-1339`, `worktree_execution.py:320-321` | All gated `mode == work and mail_enabled(config)`; all run *before* run registration |
| Launch wiring | `mail.py:81-125`; sites `cli.py:799-800`, `1370-1371`, `worktree_execution.py:657-658` | `enabled=False` returns argv untouched (`mail.py:98-99`); same gate expression repeated |
| Sandbox grant table | `mail_core.py:61-74` | Per-engine row; `RuntimeError` if an engine is missing |
| Grant + warning | `mail_core.py:358-401` | Isolated + `scoped` → `--add-dir <mailroot>` (codex: `-c sandbox_workspace_write.writable_roots=[...]`, omp: `--add-dir=<root>`); isolated + unreachable/degraded → stderr `delegate mail: WARNING: ...` |
| Env: identity | `mail_core.py:283-287` | Sets `DELEGATE_RUN_ID`, `DELEGATE_MAIL_SELF` for every work run |
| Env: strip / pin | `profiles.py:293`, `mail_core.py:246,290-292` | Strips inherited identity before a fresh bind; `DELEGATE_SOURCE_ROOT` roots a lane's mail commands |
| CLI `--mail-push` | `cli_parser.py:1631-1636` **and** `cli_parser.py:2069-2074` | Boolean, parsed in two separate token loops |
| Input JSON `mailPush` and its gate | `request_build.py:128,1953-1955,2116`, gate `2372-2385` | Same knob via `run --input-json`; `mail_push_disabled` without `mail.enabled`, `mail_push_unsupported` for slash / pass-through / non-work |
| Stop hook command | `mail_push.py:96-117` | `<python> <delegate> --cwd <root> mail hook-pump \|\| printf ... SENTINEL` |
| Claude adapter | `mail_push.py:210-219`, `262-266` | Writes `<box>/settings.json`, injects `--settings <path>` (**overwrites** an existing value) |
| Codex adapter | `mail_push.py:156-187`, `267-294` | Private `CODEX_HOME` (copies `auth.json`/`config.toml`, symlinks the rest), plus `-c hooks=true` and `--dangerously-bypass-hook-trust` |
| Env: hook | `mail_push.py:257-258`, `283-284` | `DELEGATE_MAIL_HOOK_HARNESS`, `DELEGATE_MAIL_HOOK_NONCE` |
| Internal `mail hook-pump` | `mail_push.py:551-617`; guard `cli_parser.py:906-911` | One bounded batch, at-least-once via a pending marker |
| bwrap rw root / degradation / manifest | `runner.py:2000-2010`, `2593-2742`, `576-577`, `cli.py:269-277` | Push scratch stays writable; `mailPushDegraded` state and `mail_push_degraded` event; `mailPush` and `mailPushAdapter` in manifest and dry-run |
| Global `--notify TARGET` | `cli_parser.py:587-592`; `notify.py`; `runner.py:4164-4210` | One metadata line via `post` at terminal state; **the only `post` dependency** |
| `--group` (launch vs mail) | `cli_parser.py:598-602` vs `917-922` | Launch grouping becomes the expansion set for `mail send --group` (`mail_core.py:573-586`); the two are deliberately distinct |
| Profile-guard exemption | `profile_guard.py:74-80` | `inbox`/`status`/`watch`/`read --peek` are read-only |
| Docs | `docs/configuration.md:94-115`, `docs/cli-reference.md:26-69`, `docs/security-model.md:10-57` | README has **zero** mail mentions |

---

## 2. What "always on" would change

Measured, not inferred. I copied the tree at `637475e` to `/var/tmp`, ran the suite
(`3050 passed, 15 skipped` in 424s), flipped `config.py:203` to `True`, and re-ran
(`25 failed, 3047 passed` in 424s). Same snapshot both runs. Full log saved at
`/tmp/claude-1001/.../scratchpad/mail-flip-pytest.txt`.

Four paths are gated on `mail_enabled(config)`. Flipping the default changes each as follows.

**a. Storage prep** (`cli.py:770`, `cli.py:1338`, `worktree_execution.py:320`). Every work
run now creates `.delegate/mail/{boxes,sent}` plus two JSON files in the user's workspace.
No process spawns, no network. **This is the one hard regression:** `prepare_mail_storage`
*raises* on OSError (`mail_core.py:208-211`) and the call sites have no `try`, so a workspace
where `.delegate/mail` cannot be created turns every work launch into exit 2
`mail_storage_unavailable` — before the run is registered. `tests/test_mail_gating.py:304-327`
pins that refusal today. Under an opt-in flag a refusal is right; under a default it is not.

**b. Prompt suffix** (`request_build.py:3523`). +303 UTF-8 bytes on every wrapped work
prompt (301 suffix chars plus the `\n\n` join). Cost is ~75 tokens per work run for users
who never read mail. It also pushes prompts through the 100 KiB argv guard
(`request_build.py:3531-3543`): a prompt within 303 bytes of the limit now fails
`prompt_too_large` on argv-transport engines. That is exactly what
`tests/test_persona_size_guard.py` caught. Today that means kimi, cursor, and omp; after
Lane A1 moves cursor and omp to stdin it is kimi alone.

**c. Sandbox wiring** (`mail.py:100-109`). Only fires for isolated workspaces. Scoped engines
gain `--add-dir <mailroot>`. Unreachable policies (codex `read-only`, grok
`devbox`/`read-only`/`strict`, any unknown value) now print a stderr warning on every such
isolated run. That is the spam risk: it is per-launch, not once, and it is emitted from a
gate the user did not ask for.

**d. `--mail-push` precondition** (`request_build.py:2374`). `mail_push_disabled` becomes
unreachable in practice. Push itself stays opt-in — nothing installs a hook by default, and
`tests/test_mail_push_gating.py:113-...` pins that.

**Nothing fails, hangs, spams the network, or leaks.** No `post`, no subprocess, no daemon.
The plan's degrade requirement ("a machine without `post` must be completely unaffected") is
already satisfied by construction for mail; it only ever applied to `--notify`.

**Tests that assert the current default** (the complete flipped-run failure set):

| Test | Why it breaks |
| --- | --- |
| `tests/test_mail_gating.py::test_mail_config_defaults_false_and_unknown_keys_are_rejected` | Asserts `mail_enabled(embedded_default_config())` is False. Genuine contract change. |
| `tests/test_persona_size_guard.py::test_cli_full_framed_utf8_persona_prompt_boundary` | Boundary fixture: framed prompt now crosses the argv guard. |
| `tests/test_persona_size_guard.py::test_input_json_full_framed_utf8_persona_prompt_boundary` | Same. |
| `tests/test_persona_framer.py::test_no_persona_preserves_user_prompt_bytes_for_every_framed_transport` (10 work subtests) | Exact-prompt equality; suffix appended. |
| `tests/test_persona_framer.py::test_persona_framing_preserves_blank_bytes_for_each_transport_and_mode` (3 work subtests) | Same. |
| `tests/test_persona_framer.py::test_untrusted_note_text_never_controls_persistent_worktree_framing` (9 subtests) | Same. |

Every other mail test passes unchanged, including the whole delivery, reply, rules, bounds,
push, and contract suites. `tests/test_mail_gating.py:329` (disabled-launch behavior) still
passes because it sets `enabled: false` explicitly.

---

## 3. Cruft and defects

**Unreachable code.** `mail.py:63-68` picks `dict(provision.env)` when
`provision.warning is not None and provision.env is not None`. `provision_mail_push` sets
`env` only on success (`mail_push.py:297-306`, `warning=None`) and leaves it `None` on both
failure paths (`234-238`, `316-326`), so the condition is unsatisfiable and the arm is dead.
Harmless only because `provision_mail_push` mutates the caller's env dict in place (`295-296`).

**Tautological guard.** `mail_push.py:49-54` builds `MAIL_PUSH_ADAPTER_ROWS` by comprehension
over `KNOWN_ENGINES`, then raises if its keys differ from `KNOWN_ENGINES`. Always true by
construction. (`mail_core.py:61-74` is the real version — that dict is hand-written.)

**Dead parameters.** `_effective_run` uses two of four (`mail_core.py:550-552`).
`_match_message_id(..., *, sent_only=False)` never reads `sent_only` — `roots` is always
`[sent_root(...)]` and all three callers pass `True` (`589-591`; callers `777`, `1051`,
`1135`). `_current_recipient` ignores `registry_root` (`924-925`).
`mail_push_fallback_env_overrides` opens with `del registry_root, run_id` (`mail_push.py:190-197`).

**Test-only production code.** `wire_work_mail_argv` (`mail_core.py:295-316`) is documented as
a compatibility wrapper; its only eight callers are in `tests/test_mail_gating.py`.

**Duplicated logic.** `read_message` repeats its find-and-disambiguate loop verbatim for the
peek and non-peek branches (`mail_core.py:991-1006` vs `1008-1025`), ~18 lines. The launch
gate `mode == work and mail_enabled(config)` is written out three times (`cli.py:770`+`800`,
`1338`+`1371`, `worktree_execution.py:320`+`658`). `--mail-push` is parsed in two independent
token loops (`cli_parser.py:1631`, `2069`). And `mail_core.py:883` rebinds `index` — the
registry index loaded at `805` — to a loop counter; nothing reads it after, so it is latent.

**`mail watch` never times out without `--once`.** `mail_core.py:1188-1190` sleeps and
`continue`s unconditionally; the deadline check at `1191` is only reachable when
`command.once` is true. The parser accepts and validates `--timeout` for continuous watch
(`cli_parser.py:982-993`) and it is then silently ignored. A lane told to run
`delegate mail watch` blocks until something kills it. The prompt suffix does not mention
`watch`, which is the only thing keeping this out of the default path — **keep it that way.**

**`--settings` is overwritten, not merged.** `_set_claude_settings` (`mail_push.py:210-219`)
replaces the value of an existing `--settings` argument. Delegate's own claude argv builder
never emits one, so this is reachable only through operator-supplied argv, but it is a silent
clobber of a user's settings file rather than a refusal.

**Credential copy per push run.** `_codex_home_for_mail_push` (`mail_push.py:169-179`) copies
`~/.codex/auth.json` into run scratch for every codex mail-push launch, cleaned at terminal
finalization (`runner.py:2606-2614`). Fine while push is opt-in; it is the strongest argument
against ever defaulting push on.

**Argv-transport carve-out.** `mail_core.py:395`,
`elif engine in {"codex", "omp"} and prompt_transport == "argv":`, inserts the mail grant
before the trailing prompt argument. Lane A1 moves omp to stdin, so `omp` drops out of that
set and the branch becomes codex-only. Coordinate the edit — A1 already claims
`mail_core.py`.

**Docs gap.** README never mentions mail, so a default-on flip without a README paragraph
ships a feature no new user can discover.

---

## 4. Smallest simplification proposal

The plan's target is "one config key (`mail.enabled`, default true), one CLI override, one
prompt suffix, one stop-hook path." Three of those already hold: there is exactly one config
key, exactly one prompt suffix, and `provision_mail_push` is already a single code path with
a two-branch adapter switch. Claude's `--settings` and Codex's `CODEX_HOME` are genuinely
different mechanisms and cannot collapse further. So the real work is the default, the
override, and the failure mode.

**Required for "always on" (do these):**

1. `config.py:203` → `True`; `config.example.json:137` → `true`. Deep merge
   (`config.py:1508`) carries it to operator configs that omit the key, including
   `~/.delegate/config.json`, which today has no `mail` section at all. An explicit
   `"mail": {"enabled": false}` keeps working unchanged.
2. `config.py:1684-1686` → treat an absent section as **on**. Without this, the two paths
   that bypass the merge — a pinned workflow attempt config (`config.py:1493-1506`) and
   hand-built config dicts — silently keep the old default. Three lines.
3. **New global `--no-mail`,** modeled exactly on `--no-completion-report`
   (`cli_parser.py:545-548`): ~4 lines in the global token loop plus threading through the
   `parse_*` signatures. Put it in the *global* loop, not the per-mode tails, or it inherits
   `--mail-push`'s two-loop duplication.
4. **Make storage failure degrade, not refuse.** Wrap the three `prepare_mail_storage` calls;
   on `MailError`, emit one stderr warning, record it in `request.warnings`, and run that
   launch with mail off. This is what makes a default safe on machines Delegate has never
   seen. It replaces `tests/test_mail_gating.py:304-327` with a degrade test.
5. **Make the isolated-workspace unreachable-mail warning once-per-run and non-fatal**, or
   drop it to a manifest warning only. Per-launch stderr on every codex `read-only` isolated
   run is the spam vector.
6. Docs: `docs/configuration.md:94-115` rewrite, `docs/cli-reference.md:26-69`, a new README
   paragraph, `CHANGELOG`.

**Behavior-preserving cleanups (same lane, no risk):**

7. One helper `mail.launch_enabled(mode, config)` replacing the three duplicated gate
   expressions (6 sites).
8. Delete `wire_work_mail_argv` (`mail_core.py:295-316`, 22 lines); repoint the 8 test calls
   at `wire_work_mail_launch`.
9. Delete the tautological adapter guard (`mail_push.py:53-54`).
10. Delete the five dead parameters and the unreachable env arm (`mail.py:64-67`).
11. Dedupe the `read_message` find loop (~18 lines).
12. Drop `omp` from `mail_core.py:395` when Lane A1 lands.

**Estimated size.** Roughly 75-95 source lines deleted against ~30 added for `--no-mail` and
the degrade path — near flat in line count. The win is operator surface, not LOC: one key
that is on, one flag to turn it off, and no launch that can fail because of mail.

**Explicitly not proposed.** Do not default `--mail-push` on: it copies `auth.json` per codex
run, adds `--dangerously-bypass-hook-trust`, overwrites `--settings` for claude, and is
verified for 2 of 10 engines (`mail_push.py:50`).

**Public behavior that changes:** the embedded default; `.delegate/mail/` in every workspace
running a tracked work run; +303 bytes on every wrapped work prompt; storage failure warns
instead of refusing. **Tests that must change:** the six in §2, plus a new `--no-mail` parser
test, a degrade test, and `tests/test_mail_gating.py:165-209` if the warning becomes once-per-run.

---

## 5. Risks

**Prompt-size regression.** The only way a flipped default breaks a working command line
today is the 100 KiB argv guard. Narrow — a prompt must land within 303 bytes of the limit —
but it is a hard `prompt_too_large` refusal, and it applies to kimi permanently. Mitigation:
none needed beyond a CHANGELOG line; `--no-mail` is the escape hatch.

**Unwritable or unusual `.delegate`.** Item 4 above is the whole mitigation. Without it the
flip converts a benign filesystem condition into a launch refusal for users who never asked
for mail. Do not ship the flip without the degrade.

**Prompt-behavior drift.** 301 chars telling every work lane it has a mailbox will change
model behavior on some runs, including in repos where nobody will send mail. That is the
actual cost of "always on"; `--no-mail` and an explicit `"enabled": false` are the outs.

**In-flight workflows are safe.** A pinned workflow attempt reuses its frozen config
(`config.py:1493-1506`), so a running workflow does not change default mid-flight.

**Machines without `post`.** Unaffected, before and after. Mail never invokes `post`. If the
goal is that they stay unaffected, the correct action is to change nothing about `notify.py`.

**One decision the coordinator needs from Trey.** His words were "messaging/comms is always
on." That reads two ways: (a) delegate's lane-to-coordinator mailbox on by default, which is
everything above; or (b) that *plus* `--notify` firing by default so completions ring `post`.
(b) is a different feature with a different cost. Delegate cannot infer a target — `--notify`
requires an explicit `room:`/`channel:`. It *is* mechanically derivable, since `post rooms`
maps room names to workspace paths and the source workspace is known, but that would put a
`post` subprocess on the default terminal path of every tracked run and would break the
"machine without `post` is completely unaffected" property that mail currently has for free.
**Recommendation: scope Lane M to (a) only, and leave `--notify` explicit.** If Trey wants
(b), it should be its own key (`notify.defaultTarget`) and its own decision.
