from delegate_agent.bundled_models import BUNDLED_MODELS
from delegate_agent.reasoning import BUNDLED_REASONING_CAPABILITIES


def _ids(engine: str) -> tuple[str, ...]:
    return tuple(entry["id"] for entry in BUNDLED_MODELS[engine])


def test_codex_bundled_reasoning_matches_reconciled_catalog() -> None:
    low_to_ultra = ("low", "medium", "high", "xhigh", "max", "ultra")
    low_to_max = low_to_ultra[:-1]
    low_to_xhigh = low_to_max[:-1]
    assert BUNDLED_REASONING_CAPABILITIES["codex"] == {
        "gpt-6-astra": {"supported": low_to_ultra, "default": "low"},
        "gpt-6-sol": {"supported": low_to_ultra, "default": "medium"},
        "gpt-6-luna": {"supported": low_to_max, "default": "medium"},
        "gpt-5.5": {"supported": low_to_xhigh, "default": "medium"},
        "gpt-5.4": {"supported": low_to_xhigh, "default": "medium"},
        "gpt-5.4-mini": {"supported": low_to_xhigh, "default": "medium"},
        "gpt-5.2": {"supported": low_to_xhigh, "default": "medium"},
    }


def test_claude_and_grok_bundled_models_include_current_catalog_rows() -> None:
    claude_ids = set(_ids("claude"))
    assert {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"} <= claude_ids
    assert "claude-sonnet-5" not in claude_ids
    assert _ids("grok") == ("grok-4.7", "grok-4.6")
    assert "swe-1.7" not in _ids("grok")
    assert "claude-opus-5" not in claude_ids
