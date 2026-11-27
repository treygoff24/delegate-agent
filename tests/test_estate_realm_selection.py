import json
import os
import subprocess
import sys
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[1] / "bin" / "delegate.py"


def resolve(home, *args, **env):
    return subprocess.run(
        [sys.executable, str(ENTRY), "--_estate-selection", *args],
        env={"HOME": str(home), "PATH": os.defpath, **env},
        cwd=home,
        check=False,
        capture_output=True,
        text=True,
    )


def config(home, realm):
    path = home / ".delegate" / f"config.{realm}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"profiles": {"default": realm}}))
    return str(path)


def test_explicit_config_selects_realm_over_stale_inheritance(tmp_path):
    personal = config(tmp_path, "personal")
    result = resolve(
        tmp_path, "profiles", DELEGATE_CONFIG=personal, DELEGATE_PROFILE="work", AI_PROFILE="work"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["personal", personal]


def test_explicit_flag_selects_realm_even_after_subcommand(tmp_path):
    work = config(tmp_path, "work")
    result = resolve(tmp_path, "profiles", "--auth-profile", "work", AI_PROFILE="personal")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["work", work]


def test_conflicting_explicit_realm_inputs_refuse_before_dispatch(tmp_path):
    personal = config(tmp_path, "personal")
    result = resolve(tmp_path, "--auth-profile", "work", "profiles", DELEGATE_CONFIG=personal)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "contradictory realm" in result.stderr


def test_conflicting_inherited_realms_refuse_without_explicit_choice(tmp_path):
    result = resolve(tmp_path, "profiles", DELEGATE_PROFILE="work", AI_PROFILE="personal")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "contradictory realm" in result.stderr


def test_conflicting_repeated_flags_do_not_use_last_one(tmp_path):
    result = resolve(tmp_path, "--auth-profile", "work", "profiles", "--auth-profile", "personal")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "contradictory realm" in result.stderr


def test_estate_preflight_preserves_json_usage_errors(tmp_path):
    result = resolve(tmp_path, "--json", "--bad-flag", "profiles")
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"] == "unknown_option"
