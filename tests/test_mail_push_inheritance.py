"""followup and resume keep the source Run's mail push unless told otherwise."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delegate_agent import cli_parser as parser_api
from delegate_agent import run_registry
from delegate_agent.errors import DelegateError
from tests import test_followup_refusals
from tests.test_resume_inheritance import ResumeFixture


class MailPushFlagParserTests(unittest.TestCase):
    def parse_followup(self, *args: str):
        return parser_api.parse_cli(["--json", "followup", *args, "h", "go"]).payload

    def parse_resume(self, *args: str):
        return parser_api.parse_cli(["--json", "resume", *args, "h", "go"]).payload

    def test_followup_flags_are_tri_state(self):
        self.assertIsNone(self.parse_followup().mail_push)
        self.assertIs(self.parse_followup("--mail-push").mail_push, True)
        self.assertIs(self.parse_followup("--no-mail-push").mail_push, False)

    def test_followup_both_flags_conflict(self):
        with self.assertRaises(DelegateError) as caught:
            self.parse_followup("--mail-push", "--no-mail-push")
        self.assertEqual(caught.exception.error, "invalid_option_combination")

    def test_resume_flags_are_tri_state(self):
        self.assertIsNone(self.parse_resume().mail_push)
        self.assertIs(self.parse_resume("--mail-push").mail_push, True)
        self.assertIs(self.parse_resume("--no-mail-push").mail_push, False)


class FollowupMailPushInheritanceTests(unittest.TestCase):
    write_test_run = test_followup_refusals.FollowupRefusalsTests.write_test_run
    run_followup_cli = test_followup_refusals.FollowupRefusalsTests.run_followup_cli

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def source(self, *, mail_push: bool) -> str:
        run_id, alias = self.write_test_run(harness="codex", harness_session_id="th_valid12345")
        path = run_registry.run_directory(self.registry_root, run_id) / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if mail_push:
            manifest["mailPush"] = True
        run_registry.write_json_atomic(path, manifest)
        return alias

    def dry_run(self, alias: str, *flags: str) -> tuple[dict, str]:
        code, stdout, stderr = self.run_followup_cli(
            ["--json", "followup", "--dry-run", *flags, alias, "continue"]
        )
        self.assertEqual(code, 0, stdout + stderr)
        return json.loads(stdout), stderr

    def test_followup_inherits_mail_push_and_says_so(self):
        payload, stderr = self.dry_run(self.source(mail_push=True))
        self.assertIs(payload.get("mailPush"), True)
        self.assertIn("inherited --mail-push from the source run", stderr)

    def test_followup_of_a_source_without_push_stays_pull(self):
        payload, stderr = self.dry_run(self.source(mail_push=False))
        self.assertNotIn("mailPush", payload)
        self.assertNotIn("mail-push", stderr)

    def test_no_mail_push_drops_the_inherited_push(self):
        payload, _stderr = self.dry_run(self.source(mail_push=True), "--no-mail-push")
        self.assertNotIn("mailPush", payload)

    def test_explicit_mail_push_enables_push_on_a_pull_source(self):
        payload, _stderr = self.dry_run(self.source(mail_push=False), "--mail-push")
        self.assertIs(payload.get("mailPush"), True)


class ResumeMailPushInheritanceTests(ResumeFixture):
    def source(self, *, mail_push: bool) -> str:
        self.write_config({})
        manifest: dict = {"isolationMode": "none"}
        if mail_push:
            manifest["mailPush"] = True
        _run_id, alias, _path = self.seed_run(engine="codex", manifest=manifest)
        return alias

    def test_resume_inherits_mail_push(self):
        payload, stderr = self.run_resume(["--dry-run", self.source(mail_push=True), "next"])
        self.assertIs(payload.get("mailPush"), True)
        self.assertIn("inherited --mail-push from the source run", stderr)

    def test_resume_no_mail_push_drops_it(self):
        payload, _stderr = self.run_resume(
            ["--dry-run", "--no-mail-push", self.source(mail_push=True), "next"]
        )
        self.assertNotIn("mailPush", payload)

    def test_resume_without_source_push_stays_pull(self):
        payload, _stderr = self.run_resume(["--dry-run", self.source(mail_push=False), "next"])
        self.assertNotIn("mailPush", payload)

    def test_cross_engine_resume_onto_an_unverified_harness_falls_back_with_a_note(self):
        payload, stderr = self.run_resume(
            ["--dry-run", "--engine", "cursor", self.source(mail_push=True), "next"]
        )
        self.assertNotIn("mailPush", payload)
        self.assertIn("no verified push adapter", stderr)


if __name__ == "__main__":
    unittest.main()
