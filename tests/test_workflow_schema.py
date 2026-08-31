from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from delegate_agent.workflows import registry as workflow_registry
from delegate_agent.workflows import runtime as workflow_runtime
from delegate_agent.workflows import schema as workflow_schema

EXECUTE_RESULT = {
    "type": "object",
    "required": ["summary", "verify_results"],
    "properties": {
        "summary": {"type": "string"},
        "verify_results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["run", "exit"],
                "properties": {
                    "run": {"type": "string"},
                    "exit": {"type": "integer"},
                    "passed": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


class ParseJsonTolerant(unittest.TestCase):
    def test_markdown_report_with_decoy_brackets_yields_trailing_block(self) -> None:
        report = (
            "Status: completed.\n\n"
            "Verification: `FAIL unrouted-role: [T1] ...` then the contract "
            "`{run, exit, expect, passed, note}` per row.\n\n"
            "```json\n"
            '{"summary": "done", "verify_results": [{"run": "make test", "exit": 0}]}\n'
            "```\n"
        )
        value = workflow_schema.parse_json_tolerant(report, EXECUTE_RESULT)
        self.assertEqual(value["summary"], "done")

    def test_without_schema_last_decodable_value_wins(self) -> None:
        text = 'first [1, 2] then prose {"a": {"b": 1}} end'
        self.assertEqual(workflow_schema.parse_json_tolerant(text), {"a": {"b": 1}})

    def test_schema_prefers_validating_candidate_over_later_fragment(self) -> None:
        text = '{"summary": "ok", "verify_results": []} trailing note: []'
        value = workflow_schema.parse_json_tolerant(text, EXECUTE_RESULT)
        self.assertEqual(value["summary"], "ok")

    def test_leading_json_prefix_does_not_shadow_later_fenced_candidate(self) -> None:
        text = '[]\n\n```json\n{"summary": "done", "verify_results": []}\n```'
        value = workflow_schema.parse_json_tolerant(text, EXECUTE_RESULT)
        self.assertEqual(value["summary"], "done")

    def test_valid_leading_value_wins_over_trailing_decoys(self) -> None:
        cases = (
            (
                '{"result": 42}\n\nNote: {"result": 0} would be wrong.',
                {"type": "object"},
                {"result": 42},
            ),
            ('"the answer"\n\nSee the "notes" file.', {"type": "string"}, "the answer"),
            ('{"result": 42}\n\nThe "answer" is above.', None, {"result": 42}),
        )
        for text, schema, expected in cases:
            with self.subTest(schema=schema):
                self.assertEqual(workflow_schema.parse_json_tolerant(text, schema), expected)

    def test_bare_json_and_fenced_json_still_parse(self) -> None:
        self.assertEqual(workflow_schema.parse_json_tolerant('{"a": 1}'), {"a": 1})
        self.assertEqual(workflow_schema.parse_json_tolerant('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_string_payload_is_decoded_once_for_non_string_schema(self) -> None:
        payload = json.dumps(json.dumps({"a": 1}))
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
        self.assertEqual(workflow_schema.parse_json_tolerant(payload, schema), {"a": 1})

    def test_string_schema_remains_strictly_a_string(self) -> None:
        payload = json.dumps(json.dumps({"a": 1}))
        schema = {"type": "string"}
        self.assertEqual(workflow_schema.parse_json_tolerant(payload, schema), '{"a": 1}')

    def test_no_json_raises(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            workflow_schema.parse_json_tolerant("nothing here [T1] {run, exit}")


class AdditionalPropertiesSchema(unittest.TestCase):
    MAP: ClassVar[dict[str, object]] = {
        "type": "object",
        "additionalProperties": {"type": "boolean"},
    }

    def test_map_schema_is_accepted_and_values_validated(self) -> None:
        workflow_schema.validate_schema_subset(self.MAP)
        workflow_schema.validate_value({"B1.1": True, "B1.4": False}, self.MAP)
        with self.assertRaises(workflow_schema.SchemaError):
            workflow_schema.validate_value({"B1.1": "yes"}, self.MAP)

    def test_non_schema_additional_properties_rejected(self) -> None:
        with self.assertRaises(workflow_schema.SchemaError):
            workflow_schema.validate_schema_subset({"type": "object", "additionalProperties": 1})


class CodexNativeSchema(unittest.TestCase):
    def test_optional_fields_fall_back_to_prompt_path(self) -> None:
        self.assertIsNone(workflow_runtime._codex_native_schema(EXECUTE_RESULT))

    def test_map_schema_falls_back_to_prompt_path(self) -> None:
        schema = {
            "type": "object",
            "required": ["flags"],
            "properties": {
                "flags": {"type": "object", "additionalProperties": {"type": "boolean"}}
            },
            "additionalProperties": False,
        }
        self.assertIsNone(workflow_runtime._codex_native_schema(schema))

    def test_strict_compatible_schema_goes_native_unmodified(self) -> None:
        schema = {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        }
        self.assertIs(workflow_runtime._codex_native_schema(schema), schema)


class ResumeExhaustedKeys(unittest.TestCase):
    def test_exhausted_key_replays_none_but_is_marked_for_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wf"
            root.mkdir()
            journal = root / workflow_registry.JOURNAL_FILE
            events = [
                {"seq": 1, "type": "budget", "key": "k1"},
                {"seq": 2, "type": "agent_started", "key": "k1"},
                {
                    "seq": 3,
                    "type": "agent_finished",
                    "key": "k1",
                    "result": None,
                    "exhausted": True,
                },
                {"seq": 4, "type": "budget", "key": "k2"},
                {"seq": 5, "type": "agent_started", "key": "k2"},
                {"seq": 6, "type": "agent_finished", "key": "k2", "result": {"ok": True}},
            ]
            journal.write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )
            state = workflow_runtime.WorkflowState(
                wf_id="wf_test",
                workspace=Path(tmp),
                root=root,
                script_path=root / workflow_registry.SCRIPT_FILE,
                config={},
                cli_argv=["delegate"],
                args=None,
                budget=workflow_runtime.Budget(None),
            )
        self.assertIn("k1", state.replay_keys)
        self.assertIsNone(state.replay["k1"])
        self.assertEqual(state.exhausted_keys, {"k1"})
        self.assertNotIn("k1", state.started_without_result)
        self.assertEqual(state.replay["k2"], {"ok": True})


class JournalPreservesResultOrder(unittest.TestCase):
    def test_result_key_order_survives_round_trip(self) -> None:
        result = {"summary": "s", "changed_files": [], "branch": "b", "head": "h"}
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / workflow_registry.JOURNAL_FILE
            workflow_registry.append_jsonl(
                journal, {"seq": 1, "type": "agent_finished", "key": "k", "result": result}
            )
            (event,) = list(workflow_registry.iter_journal(journal))
        self.assertEqual(list(event["result"]), list(result))
        self.assertEqual(repr(event["result"]), repr(result))


class ApprovalAccumulates(unittest.TestCase):
    def test_second_approval_keeps_the_first_gate_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_registry.record_approval(root, "gate-one")
            self.assertTrue(workflow_registry.approval_allows(root, "gate-one"))
            workflow_registry.record_approval(root, "gate-two")
            self.assertTrue(workflow_registry.approval_allows(root, "gate-one"))
            self.assertTrue(workflow_registry.approval_allows(root, "gate-two"))
            self.assertFalse(workflow_registry.approval_allows(root, "gate-three"))
            payload = workflow_registry.read_json(root / workflow_registry.APPROVAL_FILE)
            self.assertEqual(payload["approvedKeys"], ["gate-one", "gate-two"])

    def test_legacy_single_key_file_is_honoured_and_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_registry.write_json(
                root / workflow_registry.APPROVAL_FILE, {"approved": True, "gateKey": "legacy"}
            )
            self.assertTrue(workflow_registry.approval_allows(root, "legacy"))
            workflow_registry.record_approval(root, "next")
            self.assertTrue(workflow_registry.approval_allows(root, "legacy"))
            self.assertTrue(workflow_registry.approval_allows(root, "next"))


if __name__ == "__main__":
    unittest.main()
