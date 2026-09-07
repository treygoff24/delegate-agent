from delegate_agent.bundled_models import BUNDLED_MODELS
from delegate_agent.reasoning import BUNDLED_REASONING_CAPABILITIES


def _ids(engine: str) -> tuple[str, ...]:
    return tuple(entry["id"] for entry in BUNDLED_MODELS[engine])


def test_codex_bundled_models_match_reconciled_catalog() -> None:
    assert _ids("codex") == (
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.2",
    )
    assert "gpt-5.3-codex-spark" not in _ids("codex")


def test_codex_bundled_reasoning_matches_reconciled_catalog() -> None:
    low_to_ultra = ("low", "medium", "high", "xhigh", "max", "ultra")
    low_to_max = low_to_ultra[:-1]
    low_to_xhigh = low_to_max[:-1]
    assert BUNDLED_REASONING_CAPABILITIES["codex"] == {
        "gpt-6-astra": {"supported": low_to_ultra, "default": "low"},
        "gpt-5.6-sol": {"supported": low_to_ultra, "default": "low"},
        "gpt-5.6-terra": {"supported": low_to_ultra, "default": "medium"},
        "gpt-5.6-luna": {"supported": low_to_max, "default": "medium"},
        "gpt-5.5": {"supported": low_to_xhigh, "default": "medium"},
        "gpt-5.4": {"supported": low_to_xhigh, "default": "medium"},
        "gpt-5.4-mini": {"supported": low_to_xhigh, "default": "medium"},
        "gpt-5.2": {"supported": low_to_xhigh, "default": "medium"},
    }


def test_claude_and_grok_bundled_models_include_current_catalog_rows() -> None:
    claude_ids = set(_ids("claude"))
    assert {"claude-opus-5", "claude-sonnet-5", "claude-fable-5-1"} <= claude_ids
    assert _ids("grok") == ("grok-4.6", "grok-4.5")
    assert "swe-1.7" not in _ids("grok")
