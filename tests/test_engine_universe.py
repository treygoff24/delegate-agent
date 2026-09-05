import os
import subprocess
import sys
import unittest
from pathlib import Path

from delegate_agent import config, constants, run_registry


class EngineUniverseTests(unittest.TestCase):
    def test_all_engine_sets_share_the_canonical_vocabulary(self):
        expected = frozenset(constants.KNOWN_ENGINES)
        self.assertEqual(config.SAFE_ISOLATION_REQUIRED_ENGINES, expected)
        self.assertEqual(run_registry.HARNESS_NAMES, expected)

    def test_new_engine_is_not_lost_in_a_second_handwritten_universe(self):
        source = Path(__file__).resolve().parents[1] / "src"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from delegate_agent import constants; "
                "constants.KNOWN_ENGINES=(*constants.KNOWN_ENGINES,'fixture-engine'); "
                "from delegate_agent import config; "
                "assert 'fixture-engine' in config.SAFE_ISOLATION_REQUIRED_ENGINES",
            ],
            env={**os.environ, "PYTHONPATH": str(source)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
