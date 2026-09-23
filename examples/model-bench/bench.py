#!/usr/bin/env python3
"""model-bench launcher: collect benchmarks through a Delegate workflow, then combine rows.

  python3 bench.py collect [--priority N] [--only ID ...] [--dry-run]
  python3 bench.py combine
  python3 bench.py report [--date YYYY-MM-DD]

`collect` reads manifest.jsonl and the brief templates, and launches workflow.py with
everything passed through --args. `combine` validates every data/<id>/rows.jsonl against
SCHEMA.md's field list, applies models.json aliases, and writes combined.jsonl.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

ROW_FIELDS = [
    "benchmark_id",
    "benchmark_version",
    "subset",
    "category",
    "model_raw",
    "model_id",
    "effort",
    "harness",
    "metric",
    "value",
    "unit",
    "higher_is_better",
    "cost_usd",
    "cost_basis",
    "time_s",
    "time_basis",
    "ci",
    "n",
    "measured_on",
    "reporter",
    "source_url",
    "retrieved_at",
    "license",
    "notes",
]

DEFAULT_ROUTES = {
    "collect": {"engine": "codex", "model": "luna", "effort": "xhigh"},
    "verify": {"engine": "codex", "model": "luna", "effort": "xhigh"},
}


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{number}: invalid JSON: {exc}") from exc
    return rows


def cmd_collect(opts: argparse.Namespace) -> int:
    manifest = read_jsonl(HERE / "manifest.jsonl")
    items = [m for m in manifest if int(m.get("priority", 3)) <= opts.priority]
    if opts.only:
        wanted = set(opts.only)
        items = [m for m in manifest if m["benchmark_id"] in wanted]
    if not items:
        print("no benchmarks selected", file=sys.stderr)
        return 1
    args = {
        "date": opts.date,
        "items": [
            {"benchmark_id": m["benchmark_id"], "item_json": json.dumps(m, indent=2)} for m in items
        ],
        "collect_template": (HERE / "briefs" / "collect.md").read_text(encoding="utf-8"),
        "verify_template": (HERE / "briefs" / "verify.md").read_text(encoding="utf-8"),
        "routes": json.loads(opts.routes) if opts.routes else DEFAULT_ROUTES,
    }
    for item in items:
        (HERE / "data" / item["benchmark_id"]).mkdir(parents=True, exist_ok=True)
        (HERE / "raw" / item["benchmark_id"]).mkdir(parents=True, exist_ok=True)
    budget = opts.budget or 2 * len(items) + 10
    cmd = ["delegate", "--json", "--cwd", str(REPO), "workflow", "run"]
    cmd += [str(HERE / "workflow.py"), "--args", json.dumps(args), "--budget", str(budget)]
    if opts.dry_run:
        cmd.append("--dry-run")
    print(f"launching {len(items)} benchmarks, budget {budget}", file=sys.stderr)
    return subprocess.run(cmd, check=False).returncode


LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_WORDS = set(LEVELS) | {
    "none", "non", "thinking", "reasoning", "adaptive", "effort", "extended", "mode",
}  # fmt: skip
# Names where a level word is part of the product name, not an effort setting.
LEVEL_IN_NAME = re.compile(r"^(qwen[\w-]*-max|gemini-[\w-]*-ultra)$")
# Family prefix -> vendor, so one model gets one id whatever vendor string a source used.
VENDORS = (
    (r"^(gpt|o\d|codex|chatgpt)", "openai"),
    (r"^claude", "anthropic"),
    (r"^grok", "xai"),
    (r"^(gemini|gemma)", "google"),
    (r"^deepseek", "deepseek"),
    (r"^glm", "zhipu"),
    (r"^kimi", "moonshot"),
    (r"^(qwen|qwq)", "alibaba"),
    (r"^minimax", "minimax"),
    (r"^mimo", "xiaomi"),
    (r"^(muse|llama)", "meta"),
    (r"^(mistral|devstral|magistral|codestral)", "mistral"),
)
# Compact key (lowercase alphanumerics, effort words removed) -> canonical fleet id.
FLEET_KEYS = {
    "gpt6astra": "openai/gpt-6-astra",
    "gpt6sol": "openai/gpt-6-sol",
    "gpt6luna": "openai/gpt-6-luna",
    "claudefable51": "anthropic/claude-fable-5-1",
    "fable51": "anthropic/claude-fable-5-1",
    "claudeopus55": "anthropic/claude-opus-5-5",
    "opus55": "anthropic/claude-opus-5-5",
    "grok47": "xai/grok-4.7",
    "deepseekv41flash": "deepseek/deepseek-v4.1-flash",
    "gemini38flash": "google/gemini-3.8-flash",
    "glm53": "zhipu/glm-5.3",
    "kimik3": "moonshot/kimi-k3",
    "qwen38max": "alibaba/qwen3.8-max",
    "minimaxm3": "minimax/minimax-m3",
    "mimov26pro": "xiaomi/mimo-v2.6-pro",
    "musespark13": "meta/muse-spark-1.3",
}


def normalize_effort(words: list[str]) -> str | None:
    """Map effort words to one level, 'none' for non-reasoning, or None if unstated."""
    for word in words:
        if word in LEVELS:
            return word
    joined = " ".join(words)
    if "non" in words or "none" in words or "nonthinking" in joined or "nonreasoning" in joined:
        return "none"
    return None


def split_effort(name: str) -> tuple[str, str | None]:
    """Split a model name into (base name, effort level or None)."""
    name = name.lower()
    hints = re.findall(r"\(([^)]*)\)|\[([^\]]*)\]", name)
    hint_words = [w for pair in hints for part in pair for w in re.split(r"[^a-z0-9]+", part) if w]
    name = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name)
    tokens = [t for t in re.split(r"[\s/_:.-]+", name) if t]
    trailing = []
    while tokens and tokens[-1] in EFFORT_WORDS and not LEVEL_IN_NAME.match("-".join(tokens)):
        trailing.insert(0, tokens.pop())
    return "-".join(tokens), normalize_effort(trailing + hint_words)


def vendor_for(base: str, fallback: str) -> str:
    for pattern, vendor in VENDORS:
        if re.match(pattern, base):
            return vendor
    return fallback or "unknown"


def canonicalize(row: dict, aliases: dict[str, str]) -> tuple[str, str | None]:
    """Return (canonical model id, effort level) for a row."""
    model_id = str(row.get("model_id") or "")
    given = normalize_effort(re.split(r"[^a-z0-9]+", str(row.get("effort") or "").lower()))
    if model_id in aliases:
        return aliases[model_id], given
    vendor, _, name = model_id.rpartition("/")
    for candidate in (name, str(row.get("model_raw") or "")):
        base, suffix = split_effort(candidate)
        key = re.sub(r"[^a-z0-9]", "", base)
        if key in FLEET_KEYS:
            return FLEET_KEYS[key], given or suffix
    raw = str(row.get("model_raw") or "")
    base, suffix = split_effort(name or raw)
    return f"{vendor_for(base, vendor)}/{base}", given or suffix or split_effort(raw)[1]


def cmd_combine(_opts: argparse.Namespace) -> int:
    aliases_path = HERE / "models.json"
    aliases = json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.exists() else {}
    combined, problems = [], []
    for rows_path in sorted((HERE / "data").glob("*/rows.jsonl")):
        for number, row in enumerate(read_jsonl(rows_path), 1):
            missing = [f for f in ROW_FIELDS if f not in row]
            if missing:
                problems.append(f"{rows_path.parent.name}:{number}: missing {','.join(missing)}")
            if not isinstance(row.get("value"), (int, float)) or isinstance(row.get("value"), bool):
                problems.append(f"{rows_path.parent.name}:{number}: non-numeric value")
                continue
            row["model_canonical"], row["effort_norm"] = canonicalize(row, aliases)
            combined.append(row)
    out = HERE / "combined.jsonl"
    with out.open("w", encoding="utf-8") as handle:
        for row in combined:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    benchmarks = len({r["benchmark_id"] for r in combined})
    print(f"{len(combined)} rows from {benchmarks} benchmarks -> {out.name}")
    for line in problems[:50]:
        print(f"problem: {line}", file=sys.stderr)
    if len(problems) > 50:
        print(f"... {len(problems) - 50} more problems", file=sys.stderr)
    return 0


NON_QUALITY = (
    "price",
    "cost",
    "latency",
    "tokens_per_second",
    "ttft",
    "throughput",
    "wall_clock",
    "speed",
)

# Canonical ids (models.json targets) for the models a Delegate fleet actually routes to.
FLEET = [
    "openai/gpt-6-astra",
    "openai/gpt-6-sol",
    "openai/gpt-6-luna",
    "anthropic/claude-fable-5-1",
    "anthropic/claude-opus-5-5",
    "xai/grok-4.7",
    "deepseek/deepseek-v4.1-flash",
    "google/gemini-3.8-flash",
    "zhipu/glm-5.3",
    "moonshot/kimi-k3",
    "alibaba/qwen3.8-max",
    "minimax/minimax-m3",
    "xiaomi/mimo-v2.6-pro",
    "meta/muse-spark-1.3",
]


NOT_HEADLINE = (
    "calibration",
    "evasive",
    "denial",
    "error_rate",
    "delta",
    "retries",
    "tokens",
    "share",
)
HEADLINE = re.compile(r"overall|index|average|headline|expected_performance|elo")


def primary_groups(rows: list[dict], manifest: dict[str, dict]) -> dict[str, list[dict]]:
    """One quality metric group per benchmark.

    A manifest `primary` of {"subset", "metric"} picks the group; "mean" averages every
    quality metric per configuration. Otherwise prefer a headline-named metric, then
    the group covering the most models. Superseded and usage-only benchmarks are skipped.
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        metric = str(row.get("metric", "")).lower()
        if any(word in metric for word in NON_QUALITY + NOT_HEADLINE):
            continue
        entry = manifest.get(row["benchmark_id"], {})
        if entry.get("superseded_by") or "usage" in entry.get("categories", []):
            continue
        key = (row["benchmark_id"], row.get("benchmark_version"), row.get("subset"), row["metric"])
        groups.setdefault(key, []).append(row)
    by_bench: dict[str, list[tuple]] = {}
    for key in groups:
        by_bench.setdefault(key[0], []).append(key)
    chosen: dict[str, list[dict]] = {}
    for bid, keys in by_bench.items():
        spec = manifest.get(bid, {}).get("primary")
        if spec == "mean":
            chosen[bid] = mean_group([row for key in keys for row in groups[key]])
            continue
        if isinstance(spec, dict):
            keys = [
                k for k in keys if k[2] == spec.get("subset") and k[3] == spec.get("metric")
            ] or keys

        def rank(key: tuple) -> tuple[bool, int]:
            label = f"{key[2] or ''} {key[3]}".lower()
            return bool(HEADLINE.search(label)), len({m["model_canonical"] for m in groups[key]})

        chosen[bid] = groups[max(keys, key=rank)]
    return chosen


