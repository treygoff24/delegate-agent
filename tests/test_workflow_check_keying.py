"""``workflow check`` warns about unkeyed steps in scripts that key others."""

from __future__ import annotations

import textwrap
import unittest

from delegate_agent.workflows import script as workflow_script


def warnings_for(source: str) -> list[str]:
    result = workflow_script.check_source(textwrap.dedent(source).strip() + "\n")
    return [w for w in result.warnings if w.startswith("keying warning")]


class UnkeyedStepWarningTests(unittest.TestCase):
    def test_unkeyed_agent_beside_a_keyed_one_is_flagged_with_its_line(self) -> None:
        (warning,) = warnings_for(
            """
            a = agent("settled", key="a")
            b = agent("positional")
            return [a, b]
            """
        )

        self.assertIn("1 agent() call(s) without key=", warning)
        self.assertIn("(line 2)", warning)
        self.assertIn("replay by position and prompt", warning)

    def test_a_script_that_keys_nothing_is_left_alone(self) -> None:
        self.assertEqual(
            warnings_for(
                """
                a = agent("one")
                b = parallel([lambda: agent("two")])
                return [a, b]
                """
            ),
            [],
        )

    def test_fully_keyed_script_has_no_warning(self) -> None:
        self.assertEqual(
            warnings_for(
                """
                a = agent("one", key="a")
                b = parallel([lambda: agent("two", key="b")], key="batch")
                c = pipeline([1], lambda x, y, i: agent("three", key="c"), key="pipe")
                d = workflow("child", key="child")
                return [a, b, c, d]
                """
            ),
            [],
        )

    def test_any_keyed_primitive_makes_the_script_a_keyed_one(self) -> None:
        for keyed in (
            'parallel([lambda: 1], key="batch")',
            'pipeline([1], lambda x, y, i: x, key="pipe")',
            'workflow("child", key="child")',
        ):
            with self.subTest(keyed=keyed):
                (warning,) = warnings_for(f'{keyed}\nreturn agent("positional")\n')
                self.assertIn("agent() call(s) without key=", warning)

    def test_each_unkeyed_primitive_kind_gets_its_own_warning(self) -> None:
        warnings = warnings_for(
            """
            agent("keyed", key="a")
            agent("loose one")
            agent("loose two")
            parallel([lambda: 1])
            pipeline([1], lambda x, y, i: x)
            workflow("child")
            return None
            """
        )

        self.assertEqual(
            [w.split("call(s)")[0].split()[-2:] for w in warnings],
            [["2", "agent()"], ["1", "parallel()"], ["1", "pipeline()"], ["1", "workflow()"]],
        )
        self.assertIn("(line 2, 3)", warnings[0])

    def test_kwargs_expansion_is_assumed_to_carry_a_key(self) -> None:
        self.assertEqual(
            warnings_for(
                """
                opts = {"key": "a"}
                agent("keyed", key="a")
                agent("maybe keyed", **opts)
                return None
                """
            ),
            [],
        )

    def test_long_lists_of_lines_are_capped(self) -> None:
        body = 'agent("keyed", key="a")\n' + 'agent("loose")\n' * 12 + "return None\n"

        (warning,) = warnings_for(body)

        self.assertIn("12 agent() call(s)", warning)
        self.assertIn("and 4 more", warning)

    def test_judges_and_followup_are_never_flagged(self) -> None:
        self.assertEqual(
            warnings_for(
                """
                agent("keyed", key="a", label="x")
                judges("rate", {"type": "object"})
                followup("x", "again")
                return None
                """
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
