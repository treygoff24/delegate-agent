"""Advisory per-engine model ID tables. Harness enumeration is the source of truth."""

from __future__ import annotations

from typing import TypedDict


class BundledModelEntry(TypedDict, total=False):
    id: str
    note: str


BUNDLED_MODELS: dict[str, tuple[BundledModelEntry, ...]] = {
    "codex": (
        {"id": "gpt-5.6-sol"},
        {"id": "gpt-5.5"},
        {"id": "gpt-5.4"},
        {"id": "gpt-5.4-mini"},
        {"id": "gpt-5.3-codex-spark"},
    ),
    "droid": (
        {"id": "claude-opus-4-8"},
        {"id": "claude-sonnet-4-6"},
        {"id": "gpt-5.5"},
    ),
    "devin": (
        {"id": "adaptive"},
        {"id": "claude-fable-5"},
        {"id": "claude-haiku-4.5"},
        {"id": "claude-opus-4.5"},
        {"id": "claude-opus-4.6"},
        {"id": "claude-opus-4.7"},
        {"id": "claude-opus-4.8"},
        {"id": "claude-sonnet-4.5"},
        {"id": "claude-sonnet-4.6"},
        {"id": "claude-sonnet-5"},
        {"id": "deepseek-v4-pro"},
        {"id": "gemini-3-flash"},
        {"id": "gemini-3.1-pro"},
        {"id": "gemini-3.6-flash"},
        {"id": "glm-5.2"},
        {"id": "gpt-5.2"},
        {"id": "gpt-5.3-codex"},
        {"id": "gpt-5.4"},
        {"id": "gpt-5.4-mini"},
        {"id": "gpt-5.5"},
        {"id": "kimi-k2.6"},
        {"id": "kimi-k2.7"},
        {"id": "swe-1.5"},
        {"id": "swe-1.6"},
        {"id": "swe-1.6-fast"},
        {"id": "swe-1.7"},
        {"id": "swe-1.7-lightning"},
    ),
    "cursor": (
        {"id": "composer-2.5"},
        {"id": "grok-4.5-xhigh"},
        {"id": "grok-4.5-fast-xhigh"},
        {"id": "gpt-5.5-high"},
        {"id": "claude-opus-4-8-thinking-high"},
    ),
    "claude": (
        {"id": "claude-opus-4-8"},
        {"id": "claude-sonnet-4-6"},
        {"id": "claude-haiku-4-5"},
        {"id": "claude-fable-5"},
    ),
    "grok": (
        {"id": "swe-1.7"},
        {"id": "grok-4.5"},
        {"id": "grok-4.5-fast"},
    ),
    "kimi": (
        {"id": "kimi-code/k3"},
        {"id": "kimi-code/kimi-for-coding"},
        {"id": "kimi-code/kimi-for-coding-highspeed"},
    ),
    "opencode": (
        {"id": "opencode/claude-opus-4-5"},
        {"id": "opencode/gpt-5"},
        {"id": "anthropic/claude-sonnet-4-5"},
        {"id": "openai/gpt-5"},
    ),
    "pi": (
        {"id": "openai-codex/gpt-5.6-sol"},
        {"id": "anthropic/claude-opus-4-8"},
        {"id": "anthropic/claude-sonnet-5"},
    ),
    "omp": (
        {"id": "openai-codex/gpt-5.6-sol"},
        {"id": "anthropic/claude-opus-4-8"},
        {"id": "anthropic/claude-sonnet-5"},
    ),
}
