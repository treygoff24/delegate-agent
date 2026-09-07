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
   Merge order R, D, B, S, E, A, M into feat/harness-compat-audit; scripts/gate.sh per merged checkpoint (`delegate codex safe --model astra --reasoning-effort medium`), patch
4. [ ] implement in waves (disjoint files per lane), each lane verified
5. [ ] cross-model review + fix loop
6. [ ] gate: `tests/acceptance.sh` / `python3 -m pytest -q` + ruff
7. [ ] commit, push origin, final report + PushNotification

## Rulings
- Lane M scope = mail.md proposal (a): workspace mailbox default on, global `--no-mail`, storage failure degrades, warning once per run, dead-code cleanups; `--notify` stays explicit and `--mail-push` opt-in. Reason: Trey's words "messaging/comms always on" map to the mailbox; auto-notify would put a `post` subprocess on every run's terminal path. Cost if wrong: Trey wanted completions to ring post too — one more key (`notify.defaultTarget`) later.
- Astra review dispositions: see PLAN.md v2 Rulings.
- Lane D merged at ba9a5c1 after re-running its focused tests here; its two full-suite failures were mise stderr noise in the Sol child env (pass under my shell).

## Re-arm
ScheduleWakeup fallback 1500s with prompt "babysit tick: overnight harness-compat run". Subagent
completions re-invoke the session automatically.
