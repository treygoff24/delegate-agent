import unittest
from unittest import mock

from delegate_agent import runner, sandbox_bwrap
from delegate_agent.errors import DelegateError
from delegate_agent.isolation import IsolationContext, IsolationExecutionError
from delegate_agent.sandbox_bwrap import Bind, Mask, SandboxPlan


class IsolationPlanTests(unittest.TestCase):
    def test_named_contexts_keep_lifecycle_and_preservation_together(self):
        direct = IsolationContext.unisolated("/source")
        temporary = IsolationContext.temporary("/source", isolation_mode="auto")
        attached = IsolationContext.attached(
            "/source",
            branch="branch",
            execution_cwd="/attached",
            source_git_root="/source",
            attachment={"path": "/attached", "sourceRunId": "run-a"},
        )
        self.assertEqual(
            (direct.effective_isolation, direct.isolation_lifecycle, direct.preserved_workspace),
            ("none", "none", False),
        )
        self.assertEqual(
            (
                temporary.effective_isolation,
                temporary.isolation_lifecycle,
                temporary.preserved_workspace,
            ),
            ("worktree", "temporary", False),
        )
        self.assertEqual(
            (
                attached.effective_isolation,
                attached.isolation_lifecycle,
                attached.preserved_workspace,
            ),
            ("worktree", "attached", False),
        )
        with self.assertRaises(IsolationExecutionError):
            IsolationContext.attached(
                "/source",
                branch="branch",
                execution_cwd="/attached",
                source_git_root="/source",
                attachment={"path": "/different"},
            )

    def test_plan_serializes_to_the_existing_public_shape(self):
        masks = (Mask("ignored", "tmpfs"), Mask("secret.env", "devnull"))
        binds = (Bind("/read", "ro"), Bind("/scratch", "rw"))
        plan = SandboxPlan("/usr/bin/bwrap", masks, binds)
        self.assertIs(plan.masks, masks)
        self.assertIs(plan.binds, binds)
        self.assertEqual(
            plan.payload(),
            {
                "backend": "bwrap",
                "bwrapPath": "/usr/bin/bwrap",
                "masks": [
                    {"path": "ignored", "kind": "tmpfs"},
                    {"path": "secret.env", "kind": "devnull"},
                ],
                "binds": [{"path": "/read", "mode": "ro"}, {"path": "/scratch", "mode": "rw"}],
            },
        )

    def test_invalid_plan_inputs_fail_instead_of_being_silently_dropped(self):
        for mask in (
            Mask("../escape", "tmpfs"),
            Mask("/absolute", "tmpfs"),
            Mask("valid", "wrong"),
            Mask("valid", []),
            {"path": "missing-kind"},
        ):
            with self.subTest(mask=mask), self.assertRaises(DelegateError):
                SandboxPlan(None, masks=(mask,))
        for bind in (Bind("relative", "ro"), Bind("/absolute", "wrong"), {"mode": "ro"}):
            with self.subTest(bind=bind), self.assertRaises(DelegateError):
                SandboxPlan(None, binds=(bind,))

    def test_empty_or_untyped_execution_plan_cannot_bypass_the_boundary(self):
        with mock.patch.object(runner.subprocess, "Popen") as launch:
            with self.assertRaises(DelegateError):
                runner._launch_tracked_process(["codex"], "/source", stdin_text=None, sandbox={})
            launch.assert_not_called()

    def test_typed_plan_keeps_parity_mask_limit(self):
        with (
            mock.patch.object(sandbox_bwrap, "MASK_OVERFLOW_LIMIT", 1),
            self.assertRaises(DelegateError) as error,
        ):
            SandboxPlan(None, masks=(Mask("a", "tmpfs"), Mask("b", "devnull")))
        self.assertEqual(error.exception.error, "bwrap_mask_overflow")
