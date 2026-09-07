# Issues hit running a research workflow from Claude Code (2026-09-03)

Session: `~/Code/fin-model`, Claude Code (Fable), running a 21-child `delegate workflow`
(10 research → 10 verify → 1 synthesis) plus standalone `omp` and `codex` lanes. Workflow
`wf_0171f79a04f9`, journal at `fin-model/.delegate/workflows/wf_0171f79a04f9/journal.jsonl`.
Bugs first, then inefficiencies. No fixes proposed; codebase not explored. Papercuts filed
in-session are cross-referenced by id.

## Bugs

### 1. Workflow supervisor dies with "cancellation requested during pipeline" — three times, three launch modes

`status.json` → `"error": "workflow supervisor watchdog: cancellation requested during pipeline"`,
traceback ends in `runtime.py:1824 pipeline → SupervisorWatchdogExit`.

| # | At (UTC) | How launched | What was happening on the harness side |
|---|---|---|---|
| 1 | 22:15:15 | `delegate --json workflow run script.py --budget 25` from a foreground Claude Code Bash call | A backgrounded `delegate workflow wait <wfId> --timeout 7200` Bash task was killed by a user interrupt at the same second |
| 2 | 22:26:38 | `delegate --json workflow run --resume <wfId>` from a foreground Bash call | User interrupt on the harness turn; **no** wait client running (only a passive `cat status.json` loop) |
| 3 | 22:27:57 | Same resume, but `fork()` + `os.setsid()` + `execvp` so the supervisor had its own session | 44 s after launch; no interrupt, no wait client. Nothing on the harness side coincided |

Failure 3 rules out "harness interrupt reaches the supervisor's process group" as the whole story.
Whatever requests the cancellation is not documented; the journal records only the watchdog event,
with no `cancel_requested` event, source, or signal number preceding it.
Papercuts: `pc2_6fddf27d4b0e9ca7`, `pc2_f7c64c9262286753`.

### 2. Children keep running after the supervisor dies, but are reported `stale`, and resume respawns instead of adopting them

After each failure, `delegate runs --group <wfId>` showed the in-flight children as `stale` while
their processes were alive and writing output (research files landed on disk minutes after the
supervisor died). On resume 2 the journal shows `agent_cache_hit` for every finished research lane
(good) and then `agent_started` for six verify lanes whose first-round children (`codex-20…27`) were
still running — i.e. duplicate lanes on the same prompt writing to the same output file. The skill
text says resume "adopts matching children that already exist"; it did not here, presumably because
they were marked stale rather than running.

### 3. `delegate omp` fails with no auth precheck; `delegate models` shows aliases with no auth state

Two `omp work --model glm` workflow lanes died in ~60 s with
`error: No API key found for opencode-go. Use /login, set an API key environment variable, or create
~/.omp/profiles/work/agent/agent.db`. `kimi` failed the same way. `cursor` failed with
`Authentication required. Run 'agent login'` — visible only via `delegate --json models cursor --live`
(`"live": false` plus a warning string). `delegate --json models` without `--live` lists every alias
as if usable. There is no `delegate doctor` and the dry run reports routes as valid for unauthenticated
engines. Papercut `pc2_113ee007c75d300f`.

### 4. Exhausted subscription quota surfaces as `no_assistant_text`, and the fallback chain did not carry it

After the OpenCode Go key was provisioned, `omp --profile work usage` showed the sub at
**100% monthly, resets in 16d**. `delegate omp call --model glm` then returned
`status=failed resultQuality=no_assistant_text text=''` with the only warning being the generic
"Structured child stdout contained no assistant text; suppressed raw event output." No mention of a
429/quota. The OMP `retry.fallbackChains` entry `opencode-go/glm-5.3: [openrouter/z-ai/glm-4.6]`
either did not fire or fired to a stale target; the delegate envelope gives no way to tell.

### 5. `delegate run-output <engine>` bare-name resolution picks a stale run

