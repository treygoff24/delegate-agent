"""The provider-error docs name every signature, key, flag, and outcome the code defines."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from delegate_agent import config as delegate_config
from delegate_agent import lane_health, outcome, provider_errors

DOCS = Path(__file__).resolve().parents[1] / "docs"


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


class ProviderErrorDocsTests(unittest.TestCase):
    def test_troubleshooting_table_lists_every_signature_id(self):
        # First cell of each table row: the prose elsewhere mentions some ids too, so a
        # bare substring check would pass with a row missing.
        listed: set[str] = set()
        for line in _doc("troubleshooting.md").splitlines():
            if line.startswith("| `"):
                listed.update(re.findall(r"`([a-z_]+)`", line.split("|")[1]))

        missing = [row.id for row in provider_errors.SIGNATURES if row.id not in listed]

        self.assertEqual(missing, [], "add these signatures to the docs/troubleshooting.md table")

    def test_troubleshooting_names_the_classes_and_the_marker_and_resume_controls(self):
        text = _doc("troubleshooting.md")
        for needle in (
            "persistent",
            "transient",
            "unknown",
            "lane_known_bad",
            lane_health.FORCE_LAUNCH_FLAG,
            "providerErrors.knownBadLaneMinutes",
            "providerErrors.autoResume",
            "knownBadLanes",
        ):
            with self.subTest(needle):
                self.assertIn(needle, text)

    def test_configuration_documents_every_provider_errors_key(self):
        text = _doc("configuration.md")
        section = delegate_config.embedded_default_config()["providerErrors"]
        self.assertGreaterEqual(len(section), 4)
        for key in section:
            with self.subTest(key):
                self.assertIn(f"- `{key}`:", text)

    def test_cli_reference_documents_the_flag_the_exit_code_and_the_new_failure_kinds(self):
        text = _doc("cli-reference.md")
        self.assertIn(lane_health.FORCE_LAUNCH_FLAG, text)
        self.assertIn("| 4 |", text)
        self.assertIn("authHealth", text)
        for kind in (outcome.FAILURE_LANE_KNOWN_BAD, outcome.FAILURE_PROVIDER_EXHAUSTED):
            with self.subTest(kind):
                self.assertIn(f"`{kind}`", text)

    def test_workflow_docs_describe_the_typed_outcomes_and_the_stage_stop(self):
        text = _doc("delegate-workflows.md")
        for needle in (
            outcome.FAILURE_LANE_KNOWN_BAD,
            outcome.FAILURE_PROVIDER_EXHAUSTED,
            "providerOutcomes",
            "stage_lane_stopped",
            "agent_lane_skipped",
            "stageStopAfter",
        ):
            with self.subTest(needle):
                self.assertIn(needle, text)
