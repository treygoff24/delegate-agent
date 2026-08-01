import tempfile
import unittest
from pathlib import Path

from delegate_agent import personas
from delegate_agent.errors import DelegateError
from delegate_agent.request_build import validate_prompt


class PersonaValidationTests(unittest.TestCase):
    def _write_persona(self, root: Path, name: str, data: bytes) -> Path:
        directory = root / ".delegate" / "personas"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.md"
        path.write_bytes(data)
        return path

    def test_name_grammar_is_strict(self):
        valid = ("a", "A-1._", "x" * 64)
        for name in valid:
            with self.subTest(name=name):
                self.assertEqual(personas.validate_persona_name(name), name)

        invalid = ("", "-a", ".a", "a/../b", "/absolute", "a" * 65, "a b")
        for name in invalid:
            with self.subTest(name=name):
                self._assert_invalid_name(name)

    def _assert_invalid_name(self, name: str) -> None:
        with self.assertRaises(DelegateError) as caught:
            personas.validate_persona_name(name)
        self.assertEqual(caught.exception.error, "invalid_persona_name")

    def test_traversal_and_absolute_names_cannot_escape_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("../outside", "/tmp/outside"):
                with self.subTest(name=name), self.assertRaises(DelegateError) as caught:
                    personas.resolve_persona(root, name)
                self.assertEqual(caught.exception.error, "invalid_persona_name")

            persona_dir = root / ".delegate" / "personas"
            persona_dir.mkdir(parents=True)
            outside = root / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            with self.assertRaises(DelegateError) as caught:
                personas._contained_regular_file(outside, persona_dir, name="outside")
            self.assertEqual(caught.exception.error, "invalid_persona_path")

    def test_symlink_and_non_regular_files_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            persona_dir = root / ".delegate" / "personas"
            persona_dir.mkdir(parents=True)
            target = persona_dir / "target.md"
            target.write_text("target", encoding="utf-8")
            (persona_dir / "link.md").symlink_to(target)
            with self.assertRaises(DelegateError) as caught:
                personas.resolve_persona(root, "link")
            self.assertEqual(caught.exception.error, "invalid_persona")

            (persona_dir / "directory.md").mkdir()
            with self.assertRaises(DelegateError) as caught:
                personas.resolve_persona(root, "directory")
            self.assertEqual(caught.exception.error, "invalid_persona")

    def test_utf8_size_cap_is_byte_exact_on_both_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exact = b"x" * personas.PERSONA_MAX_BYTES
            self._write_persona(root, "exact", exact)
            resolved = personas.resolve_persona(root, "exact")
            self.assertEqual(resolved.size_bytes, personas.PERSONA_MAX_BYTES)
            self.assertEqual(resolved.text.encode("utf-8"), exact)

            self._write_persona(root, "too-large", exact + b"x")
            with self.assertRaises(DelegateError) as caught:
                personas.resolve_persona(root, "too-large")
            self.assertEqual(caught.exception.error, "persona_too_large")

            self._write_persona(
                root, "multibyte", ("é" * (personas.PERSONA_MAX_BYTES // 2 + 1)).encode()
            )
            with self.assertRaises(DelegateError) as caught:
                personas.resolve_persona(root, "multibyte")
            self.assertEqual(caught.exception.error, "persona_too_large")

    def test_invalid_utf8_is_rejected_strictly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_persona(root, "bad", b"valid\xff")
            with self.assertRaises(DelegateError) as caught:
                personas.resolve_persona(root, "bad")
            self.assertEqual(caught.exception.error, "invalid_persona_encoding")

    def test_c0_is_refused_even_though_prompt_validation_strips_it(self):
        self.assertEqual(validate_prompt("before\x01after"), "beforeafter")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_persona(root, "control", b"before\x01after")
            with self.assertRaises(DelegateError) as caught:
                personas.resolve_persona(root, "control")
            self.assertEqual(caught.exception.error, "invalid_persona_control")

    def test_preview_escapes_controls_and_is_bounded(self):
        text = "line\ncolumn\tcarriage\r" + ("x" * 200)
        preview = personas.escaped_preview(text)
        self.assertEqual(preview[: len(r"line\ncolumn\tcarriage\r")], r"line\ncolumn\tcarriage\r")
        self.assertLessEqual(len(preview), personas.PERSONA_PREVIEW_MAX_CHARS)
        self.assertNotIn("\n", preview)
        self.assertNotIn("\t", preview)


if __name__ == "__main__":
    unittest.main()
