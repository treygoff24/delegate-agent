"""Explicit offline integration probe: python3 -m tests.codex_scratch_probe [codex].

Requires a real Codex CLI and working native sandbox; unsupported hosts fail,
never skip. No model turn, credentials, or user configuration are required.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry, run_scratch, runner

PROGRAM = """import json,os,socket,sys,tempfile
from pathlib import Path
result={}
for name,path in zip(('scratch','workspace','source','metadata','symlink','sibling'),sys.argv[1:]):
    try:
        Path(path).write_text('probe')
        result[name]=True
    except OSError:
        result[name]=False
hardlink=Path(sys.argv[1]).with_name('hardlink-probe')
try:
    os.link(sys.argv[3],hardlink)
    hardlink.write_text('probe')
    result['hardlink']=True
except OSError:
    result['hardlink']=False
finally:
    if hardlink.exists(): hardlink.unlink()
try:
    with tempfile.NamedTemporaryFile() as temporary:
        result['tempfile']=Path(temporary.name).parent==Path(sys.argv[1]).parent
except OSError:
    result['tempfile']=False
try:
    with socket.create_connection(('127.0.0.1',int(sys.argv[-1])),timeout=1):
        result['network']=True
except OSError:
    result['network']=False
result['cwd']=str(Path.cwd())
result['instructions']=Path('AGENTS.md').read_text()
print(json.dumps(result))
"""

WORK_PROGRAM = """import json,os,tempfile
from pathlib import Path
result={}
try:
    Path(os.environ['TMPDIR'],'work.txt').write_text('ok')
    result['scratch']=True
except OSError:
    result['scratch']=False
try:
    with tempfile.NamedTemporaryFile() as temporary:
        result['tempfile']=Path(temporary.name).parent==Path(os.environ['TMPDIR'])
except OSError:
    result['tempfile']=False
