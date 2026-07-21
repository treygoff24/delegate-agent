import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import structured_output  # noqa: E402


class CodexSchemaPreflightTests(unittest.TestCase):
    def test_injects_additional_properties_false_recursively(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                }
            },
            "required": ["items"],
        }

        normalized, injected = structured_output.normalize_codex_schema(schema)

        self.assertIs(normalized["additionalProperties"], False)
        self.assertIs(normalized["properties"]["items"]["items"]["additionalProperties"], False)
        self.assertEqual(injected, ("schema", "schema.properties.items.items"))

    def test_already_strict_schema_is_untouched(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        }
        original = copy.deepcopy(schema)

        normalized, injected = structured_output.normalize_codex_schema(schema)

        self.assertEqual(normalized, original)
        self.assertEqual(schema, original)
        self.assertEqual(injected, ())

    def test_missing_required_property_fails_precisely(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        }

        with self.assertRaises(structured_output.SchemaPreflightError) as ctx:
            structured_output.normalize_codex_schema(schema)

        self.assertIn("schema.required", str(ctx.exception))
        self.assertIn("age", str(ctx.exception))

    def test_explicit_null_additional_properties_is_not_rewritten(self):
        schema = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": None,
        }

        with self.assertRaises(structured_output.SchemaPreflightError) as ctx:
            structured_output.normalize_codex_schema(schema)

        self.assertIn("schema.additionalProperties", str(ctx.exception))

    def test_all_of_object_branches_are_rejected_before_injection(self):
        schema = {
            "allOf": [
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                {
                    "type": "object",
                    "properties": {"age": {"type": "integer"}},
                    "required": ["age"],
                },
            ]
        }

        with self.assertRaises(structured_output.SchemaPreflightError) as ctx:
            structured_output.normalize_codex_schema(schema)

        self.assertIn("schema.allOf", str(ctx.exception))
        self.assertIn("object branches", str(ctx.exception))

    def test_all_of_chained_ref_to_object_is_rejected(self):
        schema = {
            "allOf": [{"$ref": "#/$defs/first"}],
            "$defs": {
                "first": {"$ref": "#/$defs/second"},
                "second": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
        }

        with self.assertRaises(structured_output.SchemaPreflightError) as ctx:
            structured_output.normalize_codex_schema(schema)

        self.assertIn("schema.allOf", str(ctx.exception))
        self.assertIn("object branches", str(ctx.exception))

    def test_pattern_properties_is_rejected_as_unsupported(self):
        schema = {
            "type": "object",
            "patternProperties": {"^[a-z]+$": {"type": "string"}},
        }

        with self.assertRaises(structured_output.SchemaPreflightError) as ctx:
            structured_output.normalize_codex_schema(schema)

        self.assertIn("schema.patternProperties", str(ctx.exception))
        self.assertIn("unsupported", str(ctx.exception))

    def test_external_and_unresolvable_refs_are_rejected(self):
        for ref in ("other.json#/$defs/value", "#/$defs/missing"):
            with self.subTest(ref=ref):
                with self.assertRaises(structured_output.SchemaPreflightError) as ctx:
                    structured_output.normalize_codex_schema({"$ref": ref})

                self.assertIn("schema.$ref", str(ctx.exception))

    def test_resolvable_local_ref_is_normalized_at_its_definition(self):
        schema = {
            "$ref": "#/$defs/result",
            "$defs": {
                "result": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            },
        }

        normalized, injected = structured_output.normalize_codex_schema(schema)

        self.assertIs(normalized["$defs"]["result"]["additionalProperties"], False)
        self.assertEqual(injected, ("schema.$defs.result",))

    def test_resolvable_local_ref_normalizes_a_target_outside_known_keywords(self):
        schema = {
            "$ref": "#/schemas/result",
            "schemas": {
                "result": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            },
        }

        normalized, injected = structured_output.normalize_codex_schema(schema)

        self.assertIs(normalized["schemas"]["result"]["additionalProperties"], False)
        self.assertEqual(injected, ("schema.schemas.result",))


if __name__ == "__main__":
    unittest.main()
