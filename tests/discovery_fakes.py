"""Small fake-harness helpers shared by discovery integration tests."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def write_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def write_version_harness(path: Path, version: str) -> Path:
    return write_executable(path, f"print({version!r})\n")


def _write_dispatch_harness(
    path: Path, responses: dict[tuple[str, ...], tuple[str, str, int]]
) -> Path:
    body = (
        "import sys\n"
        f"responses = {responses!r}\n"
        "response = responses.get(tuple(sys.argv[1:]))\n"
        "if response is None:\n"
        "    print('unsupported fake harness argv', file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
        "stdout, stderr, returncode = response\n"
        "sys.stdout.write(stdout)\n"
        "sys.stderr.write(stderr)\n"
        "raise SystemExit(returncode)\n"
    )
    return write_executable(path, body)


def materialize_minimum_harnesses(target: Path, *, fixtures: Path = FIXTURES) -> dict[str, Path]:
    """Materialize version and metadata responses from sanitized fixtures."""
    provenance: dict[str, Any] = json.loads(
        (fixtures / "provenance.json").read_text(encoding="utf-8")
    )
    responses: dict[str, dict[tuple[str, ...], tuple[str, str, int]]] = {}
    for fixture_name, entry in provenance["fixtures"].items():
        command = entry["command"]
        binary, *arguments = command
        binary_responses = responses.setdefault(binary, {})
        binary_responses[("--version",)] = (entry["version"] + "\n", "", 0)
        binary_responses[tuple(arguments)] = (
            (fixtures / fixture_name).read_text(encoding="utf-8"),
            "",
            0,
        )
    responses["agent"] = {("--version",): ("grok 0.2.101 (fixture) [alpha]\n", "", 0)}
    responses["devin"] = {("--version",): ("devin 3000.1.27 (fixture)\n", "", 0)}
    responses["pi"][("--help",)] = (
        (fixtures / "pi_models.txt").read_text(encoding="utf-8"),
        "",
        0,
    )

    target.mkdir(parents=True, exist_ok=True)
    return {
        binary: _write_dispatch_harness(target / binary, binary_responses)
        for binary, binary_responses in responses.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize Delegate discovery fake harnesses")
    parser.add_argument("target", type=Path)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    args = parser.parse_args(argv)
    materialize_minimum_harnesses(args.target, fixtures=args.fixtures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
