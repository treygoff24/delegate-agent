# Deferred follow-ups

Work items reviewed and deliberately parked. Newest first.

## 2026-07-16 — from the kimi lane audit-fix change set

- **Unify workflow `agent(timeout=)` with tracked-run deadline semantics.**
  Workflow agent timeouts currently bound only the outer `delegate run`
  subprocess (`workflows/runtime.py`, `communicate()` +
  `cancel_workflow_agent_child`); the child run never sees or reports a
  `call_timeout`. Now that tracked safe/work runs honor request timeouts
  end-to-end, workflows should pass `timeout` into the input-json payload so
  there is one deadline semantics (and one error code) across direct and
  workflow launches. Flagged by both independent review lanes.

- **Probe `kimi --auto --prompt` as a child-side safe-mode boundary.** kimi
  safe mode is currently enforced by workspace isolation plus an advisory
  prompt prefix only — the kimi CLI rejects `--plan`/`--yolo` in prompt mode,
  but `--auto` is untested there. Probe in a disposable directory (never a
  repo); if it cleanly gates approvals in prompt mode, propose adding it to
  the kimi safe argv (`argv_builders.py`) as a separate change.

- **kimi token/cost accounting** — blocked upstream: kimi 0.26.0 emits no
  usage/token lines in stream-json, so `usage: unavailable` is expected.
  Revisit when the CLI emits usage data.

- **kimi stream-shape drift detection** — the checked-in stream-json fixtures
  are regression coverage of the 0.26.0 vocabulary only; they cannot notice
  new upstream event shapes. Optional: a version-pinned live contract probe
  outside the unit suite.