def mean_group(rows: list[dict]) -> list[dict]:
    """Average each (model, effort) configuration's values across metrics into one row."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        buckets.setdefault((row["model_canonical"], row.get("effort_norm") or ""), []).append(row)
    merged = []
    for members in buckets.values():
        signed = [m["value"] if m.get("higher_is_better", True) else -m["value"] for m in members]
        merged.append({**members[0], "metric": "mean", "subset": None,
                       "value": sum(signed) / len(signed), "higher_is_better": True})  # fmt: skip
    return merged


def config_scores(members: list[dict]) -> dict[tuple[str, str], dict]:
    """Best row per (model, effort) configuration."""
    configs: dict[tuple[str, str], dict] = {}
    for row in members:
        key = (row["model_canonical"], row.get("effort_norm") or "")
        sign = 1 if row.get("higher_is_better", True) else -1
        if key not in configs or sign * row["value"] > sign * configs[key]["value"]:
            configs[key] = row
    return configs


def percentiles(configs: dict[tuple[str, str], dict]) -> dict[tuple[str, str], float]:
    ordered = sorted(
        configs.items(),
        key=lambda kv: kv[1]["value"] * (1 if kv[1].get("higher_is_better", True) else -1),
        reverse=True,
    )
    n = len(ordered)
    return {key: 100.0 * (n - 1 - rank) / (n - 1) for rank, (key, _row) in enumerate(ordered)}


def pareto(configs: dict[tuple[str, str], dict], field: str) -> list[tuple[tuple, dict]]:
    """Configurations no cheaper (or faster) configuration beats on score."""
    priced = [(k, r) for k, r in configs.items() if isinstance(r.get(field), (int, float))]
    sign = {k: 1 if r.get("higher_is_better", True) else -1 for k, r in priced}
    priced.sort(key=lambda kv: (kv[1][field], -sign[kv[0]] * kv[1]["value"]))
    frontier, best = [], None
    for key, row in priced:
        score = sign[key] * row["value"]
        if best is None or score > best:
            frontier.append((key, row))
            best = score
    return frontier


def fleet_head_to_head(configs_by: dict[str, dict], manifest: dict[str, dict]) -> list[str]:
    """Rank fleet models against each other (best effort each) on benchmarks with 3+ of them."""
    by_cat: dict[str, dict[str, list[float]]] = {}
    used = []
    for bid, configs in configs_by.items():
        best: dict[str, float] = {}
        for (model, _effort), row in configs.items():
            if model in FLEET:
                value = row["value"] if row.get("higher_is_better", True) else -row["value"]
                best[model] = max(value, best.get(model, value))
        if len(best) < 3:
            continue
        used.append(bid)
        ordered = sorted(best, key=best.get, reverse=True)
        for rank, model in enumerate(ordered):
            score = 100.0 * (len(ordered) - 1 - rank) / (len(ordered) - 1)
            for cat in manifest.get(bid, {}).get("categories", []):
                by_cat.setdefault(cat, {}).setdefault(model, []).append(score)
    cats = sorted(by_cat)
    out = [
        "## Fleet head-to-head",
        "",
        f"Fleet models ranked only against each other, each at its best listed effort, on the "
        f"{len(used)} benchmarks that list at least three of them (100 = best fleet model "
        "present). This ignores retired models that crowd the all-model ranks below.",
        "",
        "| model | " + " | ".join(cats) + " |",
        "| --- |" + " --- |" * len(cats),
    ]
    for model in FLEET:
        cells = []
        for cat in cats:
            hits = by_cat[cat].get(model, [])
            cells.append(f"{sum(hits) / len(hits):.0f} (n={len(hits)})" if hits else "")
        out.append(f"| {model} | " + " | ".join(cells) + " |")
    return [*out, ""]


EFFORT_ORDER = {
    "": 0,
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}


def effort_ladders(configs_by: dict[str, dict]) -> list[str]:
    """Score, cost, and time at each effort for fleet models measured at two or more efforts."""
    out = ["## Effort ladders", ""]
    for bid, configs in sorted(configs_by.items()):
        ladders: dict[str, list[tuple[str, dict]]] = {}
        for (model, effort), row in configs.items():
            if model in FLEET:
                ladders.setdefault(model, []).append((effort, row))
        ladders = {m: steps for m, steps in ladders.items() if len(steps) >= 2}
        if not ladders:
            continue
        metric = next(iter(ladders.values()))[0][1]["metric"]
        out += [f"**{bid}** ({metric}; cost and time as reported):", ""]
        out += ["| model | effort | score | cost_usd | time_s |", "| --- | --- | --- | --- | --- |"]
        for model in FLEET:
            for effort, row in sorted(
                ladders.get(model, []), key=lambda s: EFFORT_ORDER.get(s[0], 9)
            ):
                cost, time_s = row.get("cost_usd"), row.get("time_s")
                out.append(
                    f"| {model} | {effort or '-'} | {row['value']:.3g} | "
                    f"{'' if cost is None else f'{cost:.3g}'} | {'' if time_s is None else f'{time_s:.3g}'} |"
                )
        out.append("")
    return out


def cmd_report(opts: argparse.Namespace) -> int:
    rows = read_jsonl(HERE / "combined.jsonl")
    manifest = {m["benchmark_id"]: m for m in read_jsonl(HERE / "manifest.jsonl")}
    groups = primary_groups(rows, manifest)
    pct: dict[str, dict[tuple[str, str], float]] = {}
    configs_by: dict[str, dict] = {}
    for bid, members in groups.items():
        configs = config_scores(members)
        configs_by[bid] = configs
        if len(configs) >= 5:
            pct[bid] = percentiles(configs)

    # category -> model -> list of (benchmark, best percentile over efforts)
    by_cat: dict[str, dict[str, list[tuple[str, float]]]] = {}
    for bid, scores in pct.items():
        best_per_model: dict[str, float] = {}
        for (model, _effort), value in scores.items():
            best_per_model[model] = max(value, best_per_model.get(model, 0.0))
        for cat in manifest.get(bid, {}).get("categories", []):
            for model, value in best_per_model.items():
                by_cat.setdefault(cat, {}).setdefault(model, []).append((bid, value))

    out = [f"# model-bench report, {opts.date}", ""]
    out += [
        f"{len(rows)} rows, {len(groups)} benchmarks with a quality metric, "
        f"{len(pct)} with at least five configurations (used for percentile ranks).",
        "",
        "Scores are percentile ranks within each benchmark's primary metric (100 = best "
        "configuration listed), averaged over the benchmarks in a category. `n` is how many "
        "benchmarks the model appears in; a high average on n=1 is weak evidence.",
        "",
        "## Fleet models by category",
        "",
    ]
    cats = sorted(by_cat)
    out.append("| model | " + " | ".join(cats) + " |")
    out.append("| --- |" + " --- |" * len(cats))
    for model in FLEET:
        cells = []
        for cat in cats:
            hits = by_cat[cat].get(model, [])
            cells.append(
                f"{sum(v for _b, v in hits) / len(hits):.0f} (n={len(hits)})" if hits else ""
            )
        out.append(f"| {model} | " + " | ".join(cells) + " |")
    out.append("")

    for cat in cats:
        ranked = sorted(
            ((m, hits) for m, hits in by_cat[cat].items() if len(hits) >= 2),
            key=lambda mh: sum(v for _b, v in mh[1]) / len(mh[1]),
            reverse=True,
        )[: opts.top]
        if not ranked:
            continue
        out += [
            f"## {cat}: top models (n >= 2)",
            "",
            "| model | score | n |",
            "| --- | --- | --- |",
        ]
        for model, hits in ranked:
            mark = " *" if model in FLEET else ""
            avg = sum(v for _b, v in hits) / len(hits)
            out.append(f"| {model}{mark} | {avg:.0f} | {len(hits)} |")
        out.append("")

    out += fleet_head_to_head(configs_by, manifest)
    out += effort_ladders(configs_by)
    out += ["## Cost and time Pareto frontiers", ""]
    for bid, configs in sorted(configs_by.items()):
        for field, label in (("cost_usd", "cost"), ("time_s", "time")):
            frontier = pareto(configs, field)
            if len(frontier) < 2:
                continue
            basis = frontier[0][1].get(f"{label}_basis") or "unstated basis"
            metric = frontier[0][1]["metric"]
            out.append(f"**{bid}**, {metric} vs {label} ({basis}):")
            for (model, effort), row in frontier:
                eff = f" [{effort}]" if effort else ""
                out.append(f"- {model}{eff}: {row['value']} at {row[field]}")
            out.append("")

    unmapped = sorted(
        {r["model_canonical"] for r in rows if r["model_canonical"].startswith("unknown/")}
    )
    out += [f"## Unmapped model ids ({len(unmapped)})", "", ", ".join(unmapped) or "none", ""]
    path = HERE / "reports" / f"{opts.date}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {path.relative_to(HERE)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="launch the collection workflow")
    collect.add_argument("--priority", type=int, default=3, help="collect priority <= N")
    collect.add_argument("--only", nargs="+", help="collect only these benchmark ids")
    collect.add_argument("--date", default=dt.date.today().isoformat())
    collect.add_argument("--routes", help="JSON routes overriding the Luna defaults")
    collect.add_argument("--budget", type=int)
    collect.add_argument("--dry-run", action="store_true")
    collect.set_defaults(func=cmd_collect)
    combine = sub.add_parser("combine", help="validate and combine collected rows")
    combine.set_defaults(func=cmd_combine)
    report = sub.add_parser("report", help="write reports/<date>.md from combined.jsonl")
    report.add_argument("--date", default=dt.date.today().isoformat())
    report.add_argument("--top", type=int, default=12, help="models listed per category")
    report.set_defaults(func=cmd_report)
    opts = parser.parse_args()
    return opts.func(opts)


if __name__ == "__main__":
    raise SystemExit(main())
