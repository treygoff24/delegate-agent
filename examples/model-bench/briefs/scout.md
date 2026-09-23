PRE-APPROVED: this is a research-and-write task; do it now without asking for design confirmation.

# Task: map the public AI-model benchmark landscape as of today

Today is {DATE}. Build the list of benchmarks and leaderboards worth collecting
to decide which frontier and near-frontier language models to route work to.
The list feeds a fan-out where one collector lane pulls each benchmark's data.

## What to find

Cast wide, then rank. Cover at least these areas:

- agentic and repository-level coding, terminal/CLI agent tasks, general coding
- UI, frontend, and web-app building
- knowledge work and white-collar tasks (economically valuable occupational tasks)
- legal, and finance if present
- reasoning, math, science, instruction following, tool use, long context
- speed, latency, throughput, and price (serving benchmarks)
- aggregate indexes that combine many benchmarks
- human-preference arenas
- **benchmarks that report task success together with dollar cost and/or time
  per task** (Pareto-style cost/time/quality leaderboards): these are the most
  valuable; find every one you can

Prefer benchmarks that are maintained, cover current models (including models
released in the last few weeks), and publish raw data. Note which ones are
independent versus vendor-run. Include a benchmark that is stale only when it is
still widely cited, and mark it `stale`.

## How

Use web search and open the actual leaderboard or repository page for every
entry; do not list a benchmark from memory. For each, find the raw-data source
(GitHub repo, CSV, JSON endpoint behind the page) when one exists. You may use
the shell tools `exa-agent`, `firecrawl`, and `curl` if they are available.

## Write boundary

Write only these files, inside `examples/model-bench/`:

1. `manifest.jsonl`: one line per benchmark, in the manifest format defined in
   `examples/model-bench/SCHEMA.md` (read it first). Aim for completeness over
   brevity: every benchmark a routing decision could plausibly use, with
   `priority` 1 for the ones that must be collected.
2. `data/_scout/NOTES.md`: a short research note: the landscape in a few
   paragraphs, which benchmarks matter most for which category, which ones pair
   cost/time with quality, gaps (categories with thin or vendor-only coverage),
   and the models released recently that the collectors should expect to see.

Do not edit any other file. Do not commit.

## Done when

`manifest.jsonl` parses line by line as JSON, every line has every manifest
field, every `url` was opened during this run, and the note exists. Report the
count of benchmarks by priority and category, and anything you could not reach.
