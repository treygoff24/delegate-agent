import importlib.util
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT_PATH = ROOT / "src" / "delegate_agent" / "__init__.py"


def load_package_version() -> str:
    spec = importlib.util.spec_from_file_location("delegate_agent_pkg_under_test", INIT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.VERSION


class PackagingTests(unittest.TestCase):
    def test_package_version_matches_pyproject(self):
        version = load_package_version()
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(version, pyproject["project"]["version"])

    def test_changelog_documents_current_version(self):
        version = load_package_version()
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog)

    def test_docs_linked_from_readme_are_shipped(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        linked = set(re.findall(r"\]\((docs/[^)#\s]+\.md)", readme))
        self.assertIn("docs/delegate-workflows.md", linked)
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        for doc in sorted(linked):
            with self.subTest(doc=doc):
                self.assertTrue((ROOT / doc).is_file(), f"{doc} linked but missing")
                self.assertIn(f"include {doc}", manifest)


if __name__ == "__main__":
    unittest.main()