print(json.dumps(result))
"""


def probe(binary: str) -> dict:
    with (
        tempfile.TemporaryDirectory(prefix="delegate-native-scratch-", dir="/var/tmp") as temp,
        socket.socket() as listener,
    ):
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        root = Path(temp)
        source, workspace, home = root / "source", root / "review-copy", root / "home"
        for directory in (source, workspace, home):
            directory.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            registry = run_registry.ensure_registry(source, workspace_kind="directory")
            run_registry.register_run(
                registry, harness="codex", run_id="del_20260906T000000Z_aaaaaa"
            )
            run_registry.register_run(
                registry, harness="codex", run_id="del_20260906T000000Z_bbbbbb"
            )
            scratch = run_scratch.allocate(registry, "del_20260906T000000Z_aaaaaa")
            sibling = run_scratch.allocate(registry, "del_20260906T000000Z_bbbbbb")
        protected = (
            workspace / "sentinel",
            source / "sentinel",
            run_registry.run_directory(registry, "del_20260906T000000Z_aaaaaa") / "state.json",
        )
        for path in protected:
            path.write_text("unchanged")
        (workspace / "AGENTS.md").write_text("SCRATCH_PROBE_INSTRUCTIONS")
        link = scratch / "source-link"
        link.symlink_to(source / "sentinel")
        paths = [
            str(scratch / "result"),
            *(str(path) for path in protected),
            str(link),
            str(sibling / "denied"),
            str(listener.getsockname()[1]),
        ]
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "CODEX_API_KEY"}
        }
        env["CODEX_HOME"] = str(home)
        env.update({key: str(scratch) for key in ("TMPDIR", "TMP", "TEMP")})

        def run(argv):
            result = subprocess.run(
                argv, cwd=workspace, env=env, capture_output=True, text=True, timeout=20
            )
            if result.returncode:
                raise AssertionError(f"offline probe failed: {result.stderr}")
            return result.stdout

        version = run([binary, "--version"]).strip()
        control = json.loads(run([sys.executable, "-c", PROGRAM, *paths]))
        assert all(
            control[name]
            for name in (
                "scratch",
                "workspace",
                "source",
                "metadata",
                "symlink",
                "sibling",
                "hardlink",
            )
        ), control
        assert control["tempfile"] is True and control["network"] is True, control
        for path in protected:
            path.write_text("unchanged")
        # A pre-existing fixed profile would merge additional permissions. The
        # generated launch profile must not inherit this unrelated write grant.
        config = (
            'default_permissions="ambient"\n'
            '[permissions.ambient]\nextends=":workspace"\n'
            "[permissions.ambient.network]\nenabled=true\n"
            "[permissions.delegate_safe.filesystem]\n" + json.dumps(str(source)) + '="write"\n'
        )
        (home / "config.toml").write_text(config)
        original = [
            binary,
            "--ask-for-approval",
            "never",
            "exec",
            "--cd",
            str(workspace),
            "--sandbox",
            "read-only",
            "--json",
            "-",
        ]
        applied = runner._codex_argv_with_scratch(original, scratch)
        settings = [applied[index + 1] for index, value in enumerate(applied[:-1]) if value == "-c"]
        effective = tomllib.loads("\n".join(settings))
        name = effective["default_permissions"]
        assert name != "delegate_safe"
        assert effective["permissions"][name] == {
            "extends": ":read-only",
            "filesystem": {str(scratch): "write"},
        }
        flags = [token for value in settings for token in ("-c", value)]
        run([*applied[:-1], "--help"])  # Real exec argv parsing, no turn.
        observed = json.loads(
            run(
                [
                    binary,
                    "sandbox",
                    "-P",
                    name,
                    "-C",
                    str(workspace),
                    *flags,
                    "--",
                    sys.executable,
                    "-c",
                    PROGRAM,
                    *paths,
                ]
            )
        )
        assert observed["scratch"] is True, observed
        assert observed["tempfile"] is True and observed["network"] is False, observed
        assert all(
            observed[name] is False
            for name in (
                "workspace",
                "source",
                "metadata",
                "symlink",
                "sibling",
                "hardlink",
            )
        ), observed
        assert observed["cwd"] == str(workspace), observed
        assert observed["instructions"] == "SCRATCH_PROBE_INSTRUCTIONS", observed
        assert all(path.read_text() == "unchanged" for path in protected)
        # Debug rendering performs config and instruction discovery without a
        # provider turn. This command does not accept --strict-config; exec's
        # parsing and strict rejection are checked separately above/below.
        rendered = run(
            [binary, *flags, "-c", 'approval_policy="never"', "debug", "prompt-input", "probe"]
        )
        assert "SCRATCH_PROBE_INSTRUCTIONS" in rendered, "AGENTS instructions were not discovered"
        assert str(scratch) in rendered, "model context omitted the configured scratch root"

        work_observed = json.loads(
            run(
                [
                    binary,
                    "sandbox",
                    "-P",
                    ":workspace",
                    "-C",
                    str(workspace),
                    "--",
                    sys.executable,
                    "-c",
                    WORK_PROGRAM,
                ]
            )
        )
        assert work_observed == {"scratch": True, "tempfile": True}, work_observed

        # Regression control: a legacy writable_roots setting does NOT grant
        # writable scratch when the selected base policy remains read-only.
        legacy = json.loads(
            run(
                [
                    binary,
                    "sandbox",
                    "-P",
                    ":read-only",
                    "-C",
                    str(workspace),
                    "-c",
                    f"sandbox_workspace_write.writable_roots=[{json.dumps(str(scratch))}]",
                    "--",
                    sys.executable,
                    "-c",
                    PROGRAM,
                    *paths,
                ]
            )
        )
        assert legacy["scratch"] is False, legacy

        # Unsupported configuration is rejected by actual exec before a model
        # turn. Never silently drop --strict-config to make this pass.
        (home / "config.toml").write_text("unknown_fixture_key=true\n" + config)
        refused = subprocess.run(
            [*applied[:-1], "probe"],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert refused.returncode != 0 and "unknown configuration field" in refused.stderr, (
            refused.stderr
        )
        return {
            "version": version,
            "unsandboxedControl": True,
            "scratchWritable": True,
            "workspaceSourceMetadataDenied": True,
            "siblingScratchDenied": True,
            "symlinkEscapeDenied": True,
            "hardlinkEscapeDenied": True,
            "temporaryFilesUseScratch": True,
            "workModeTemporaryFilesUseScratch": True,
            "networkDeniedWithPermissiveAmbientDefault": True,
            "cwdAndInstructionsPreserved": True,
            "modelVisibleInstructionsPreserved": True,
            "legacyScratchDenied": True,
            "strictConfigRefused": True,
        }


if __name__ == "__main__":
    print(json.dumps(probe(sys.argv[1] if len(sys.argv) > 1 else "codex"), sort_keys=True))
