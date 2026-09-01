# Code review: delegate-agent branch feat/doctor-promote-cli

You are reviewing, not implementing. Do NOT edit any file in the repository. Do not run git commands that mutate state (no stash, checkout, restore, reset, commit). Write your report to /tmp/dlg-review/sol-review.md and nothing else.

Repository: /home/trey-agent/Code/delegate-agent (Python, `uv run --extra dev pytest`, `scripts/gate.sh` is the canonical gate).
Diff under review: `git -C /home/trey-agent/Code/delegate-agent diff main...HEAD` (two commits: 16cbd12, ae5bfc0). Read `git log main..HEAD` for the commit messages, which state intent.

## What changed

1. **Claude `--output-schema` accepted in tracked safe/work modes** (16cbd12). Previously `_validate_output_schema_mode` in request_build.py hard-refused Claude schemas outside call mode. Now `build_claude_argv` keeps `--output-format stream-json` for tracked runs and passes the inlined schema as `--json-schema`; call mode still forces `json`. Live-verified against Claude Code 2.1.257: the stream-json `result` event's `result` field is the schema-bound JSON string, so `harness_events.StreamAccumulator._ingest_result_event` records it as completion text with no change. resume_command.py now inherits inline schema text for Claude as it does for Codex.

2. **`delegate doctor` and `delegate promote` CLI commands** (ae5bfc0). workflow_pinning.py gains `live_runtime_digest()`; `doctor()` compares it to the stamped digest in ~/.delegate/last-promotion.json and warns on mismatch/missing; `emit_promote` defaults the digest to the live one. New parser `parse_runtime_subcommand` in cli_parser.py, `PromoteOptions` in request_models.py, dispatch in cli.py before workspace resolution, CommandSpecs in command_help.py, `doctor` added to READ_ONLY_SUBCOMMANDS and to bin/delegate-profile-shim's allow-list. Docs in docs/cli-reference.md and docs/live-runtime.md.

## What I want from you

Adversarial, evidence-cited review. For each finding: file:line, severity (blocker / major / minor / nit), the concrete failure scenario (inputs/state -> wrong behavior), and a proposed fix. Verify claims by reading code and, where cheap, running targeted tests (`uv run --extra dev pytest -q tests/<file>`). Do not report style preferences as findings.

Specific questions to answer, each with evidence:

A. Schema lift correctness. Are there other code paths that assumed Claude schema => call mode (snapshots, run-output, completion-report parsing, worktree manifests, resume, followup, workflow runtime, input-json path)? Does `argv_utils.py:39-40` redaction of the inlined schema still cover tracked-run manifests and dry-run argv? Does `--pass-through` (stream_capture False, output_format text) with a schema do anything surprising? Is there any place `structuredOutput`/`output_schema` metadata was only written for call mode and is now misleading for tracked runs?

B. Safe-mode boundary. Claude safe mode runs `--permission-mode plan --tools <safe list> --allowedTools ...`. Does allowing `--json-schema` weaken any safe-mode guarantee (the StructuredOutput tool is not in the allow-list; confirm it cannot be used for anything but emitting the final JSON)? Any prompt-injection surface via schema contents being inlined into argv?

C. doctor/promote. Is `live_runtime_digest()` (digest of `Path(__file__)`'s package tree plus embedded launcher/sitecustomize templates) actually the same digest that `_write_runtime_snapshot` records for pins, so a mismatch warning is trustworthy and not a tautological false positive? Does `_runtime_source_files` include files that legitimately differ between install and checkout (e.g. __pycache__, .pyc excluded — anything else like .DS_Store, editor swap files, `tests`?) that would make a correct promotion still report mismatch? Is the 0600 chmod and atomic write sound? Any race between two agents promoting concurrently?

D. Profile guard parity. `doctor` is read-only in both the Python guard and the shell shim; `promote` is a mutation in both. Confirm the shim's readonly classifier actually reaches the `doctor` case for `delegate --json doctor` and `AI_PROFILE=work delegate doctor` when the overlay is missing. Is classifying `promote` as mutation correct given it runs during upgrade windows when configs may be mid-flight — or does that make the stamp skippable in exactly the situation it exists for?

E. Tests. Are the new tests load-bearing (would they fail if the feature regressed)? Name any that pass vacuously. Are there missing tests you would require before merge?

F. Docs/help drift. `docs/cli-reference.md`, `README.md`, `command_help.py` usage strings, `render_overview_text`, and `describe --summary` catalog must agree. Report any drift.

Report format: a short verdict line first (SHIP / SHIP WITH FIXES / DO NOT SHIP), then findings ordered by severity, then answers A-F, then "What I checked and found clean" so absence of findings is distinguishable from absence of looking.
