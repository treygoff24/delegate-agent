"""Unit tests for creation-side isolation helpers (isolation.py).

Tests pure functions only; no filesystem mutations.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_isolation():
    from delegate_agent import isolation

    return isolation


class RepoFingerprintTests(unittest.TestCase):
    """compute_repo_fingerprint_from_common_dir: stable 12-char SHA-256[:12] fingerprint."""

    def test_returns_12_hex_chars(self):
        iso = load_isolation()
        with tempfile.TemporaryDirectory() as tmp:
            fp = iso.compute_repo_fingerprint_from_common_dir(tmp)
        self.assertEqual(len(fp), 12)
        int(fp, 16)  # must be valid hex

    def test_stable_across_calls(self):
        iso = load_isolation()
        with tempfile.TemporaryDirectory() as tmp:
            fp1 = iso.compute_repo_fingerprint_from_common_dir(tmp)
            fp2 = iso.compute_repo_fingerprint_from_common_dir(tmp)
        self.assertEqual(fp1, fp2)

    def test_path_with_spaces(self):
        iso = load_isolation()
        with tempfile.TemporaryDirectory(suffix=" my repo") as tmp:
            fp = iso.compute_repo_fingerprint_from_common_dir(tmp)
        self.assertEqual(len(fp), 12)
        int(fp, 16)

    def test_path_with_unicode(self):
        iso = load_isolation()
        with tempfile.TemporaryDirectory(suffix="\u00e9\u00e0\u00f1") as tmp:
            fp = iso.compute_repo_fingerprint_from_common_dir(tmp)
        self.assertEqual(len(fp), 12)
        int(fp, 16)

    def test_distinct_repos_produce_distinct_fingerprints(self):
        iso = load_isolation()
        with (
            tempfile.TemporaryDirectory() as tmp1,
            tempfile.TemporaryDirectory() as tmp2,
        ):
            fp1 = iso.compute_repo_fingerprint_from_common_dir(tmp1)
            fp2 = iso.compute_repo_fingerprint_from_common_dir(tmp2)
        self.assertNotEqual(fp1, fp2)

    def test_symlink_to_same_path_produces_same_fingerprint(self):
        iso = load_isolation()
        with tempfile.TemporaryDirectory() as real_dir:
            fp_real = iso.compute_repo_fingerprint_from_common_dir(real_dir)
            link_dir = Path(real_dir) / "linked"
            link_dir.symlink_to(real_dir, target_is_directory=True)
            fp_link = iso.compute_repo_fingerprint_from_common_dir(str(link_dir))
            self.assertEqual(fp_real, fp_link)

    def test_resolve_strict_raises_for_missing_path(self):
        iso = load_isolation()
        with self.assertRaises(FileNotFoundError):
            iso.compute_repo_fingerprint_from_common_dir("/nonexistent/path")


class ShortRunIdTests(unittest.TestCase):
    """short_run_id: strips del_ prefix, sanitizes, truncates to 32 chars."""

    def test_strips_del_prefix(self):
        iso = load_isolation()
        result = iso.short_run_id("del_20260524T184455Z_a1b2c3")
        self.assertFalse(result.startswith("del_"))
        # 20260524T184455Z_a1b2c3 = 22 chars, _ is allowed
        self.assertEqual(result, "20260524T184455Z_a1b2c3")

    def test_replaces_non_alnum_chars(self):
        iso = load_isolation()
        result = iso.short_run_id("del_20260524T184455Z_a1b2c3!@#$")
        # !@#$ become ----
        self.assertNotIn("!", result)
        self.assertNotIn("@", result)
        self.assertIn("-", result)

    def test_truncates_to_32_chars(self):
        iso = load_isolation()
        long_suffix = "a" * 50
        result = iso.short_run_id(f"del_{long_suffix}")
        self.assertEqual(len(result), 32)

    def test_preserves_allowed_chars(self):
        iso = load_isolation()
        result = iso.short_run_id("del_ABCabc09._-")
        self.assertEqual(result, "ABCabc09._-")

    def test_empty_after_prefix(self):
        iso = load_isolation()
        result = iso.short_run_id("del_")
        self.assertEqual(result, "")


class BranchLabelTests(unittest.TestCase):
    """branch_label: cursor/codex/droid with model alias slugging."""

    def test_cursor(self):
        iso = load_isolation()
        self.assertEqual(iso.branch_label("cursor", None), "cursor")

    def test_codex(self):
        iso = load_isolation()
        self.assertEqual(iso.branch_label("codex", None), "codex")

    def test_droid_without_model(self):
        iso = load_isolation()
        self.assertEqual(iso.branch_label("droid", None), "droid")

    def test_droid_with_model_alias(self):
        iso = load_isolation()
        self.assertEqual(iso.branch_label("droid", "qwen"), "droid-qwen")

    def test_droid_model_alias_slugging(self):
        iso = load_isolation()
        self.assertEqual(iso.branch_label("droid", "My Model V2"), "droid-my-model-v2")

    def test_droid_model_alias_slug_multiple_dashes(self):
        iso = load_isolation()
        self.assertEqual(iso.branch_label("droid", "qwen---3.5"), "droid-qwen-3-5")

    def test_droid_model_alias_slug_leading_trailing(self):
        iso = load_isolation()
        self.assertEqual(iso.branch_label("droid", "---model---"), "droid-model")


class PlanBranchNameTests(unittest.TestCase):
    """plan_branch_name: delegate/<label>-<short-id>."""

    def test_basic(self):
        iso = load_isolation()
        result = iso.plan_branch_name("cursor", "20260524T184455Z-a1b2c3")
        self.assertEqual(result, "delegate/cursor-20260524T184455Z-a1b2c3")

    def test_with_droid_label(self):
        iso = load_isolation()
        result = iso.plan_branch_name("droid-qwen", "abc123")
        self.assertEqual(result, "delegate/droid-qwen-abc123")


class PlanWorktreePathTests(unittest.TestCase):
    """plan_worktree_path: <dataHome>/<fingerprint>/<label>-<short-id>."""

    def test_basic(self):
        iso = load_isolation()
        data_home = Path("/tmp/.delegate/worktrees")
        result = iso.plan_worktree_path(data_home, "abc123def456", "cursor", "short-id")
        self.assertEqual(result, Path("/tmp/.delegate/worktrees/abc123def456/cursor-short-id"))

    def test_uses_data_home_as_base(self):
        iso = load_isolation()
        data_home = Path("/custom/path")
        result = iso.plan_worktree_path(data_home, "fp", "codex", "s1")
        self.assertEqual(result, Path("/custom/path/fp/codex-s1"))


class WorktreesDataHomeTests(unittest.TestCase):
    """worktrees_data_home: config-driven or default ~/.delegate/worktrees."""

    def test_default(self):
        iso = load_isolation()
        result = iso.worktrees_data_home({})
        self.assertEqual(result, Path.home() / ".delegate" / "worktrees")

    def test_config_override(self):
        iso = load_isolation()
        config = {"worktrees": {"dataHome": "/custom/data"}}
        result = iso.worktrees_data_home(config)
        self.assertEqual(result, Path("/custom/data"))

    def test_config_tilde_expansion(self):
        iso = load_isolation()
        config = {"worktrees": {"dataHome": "~/custom/worktrees"}}
        result = iso.worktrees_data_home(config)
        self.assertEqual(result, Path.home() / "custom" / "worktrees")

    def test_config_relative_path_raises(self):
        iso = load_isolation()
        with self.assertRaises(iso.ConfigError):
            iso.worktrees_data_home({"worktrees": {"dataHome": "relative/path"}})

    def test_explicit_null_uses_default(self):
        iso = load_isolation()
        config = {"worktrees": {"dataHome": None}}
        result = iso.worktrees_data_home(config)
        self.assertEqual(result, Path.home() / ".delegate" / "worktrees")

    def test_non_dict_worktrees_uses_default(self):
        iso = load_isolation()
        config = {"worktrees": "bananas"}
        result = iso.worktrees_data_home(config)
        self.assertEqual(result, Path.home() / ".delegate" / "worktrees")


class BuildIsolationContextTests(unittest.TestCase):
    """build_isolation_context: creates IsolationContext from resolved isolation value.

    Finding B: isolation_mode preserves the raw resolved value (auto/none/worktree),
    while effective_isolation is the mapped behavior (none/worktree, never auto).
    """

    def test_cursor_safe_auto_maps_to_worktree_temporary(self):
        iso = load_isolation()
        ctx = iso.build_isolation_context(
            source_workspace="/repo",
            resolved_isolation="auto",
            engine="cursor",
            mode="safe",
        )
        # isolation_mode preserves "auto"; effective_isolation is mapped to "worktree"
        self.assertEqual(ctx.isolation_mode, "auto")
        self.assertEqual(ctx.effective_isolation, "worktree")
        self.assertEqual(ctx.isolation_lifecycle, "temporary")
        self.assertFalse(ctx.preserved_workspace)

    def test_codex_safe_auto_maps_to_worktree_temporary(self):
        iso = load_isolation()
        ctx = iso.build_isolation_context(
            source_workspace="/repo",
            resolved_isolation="auto",
            engine="codex",
            mode="safe",
        )
        self.assertEqual(ctx.isolation_mode, "auto")
        self.assertEqual(ctx.effective_isolation, "worktree")
        self.assertEqual(ctx.isolation_lifecycle, "temporary")

    def test_droid_safe_auto_maps_to_worktree(self):
        iso = load_isolation()
        ctx = iso.build_isolation_context(
            source_workspace="/repo",
            resolved_isolation="auto",
            engine="droid",
            mode="safe",
        )
        self.assertEqual(ctx.isolation_mode, "auto")
        self.assertEqual(ctx.effective_isolation, "worktree")
        self.assertEqual(ctx.isolation_lifecycle, "temporary")
        self.assertFalse(ctx.preserved_workspace)

    def test_any_work_auto_maps_to_none(self):
        iso = load_isolation()
        ctx = iso.build_isolation_context(
            source_workspace="/repo",
            resolved_isolation="auto",
            engine="cursor",
            mode="work",
        )
        self.assertEqual(ctx.isolation_mode, "auto")
        self.assertEqual(ctx.effective_isolation, "none")
        self.assertEqual(ctx.isolation_lifecycle, "none")

    def test_explicit_worktree_work_is_persistent(self):
        iso = load_isolation()
        ctx = iso.build_isolation_context(
            source_workspace="/repo",
            resolved_isolation="worktree",
            engine="cursor",
            mode="work",
        )
        self.assertEqual(ctx.isolation_mode, "worktree")
        self.assertEqual(ctx.effective_isolation, "worktree")
        self.assertEqual(ctx.isolation_lifecycle, "persistent")
        self.assertTrue(ctx.preserved_workspace)

    def test_explicit_worktree_safe_is_temporary(self):
        iso = load_isolation()
        ctx = iso.build_isolation_context(
            source_workspace="/repo",
            resolved_isolation="worktree",
            engine="cursor",
            mode="safe",
        )
        self.assertEqual(ctx.isolation_lifecycle, "temporary")
        self.assertFalse(ctx.preserved_workspace)

    def test_explicit_none_all_modes(self):
        iso = load_isolation()
        for mode in ("safe", "work"):
            with self.subTest(mode=mode):
                ctx = iso.build_isolation_context(
                    source_workspace="/repo",
                    resolved_isolation="none",
                    engine="cursor",
                    mode=mode,
                )
                self.assertEqual(ctx.isolation_mode, "none")
                self.assertEqual(ctx.effective_isolation, "none")
                self.assertEqual(ctx.isolation_lifecycle, "none")
                self.assertFalse(ctx.preserved_workspace)

    def test_planned_paths_with_git_metadata(self):
        """Finding C: When git metadata is provided and effective is worktree,
        planned paths/branches are computed even in dry-run."""
        iso = load_isolation()
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                ["git", "-C", tmp, "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", tmp, "config", "user.name", "Test"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", tmp, "config", "user.email", "test@example.com"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", tmp, "commit", "--allow-empty", "-m", "init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            gd = subprocess.run(
                ["git", "-C", tmp, "rev-parse", "--git-common-dir"],
                text=True,
                capture_output=True,
                check=True,
            )
            git_common_dir = gd.stdout.strip()
            if not git_common_dir.startswith("/"):
                git_common_dir = str(Path(tmp) / git_common_dir)

            oid = subprocess.run(
                ["git", "-C", tmp, "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()

            ctx = iso.build_isolation_context(
                source_workspace=tmp,
                resolved_isolation="worktree",
                engine="cursor",
                mode="work",
                source_git_root=tmp,
                source_git_common_dir=git_common_dir,
                source_head_oid=oid,
                source_head_ref="refs/heads/main",
                source_branch="main",
                config={},
                run_short_id="test1234",
            )
            self.assertIsNotNone(ctx.planned_branch)
            self.assertTrue(ctx.planned_branch.startswith("delegate/cursor-"))
            self.assertIsNotNone(ctx.planned_execution_cwd)
            self.assertIn("cursor-test1234", ctx.planned_execution_cwd)
            # No plain placeholder strings when git metadata is provided
            self.assertNotIn("<short-run-id-placeholder>", ctx.planned_branch)
            self.assertNotIn("<short-run-id-placeholder>", ctx.planned_execution_cwd)

    def test_no_git_metadata_no_planned_paths(self):
        """Without git metadata, planned fields remain None even for worktree."""
        iso = load_isolation()
        ctx = iso.build_isolation_context(
            source_workspace="/repo",
            resolved_isolation="worktree",
            engine="cursor",
            mode="work",
        )
        # Without git_common_dir, planned paths can't be computed
        self.assertIsNone(ctx.planned_branch)
        self.assertIsNone(ctx.planned_execution_cwd)

    def test_passes_git_metadata(self):
        iso = load_isolation()
        ctx = iso.build_isolation_context(
            source_workspace="/repo",
            resolved_isolation="worktree",
            engine="cursor",
            mode="work",
            source_git_root="/repo/.git",
            source_git_common_dir="/repo/.git",
            source_head_oid="abc123def456",
            source_head_ref="refs/heads/main",
            source_branch="main",
        )
        self.assertEqual(ctx.source_git_root, "/repo/.git")
        self.assertEqual(ctx.source_git_common_dir, "/repo/.git")
        self.assertEqual(ctx.source_head_oid, "abc123def456")
        self.assertEqual(ctx.source_head_ref, "refs/heads/main")
        self.assertEqual(ctx.source_branch, "main")


class GitTimeoutTests(unittest.TestCase):
    def test_require_clean_source_raises_git_timeout(self):
        iso = load_isolation()
        with (
            mock.patch.object(
                iso.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["git", "status"], 30),
            ),
            self.assertRaises(iso.IsolationExecutionError) as ctx,
        ):
            iso.require_clean_source("/repo")
        self.assertEqual(ctx.exception.error, "git_timeout")
        self.assertIn("git status timed out", ctx.exception.message)

    def test_create_persistent_worktree_branch_probe_timeout_is_structured(self):
        iso = load_isolation()
        with (
            mock.patch.object(
                iso.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["git", "show-ref"], 30),
            ),
            self.assertRaises(iso.IsolationExecutionError) as ctx,
        ):
            iso.create_persistent_worktree(
                "/repo",
                "delegate/cursor-timeout",
                "/tmp/delegate-timeout",
                "HEAD",
            )
        self.assertEqual(ctx.exception.error, "git_timeout")
        self.assertIn("branch availability check timed out", ctx.exception.message)


class ReexportTests(unittest.TestCase):
    """isolation.py re-exports constants from config."""

    def test_constants_reexported(self):
        iso = load_isolation()
        self.assertEqual(iso.ISOLATION_AUTO, "auto")
        self.assertEqual(iso.ISOLATION_NONE, "none")
        self.assertEqual(iso.ISOLATION_WORKTREE, "worktree")
        self.assertIn("auto", iso.VALID_ISOLATION_VALUES)
        self.assertIn("none", iso.VALID_ISOLATION_VALUES)
        self.assertIn("worktree", iso.VALID_ISOLATION_VALUES)


class PromptInstructionImportTests(unittest.TestCase):
    """Isolation imports prompt constants from the neutral prompt_instructions module."""

    def test_prompt_instruction_import_no_cycle(self):
        """Subprocess imports isolation first (before runner) to prove no import cycle."""
        # Run in a clean subprocess to get a guaranteed fresh import order.
        code = "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {SRC!r})",
                "from delegate_agent import isolation",
                "result = isolation.prepend_persistent_worktree_context('hi')",
                "assert 'Delegate-created isolated Git worktree' in result",
                "print('ok')",
            ]
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=f"import cycle detected: {proc.stderr}")
        self.assertIn("ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
