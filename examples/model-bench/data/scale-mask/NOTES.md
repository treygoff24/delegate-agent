# MASK (Scale Labs) — SKIPPED (stale)

added by coverage audit 2026-09-22

## Status: skipped

Per the collection brief: "Only collect if it has 2026 frontier models; else
skip." MASK's board does not have the current September 2026 frontier wave,
so no `rows.jsonl` was written.

## What MASK measures

MASK evaluates honesty under pressure — whether a model contradicts its own
stated beliefs when incentivized to do so — distinct from factual accuracy
benchmarks.

## Evidence for staleness

Fetched https://labs.scale.com/leaderboard/mask with `curl -A 'Mozilla/5.0'`
and extracted the embedded `entries` array from the page's RSC flight
payload (same method as the other Scale Labs boards in this batch; raw HTML
saved under `raw/scale-mask/mask.html` for reference, in case a future pass
wants to re-check). 67 model configurations are listed.

The newest row on the board is `gemini-3.1-pro-preview`, dated
**2026-03-25** — nearly six months before this capture (2026-09-22/23). No
row after that date exists. By contrast, the six other Scale Labs boards
collected in this same batch (`scale-mcp-atlas`, `scale-rli`,
`scale-prbench`, `scale-swe-atlas`; `scale-multichallenge` and `scale-vtb`
flagged as lagging but not fully stale) all carry at least some 2026
Q1-Q2 models, and four of the six carry the September 2026 wave. MASK has
**none** of: GPT-6 (Astra/Luna/Sol), Claude Fable 5.1, Gemini 3.8 Flash,
DeepSeek V4.1, GLM 5.3, Kimi K3, Qwen 3.8, MiniMax M3, Grok 4.7, or even Muse
Spark (which appears as a top-ranked, actively-updated entry on every other
Scale board in this batch, including versions as recent as 1.3 from
2026-09-14). The absence of any Muse Spark row at all on MASK is the
strongest single signal that this board has not been re-run since well
before Muse Spark existed on Scale's other boards.

No `rows.jsonl` was written for `scale-mask`; this NOTES.md documents the
decision and the raw capture is kept under `raw/scale-mask/` in case a later
pass wants to re-check whether the board has since been refreshed.
