"""`describe --full` advertises the workflow DSL the runtime actually injects.

The delegate-workflows skill names `describe --full` as the authority for DSL
signatures, so a global or capability the runtime grows without describe
listing it is a feature agents never learn about. These tests read the names
from inside a real workflow script rather than from the runtime's constant.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from delegate_agent import config as delegate_config
from delegate_agent import describe_payload, run_registry
from delegate_agent.workflows import runtime as workflow_runtime

# Returns what the script's own namespace holds: injected names (the wrapper
# function and __builtins__ excluded), the capabilities global, and whether
# each alias describe declares is bound to the same object as its target.
PROBE_SCRIPT = """\
names = sorted(name for name in globals() if not name.startswith("__"))
aliases = {alias: globals()[alias] == globals()[target] for alias, target in args.items()}
return {"names": names, "capabilities": capabilities, "aliases": aliases}
"""


class DescribeWorkflowDslTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.config = delegate_config.embedded_default_config()
        payload = describe_payload.describe_payload(self.config, "embedded-default", self.workspace)
        self.dsl = payload["workflows"]["dsl"]

    def _probe(self) -> dict[str, object]:
        wf_id = "wf_d5c0d5c0d5c0"
        root = self.workspace / ".delegate" / "workflows" / wf_id
        run_registry.ensure_private_dir(root)
        script = self.workspace / "probe.py"
        script.write_text(PROBE_SCRIPT, encoding="utf-8")
        aliases = dict(self.dsl["globalAliases"])
        state = workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=self.workspace,
            root=root,
            script_path=script,
            config=self.config,
            cli_argv=["delegate"],
            args=aliases,
            budget=workflow_runtime.Budget(None),
        )
        frame = workflow_runtime._WorkflowInvocation(script, aliases, "root", 0)
        result = workflow_runtime.execute_workflow(state, frame)
        self.assertIsInstance(result, dict)
        return result

    def test_capabilities_match_the_runtime(self) -> None:
        injected = self._probe()["capabilities"]
        # Precondition: the runtime advertises more than the original three,
        # so a stale literal cannot pass by coincidence.
        self.assertGreater(len(workflow_runtime.WORKFLOW_CAPABILITIES), 3)
        self.assertEqual(injected, workflow_runtime.WORKFLOW_CAPABILITIES)
        self.assertEqual(self.dsl["capabilityVersions"], injected)
        self.assertIn(repr(injected), self.dsl["capabilities"])

    def test_globals_list_every_injected_name(self) -> None:
        injected = set(self._probe()["names"])
        self.assertIn("agent", injected)  # precondition: the probe saw the DSL
        listed = self.dsl["globals"]
        self.assertEqual(len(listed), len(set(listed)), "describe lists a global twice")
        self.assertEqual(
            sorted(injected - set(listed)), [], "injected globals missing from describe"
        )
        self.assertEqual(
            sorted(set(listed) - injected), [], "describe lists globals the runtime lacks"
        )

    def test_every_global_is_documented_and_aliases_are_real(self) -> None:
        aliases = self.dsl["globalAliases"]
        for name in self.dsl["globals"]:
            with self.subTest(name=name):
                documented = aliases.get(name, name)
                self.assertIn(documented, self.dsl["globals"])
                self.assertIn(documented, self.dsl, f"no describe entry for {documented}")
        self.assertTrue(aliases)
        self.assertEqual(self._probe()["aliases"], dict.fromkeys(aliases, True))


if __name__ == "__main__":
    unittest.main()
