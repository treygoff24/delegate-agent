# dlg-8oc — `workflow --dry-run` hangs forever on gated plans

Live capture, 2026-08-27 ~15:49Z. Process had been hung **38+ minutes** when caught
(started 15:11Z), PID 4082512, launched from
`~/Code/writing-plans/.worktrees/plan-v2-hardening`:

    bin/plan-lint  docs/plans/2026-08-27-v2-hardening-plan.md &&
    bin/plan-to-workflow docs/plans/2026-08-27-v2-hardening-plan.md -o "$T/wf.py" &&
    delegate workflow run "$T/wf.py" --dry-run

## Root cause

`--dry-run` short-circuits `agent()` inside the delegate runtime, but **the runtime
never tells the script it is a dry run**, and the human-approval gate lives entirely
in the *generated* script. So the gate blocks on a human decision that cannot arrive.

Deadlock shape from the py-spy dump (`py-spy-dump.txt`, 37 threads):

- MainThread — `pipeline()` (`runtime.py:570`) → `join()`, waiting on every item thread.
- Threads 4, 8, 12, 13 — `await_decision` (generated script :3782), polling
  `STATE["decisions"]` on a `threading.Event().wait(park_poll_seconds())` loop
  with no deadline and no dry-run escape.
- ~29 remaining threads — `wait_for_deps` (:2211), blocked on dependencies that
  can never complete because the four gate threads never return.

`grep -i dry generated-wf.py` finds **zero** references to the runtime's dry-run mode
(the handful of `dry` hits are an unrelated legacy ADJUDICATION contract key). The
generated script has no concept of dry-run at all.

Confirming detail: kernel state is `futex_wait_queue` with **no child processes** and
stdin on `/dev/null` — the hang is an internal lock wait, not a stalled subprocess or
a prompt waiting on a terminal.

## Why this is delegate's bug, not just the generator's

A `--dry-run` that can block forever is a broken contract: dry-run must be provably
terminating. Two fixes, and the durable answer is both:

1. **Expose dry-run to the script** — surface it as a runtime global alongside
   `args`/`budget`, so a generated script can auto-continue its human gates.
2. **Runtime deadlock guard** — under dry-run, a park/gate wait must abort rather
   than poll forever. `await_decision` already checks `ABORT_EVENT` and unwinds
   cleanly, so a runtime-side dry-run abort would resolve this exact deadlock with
   no change to the generated script.

Fix (2) alone unhangs it; fix (1) makes dry-run semantically correct (gates should
report as "would prompt", not silently auto-approve).

## Files

- `py-spy-dump.txt` — full 37-thread stack dump, the primary evidence.
- `process-state.txt` — pid/ppid/wchan/syscall/cwd for both processes.
- `generated-wf.py` — the 4504-line generated workflow (was in `/tmp`, would not
  have survived a reboot). `await_decision` at :3749, `wait_for_deps` at :2208.
- `trigger-plan.md` — the v2-hardening plan that generated it.

Process killed after capture, on Trey's instruction.