Immediately after a failed `omp call`, `delegate run-output omp --stderr` resolved to `omp-4`, an
earlier run (`del_20260903T221053Z_74dc71`), not the one that just failed. `delegate --json runs`
did not let me find the newest run either: the call run either was not listed or carried no
`createdAt`/`startedAt` I could sort on. I never located that run's stderr.

### 6. `run-output --stderr` for OMP children is a wall of minified JS

The useful line (`error: No API key found for opencode-go.`) sits at the end of a ~2 KB minified
`cli.js` stack dump (`#dn (/…/pi-coding-agent/dist/cli.js:16919:105)`). The completion report's
"Redacted stderr tail" carries the same noise.

### 7. Inconsistent result field names between run kinds

The JSON envelope for `codex safe` had `assistantText` (16 KB, `assistantTextChars`,
`assistantTextTruncated`) and no `text`; `omp call` had `text` and `textChars`. My first save of
the review wrote an empty file because I read `text`. `textChars` was `None` on the safe run.

### 8. `workflow run` JSON envelope has no status field

Launch and resume both returned `{"ok": true, "wfId": …, "journalPath": …, "scriptPath": …}` with
no `status`. `--json workflow status` is a second call.

### 9. `delegate followup` — noted from the skill, not hit here

The skill text records `delegate followup` failing (`codex exec resume` rejects `--cd`,
`pc2_9b59c000f14d3364`). I launched the synthesis lane `resumable=True` intending to follow it up
when late lanes landed; if that rake is still open the plan would have failed. Flagging so the two
issues are linked.

## Inefficiencies and annoyances

- **`delegate workflow wait` in a harness background task is a footgun.** Claude Code's Bash
  timeout caps at 600 s, so a 7200 s wait is killed every ten minutes, and (issue 1) killing it
  coincided with the supervisor dying. The safe pattern that worked was a shell loop reading
  `status.json` — which the skill does not mention.
- **No `workflow status` view of child liveness.** `status: failed` + `runs --group` showing
  `stale` gave no way to know whether children were alive short of `ps` and watching the output
  directory. "stale" needs a definition (heartbeat lost? process gone?).
- **Workflow `check`/`--dry-run` say nothing about auth**, so a 21-child launch burned two lanes and
  a budget slot on a dead engine that a one-line precheck would have caught.
- **OMP alias table in `delegate-agent` skill is stale relative to reality**: `gemini` alias line
  says 3.8 but `delegate --json models` prints `google/gemini-3.7-flash`; GLM is described as the
  fleet's first-pick reviewer while its only route is an exhausted sub with a fallback to
  `glm-4.6`. Fireworks has `glm-5p3` / `glm-5p3-flash` / `kimi-k3` / `qwen3p8-max` and none are in
  any chain except deepseek.
- **Budget accounting on failed children**: two omp lanes that died in 60 s consumed budget claims
  and their retries; after the resume the pool read `spent 18 / 25` with the synthesis still to run.
  Whether an instantly-failed child should count is a policy question; today it silently does.
- **`--group` is a global flag** (before the subcommand); every first attempt puts it after. Minor,
  recurring.
- **`omp call` probe cost**: the cheapest way to learn whether an alias is authenticated is to
  spend a real call on it. A `models --live` that actually pings auth for OMP, the way it does for
  cursor, would save that.
- **Completion-report "Next actions" suggested `delegate --cwd … run-output omp-1 --stderr --tail
  80`** — a command that (issue 5) resolved to the wrong run when I ran the bare-name form.

## Adjacent (estate, not delegate)

- `aiw`/`aip` from the devbox quickstart are zsh functions, not on PATH in the agent shell;
  `estate-exec` works. Cost one failed call.
- OpenCode Go's key had to be added to `~/.ai-profiles/managed-env-vars.zsh`
  (`AI_PROFILE_PROVIDER_KEYS`) and the realm `keys.zsh` by hand; there is no `estate-harness
  add-key <NAME>` and the OMP env-var name (`OPENCODE_API_KEY`) had to be grepped out of
  `pi-coding-agent/dist/cli.js`.
