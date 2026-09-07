from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent.workflows import registry, runtime


class NestedWorkflowThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.sequence = 0

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")

    def _workflow_root(self, root_body: str) -> Path:
        self.sequence += 1
        wf_id = f"wf_{self.sequence:012x}"
        root = registry.ensure_workflow_dir(self.workspace, wf_id)
        self._write(root / registry.SCRIPT_FILE, root_body)
        return root

    def _state(self, root: Path) -> runtime.WorkflowState:
        return runtime.WorkflowState(
            wf_id=root.name,
            workspace=self.workspace,
            root=root,
            script_path=root / registry.SCRIPT_FILE,
            config={"workflows": {"itemThreads": 2}},
            cli_argv=["delegate"],
            args={"token": "root-arg"},
            budget=runtime.Budget(None),
        )

    def test_parallel_nested_workflow_uses_child_directory_args_and_replay_key(self) -> None:
        root = self._workflow_root(
            """
            meta = {"name": "root"}
            return workflow("sub/child.py", args={"token": "parallel-arg"})
            """
        )
        self._write(
            root / "sub" / "child.py",
            """
            meta = {"name": "child"}
            return parallel([
                lambda: workflow(
                    "grandchild.py",
                    args={"prompt": args["token"] + "-grandchild"},
                ),
            ])
            """,
        )
        self._write(
            root / "sub" / "grandchild.py",
            """
            meta = {"name": "sub-grandchild", "defaults": {"engine": "codex"}}
            return agent("sub:" + args["prompt"])
            """,
        )
        self._write(
            root / "grandchild.py",
            """
            meta = {"name": "root-decoy", "defaults": {"engine": "codex"}}
            return agent("root-decoy:" + args["prompt"])
            """,
        )

        prompts: list[str] = []

        def fake_agent(_dsl: object, _engine: str, prompt: str, **_kwargs: object) -> str:
            prompts.append(prompt)
            return "fake completion"

        with mock.patch.object(
            runtime.WorkflowDsl,
            "_run_agent_attempts",
            autospec=True,
            side_effect=fake_agent,
        ):
            self.assertEqual(runtime.execute_workflow(self._state(root)), ["fake completion"])

        self.assertEqual(prompts, ["sub:parallel-arg-grandchild"])
        first_events = registry.iter_journal(root / registry.JOURNAL_FILE)
        first_start = next(event for event in first_events if event["type"] == "agent_started")
        expected_scope = "root/wf:child@0/parallel@0/thunk#0/wf:grandchild@0/seq#0"
        self.assertEqual(first_start["scope"], expected_scope)

        with mock.patch.object(
            runtime.WorkflowDsl,
            "_run_agent_attempts",
            side_effect=AssertionError("a replayed agent must not relaunch"),
        ):
            self.assertEqual(runtime.execute_workflow(self._state(root)), ["fake completion"])

        final_events = registry.iter_journal(root / registry.JOURNAL_FILE)
        cache_hit = next(event for event in final_events if event["type"] == "agent_cache_hit")
        self.assertEqual(cache_hit["scope"], expected_scope)
        self.assertEqual(cache_hit["key"].encode(), first_start["key"].encode())

    def test_pipeline_nested_workflow_uses_child_directory_and_args(self) -> None:
        root = self._workflow_root(
            """
            meta = {"name": "root"}
            return workflow("sub/child.py", args={"token": "pipeline-arg"})
            """
        )
        self._write(
            root / "sub" / "child.py",
            """
            meta = {"name": "child"}
            return pipeline(
                ["stage-input"],
                lambda previous, item, index: workflow(
                    "grandchild.py",
                    args={"token": args["token"], "item": item, "index": index},
                ),
            )
            """,
        )
        self._write(
            root / "sub" / "grandchild.py",
            """
            meta = {"name": "sub-grandchild"}
            return {
                "path": "sub/grandchild.py",
                "token": args["token"],
                "item": args["item"],
                "index": args["index"],
            }
            """,
        )
        self._write(
            root / "grandchild.py",
            """
            meta = {"name": "root-decoy"}
            return {"path": "root-decoy", "token": args["token"]}
            """,
        )

        self.assertEqual(
            runtime.execute_workflow(self._state(root)),
            [
                {
                    "path": "sub/grandchild.py",
                    "token": "pipeline-arg",
                    "item": "stage-input",
                    "index": 0,
                }
            ],
        )

    def test_soft_park_resume_nested_workflow_uses_child_directory_and_args(self) -> None:
        root = self._workflow_root(
            """
            meta = {"name": "root"}
            return workflow("sub/child.py", args={"token": "recovery-arg"})
            """
        )
        self._write(
            root / "sub" / "child.py",
            """
            meta = {"name": "child"}

            def recover():
                if not parked("recover"):
                    park_item("recover", {"reason": "retry"})
                return workflow("grandchild.py", args={"token": args["token"]})

            return soft_park({"recover": recover})
            """,
        )
        self._write(
            root / "sub" / "grandchild.py",
            """
            meta = {"name": "sub-grandchild"}
            return {"path": "sub/grandchild.py", "token": args["token"]}
            """,
        )
        self._write(
            root / "grandchild.py",
            """
            meta = {"name": "root-decoy"}
            return {"path": "root-decoy", "token": args["token"]}
            """,
        )

        with self.assertRaises(runtime.SoftParkExit):
            runtime.execute_workflow(self._state(root))
        parked = next(
            event
            for event in registry.iter_journal(root / registry.JOURNAL_FILE)
            if event["type"] == "item_parked"
        )

        self.assertEqual(
            runtime.execute_workflow(self._state(root)),
            [{"path": "sub/grandchild.py", "token": "recovery-arg"}],
        )
        unparked = next(
            event
            for event in registry.iter_journal(root / registry.JOURNAL_FILE)
            if event["type"] == "item_unparked"
        )
        self.assertEqual(unparked["scope"].encode(), parked["scope"].encode())

    def test_threaded_nested_workflow_cannot_bypass_depth_limit(self) -> None:
        root = self._workflow_root(
            """
            meta = {"name": "root"}
            return workflow("sub/a.py")
            """
        )
        self._write(root / "sub" / "a.py", 'meta = {"name": "a"}\nreturn workflow("b.py")')
        self._write(root / "sub" / "b.py", 'meta = {"name": "b"}\nreturn workflow("c.py")')
        self._write(
            root / "sub" / "c.py",
            """
            meta = {"name": "c"}
            return parallel([lambda: workflow("d.py")])
            """,
        )
        self._write(
            root / "d.py",
            'meta = {"name": "root-decoy"}\nreturn "depth-limit-bypassed"',
        )

        self.assertEqual(runtime.execute_workflow(self._state(root)), [None])
        failure = next(
            event
            for event in registry.iter_journal(root / registry.JOURNAL_FILE)
            if event["type"] == "thunk_failed"
        )
        self.assertEqual(
            failure["scope"],
            "root/wf:a@0/wf:b@0/wf:c@0/parallel@0/thunk#0",
        )
        self.assertIn("workflow nesting depth exceeded 3", failure["error"])


if __name__ == "__main__":
    unittest.main()
