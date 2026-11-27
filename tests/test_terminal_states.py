import unittest

from delegate_agent import terminal_states


class TerminalStatesTests(unittest.TestCase):
    def test_ratified_terminal_state_values_are_exact(self):
        self.assertEqual(
            terminal_states.TERMINAL_STATES,
            {
                "completed_verified",
                "completed_unverified",
                "blocked_human",
                "blocked_dependency",
                "provider_refusal",
                "provider_cancelled",
                "provider_max_turns",
                "stalled",
                "failed",
            },
        )

    def test_provider_failure_subset_is_runtime_only(self):
        self.assertEqual(
            terminal_states.PROVIDER_FAILURE_STATES,
            {"provider_refusal", "provider_cancelled", "provider_max_turns"},
        )

    def test_every_terminal_state_has_a_meaning_and_nothing_else_does(self):
        self.assertEqual(
            set(terminal_states.TERMINAL_STATE_MEANINGS), terminal_states.TERMINAL_STATES
        )
        for state, meaning in terminal_states.TERMINAL_STATE_MEANINGS.items():
            with self.subTest(state=state):
                self.assertTrue(meaning.strip())
                self.assertEqual(terminal_states.terminal_state_meaning(state), meaning)
        self.assertIsNone(terminal_states.terminal_state_meaning("not_a_state"))
