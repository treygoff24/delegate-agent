# STATE — delegate-agent

Updated: 2026-09-28 (papercuts report written; diagnosis only, nothing fixed)

## Where things stand

- `main` at `2721286f` (Claude engine model table: Sonnet 5.5). Version 0.31.0 on the Mac and the devbox agent cell.
- Mac runtime `~/.delegate/src` is a symlink into release `6393bbe6…` (checked 2026-09-28). The 2026-09-25 note that the Mac was not promoted is out of date.
- The devbox checkout was one commit behind `main` on 2026-09-28 (`027d79f`).
- Papercuts report for the whole tool: `docs/audits/2026-09-28-papercuts/REPORT.md`, with ten family diagnoses in `appendix/`. Built from 752 delegate cuts across Mac and devbox ledgers; 10 agents read the code at 0.31.0. Trey read it; the title has a stray "a" ("aDelegate") from his edit, left as is.
- One diagnosing agent accidentally launched a real Codex Run (`codex-1117`, prompt "hello", no changes). It finished cleanly.

## Open loops

- Decisions for Trey (write guard scope, degraded flag, resumable default, workflow repin, others): bead `dlg-dui` (blocks `dlg-3pl`).
- Follow-up work in the report's order: bead `dlg-3pl`.
- Five small live bugs (mail-push flag, notify flag, `install-local` skill, Python 3.11 guard, Codex profile setting): bead `dlg-42q`. `dlg-4l5` is reportedly fixed by `70c3203d`; verify and close.
- The `install-local` skill still tells agents to rsync into the live release directory. Do not follow it until `dlg-42q` item 3 lands.
- The estate launcher and `bin/delegate-profile-shim` still disagree on `DELEGATE_CONFIG` versus `--auth-profile`; aligning them is a linux-devbox change.

## Next

Trey picks the decisions in `dlg-dui`. Then start with the independent pieces: `dlg-42q`, the ledger sweep (close stale cuts with the fixing commit), and the docs-versus-parser conformance test.

## Earlier records

Everything before 2026-09-28 (describe DSL, bwrap fixture, devbox promotion, GPT-6 rows, gate counts) is in `docs/handoffs/2026-09-25-state-record.md`, plus `CHANGELOG.md` and git history.
