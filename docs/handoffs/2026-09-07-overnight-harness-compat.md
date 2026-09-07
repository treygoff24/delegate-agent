# Overnight run: harness compatibility audit → plan → ship (2026-09-07)

Operator: this Claude session (Fable). Trey asleep; authorized 2026-09-07 evening to fully ship:
audit → plan → Astra medium review → patch plan → implement (delegate fleet + native subagents,
any models) → cross-model review → full gate → commit + push Forgejo. **Not** authorized: promote to
the live runtime, GitHub push, force/amend, `--no-verify`.

Also in scope (Trey, mid-turn): put the auto-injected skill-review preamble
(`prompt_instructions.py:SKILL_REVIEW_PREFIX`) behind a config toggle, default **off**.

## State
- Branch `feat/harness-compat-audit` off `main` @ 12fe7ac (main pushed to Forgejo).
- Bead: dlg-? (created 2026-09-07 "Harness compatibility audit + minimal-change plan").
- Audit reports: `docs/audits/2026-09-07-harness-compat/{claude,codex,cursor,droid,grok,devin,opencode,pi,omp,kimi,shared}.md`
  written by 11 native Opus subagents (names `audit-<harness>`). Brief: `BRIEF.md` there.
- Plan (to write): `docs/audits/2026-09-07-harness-compat/PLAN.md`.

## Phases
1. [done] audits (11 reports)
2. [done] plan
3. [done] Astra medium review → PLAN.md v2 (b44f886); Lane P landed as wave 0 (637475e); gate on that checkpoint running (log /var/tmp/gate-wave0.log)
4. [running] wave 1: worktrees .worktrees/lane-{S,E,A,R,D,B} on branches lane/<X> from 637475e. E and A = native Opus subagents (names lane-E, lane-A); S,R,D,B = `delegate codex work --model sol --reasoning-effort high --isolation none` (json outputs /var/tmp/dlg-lanes/out-<X>.json, briefs brief-<X>.md). Lane M waits on recon-mail (docs/audits/…/mail.md)
   Merge order R, D, B, S, E, A, M into feat/harness-compat-audit; scripts/gate.sh per merged checkpoint
   06:27Z: D merged (ba9a5c1), S merged (66821d6), gate on D+S running (/var/tmp/gate-wave1a.log). R (1 commit), B (2), E (9), A (4), M (relaunched 06:22Z as del_20260907T062247Z_dba8a8 after an ownership block, dlg-cjz.5 closed) still running.
   06:50Z: B merged (16d49ca), R merged (04bf703); gate on D+S+B+R: GATE PASS 3076 passed; pushed to origin. E (12 commits) and A (12) finishing; M (Astra) mid-work.
   06:53Z: M merged (5bc9e06; coordinator added the two --no-mail help/parser hunks as 100ff6e); gate on D+S+B+R+M running (/var/tmp/gate-wave1c.log). E (12 commits) and A (13) told to merge feat into their branches before final suite. Next: merge E then A, gate, wave-1 review (GLM 5.3 via omp + Cursor Grok + Opus), Lane I docs.
   07:05Z: gate D+S+B+R+M PASS 3083 (pushed 5bc9e06). E merged, A merged, no conflicts → 77ba100. Gate on full wave-1 running (/var/tmp/gate-wave1d.log). Reviews launched on 77ba100: GLM 5.3 (`delegate omp safe --model fireworks/glm-5.3`, /var/tmp/dlg-lanes/review-glm.json), Cursor Grok (`delegate cursor safe --model cursor-grok-4.6-xhigh`, review-grok.json), Opus native agent review-opus (review-opus.md). Lane I (Opus agent lane-I) in .worktrees/lane-I from 77ba100: seams, manifest fields, doctor, docs, CHANGELOG. Then: triage findings → fix lane → gate → push → final report.
   07:12Z: gate on 77ba100 PASS 3201. Lane A post-merge commits merged → 4aec1c1 (pushed); Lane I told twice to re-merge feat (shim + getattr + help strings already done upstream).
   07:18Z: Opus review in (/var/tmp/dlg-lanes/review-opus.md): 1 blocker (error text unredacted in run.completed mirror), 4 major (kimi bwrap absent-home, malformed lines suppress raw fallback, empty claude result → invalid, codex turn.completed arms 1s kill), 5 minor, 4 nits. Fix lane F (Opus agent lane-F) in .worktrees/lane-F from 4aec1c1, brief /var/tmp/dlg-lanes/brief-F.md. Ruling: F5 records the codex success terminal without arming the exit kill (no live flush-timing verification tonight). GLM + Grok reviews still running. (`delegate codex safe --model astra --reasoning-effort medium`), patch
4. [ ] implement in waves (disjoint files per lane), each lane verified
5. [ ] cross-model review + fix loop
6. [ ] gate: `tests/acceptance.sh` / `python3 -m pytest -q` + ruff
7. [ ] commit, push origin, final report + PushNotification

## Rulings
- 07:35Z Cursor safe: revert `--mode ask` for `safe` (keep for `call --read-only`). Reason: both cursor read-only modes block the shell (verified live), so reviewers lose git/pytest; the isolated copy was and remains the safe boundary. Cost if wrong: cursor safe stays prompt-enforced read-only, as before this branch.
- 07:35Z Codex turn.completed success terminal does not arm the 1 s exit kill (F5): no live flush-timing proof tonight.
- 07:30Z Live incident: Lane A's branch-code `capabilities refresh claude` (~07:00Z) wrote the shared discovery cache with a `capabilities` field the installed reader rejects → installed runtime lost every codex reasoning declaration (fin-model norem T6 failed twice, rotated to Sol). Resolved 07:24Z by fin-model's `capabilities refresh codex`. Fix F17 (tolerant reader) + promotion-checklist note (Lane I). No more cache writes from branch code tonight.
- GLM review lane died on delegate's 16 MiB stdout cap (dlg-o7i); rerun with a narrowed brief. Grok G1 (codex `-c` after `-`) and G2 (workflow child timeout) verified NOT defects.
- Lane M scope = mail.md proposal (a): workspace mailbox default on, global `--no-mail`, storage failure degrades, warning once per run, dead-code cleanups; `--notify` stays explicit and `--mail-push` opt-in. Reason: Trey's words "messaging/comms always on" map to the mailbox; auto-notify would put a `post` subprocess on every run's terminal path. Cost if wrong: Trey wanted completions to ring post too — one more key (`notify.defaultTarget`) later.
- Astra review dispositions: see PLAN.md v2 Rulings.
- Lane D merged at ba9a5c1 after re-running its focused tests here; its two full-suite failures were mise stderr noise in the Sol child env (pass under my shell).

## Re-arm
ScheduleWakeup fallback 1500s with prompt "babysit tick: overnight harness-compat run". Subagent
completions re-invoke the session automatically.
