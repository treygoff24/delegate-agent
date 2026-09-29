"""Degraded Runs: a child that ended its turn with its job unfinished.

The true-positive fixtures are the final messages of real Claude work Runs that
were recorded ``succeeded`` / ``resultQuality=ok`` while their full test gate,
or a Monitor they had armed, was still running (2026-09-25 to 2026-09-28). The
false-positive fixtures are real finished or blocked reports that mention
waiting or a background process and must not be flagged.
"""

import json
import unittest

from delegate_agent import degraded, harness_events, terminal_states

MIDJOB_FINALS = {
    "gate_results_next": "Full gate is still running; results come next.",
    "suite_commit_follows": (
        "The full suite is still running in the background. Code, tests, red-proofs, "
        "hand-breaks and the related suites are all done. The commit and final report "
        "follow once the suite finishes."
    ),
    "harness_will_wake": "The suite is still running; the harness will wake me when it finishes.",
    "waiting_on_the_gate": "Waiting on the gate.",
    "cache_build_monitors": (
        "Waiting on the cache build; the monitors will report progress or a traceback."
    ),
    "both_monitors_armed": (
        "Both monitors are armed: one fires on the first progress line, the other when "
        "the run exits. I'll pick up from whichever lands first."
    ),
    "still_running_commit_report": (
        "No mail. The full suite is still running in the background, and I'll finish the "
        "commit and report when it completes."
    ),
    "monitor_armed_waiting": "Monitor armed; waiting for both suites to finish.",
    "monitor_will_report": "Suite still running; the monitor will report the summary when it lands.",
    "pick_up_when_complete": "Both runs are progressing slowly; I'll pick this up when they complete.",
    "commit_when_finishes": "The full suite is still running; I'll commit when it finishes.",
    "enumeration_and_suite": (
        "Waiting on the enumeration and the full suite. The commit and report follow once "
        "both are back."
    ),
    "notified_when_finishes": (
        "Duplicate stopped. The first watcher (bfvq6arj6) and the test run itself are both "
        "still going, and I'll be notified when the run finishes."
    ),
}

FINISHED_OR_BLOCKED_REPORTS = {
    # The tail of a long completion report: the background run died and was not rerun.
    "died_with_previous_session": (
        "## Delegate completion report\n\n"
        "- **Status:** completed. The full suite was not run in this lane; Taproot runs the "
        "full gate on the commit.\n"
        "- **What:**\n  - Reload restore writes only inside the reload\n"
        "- **Full suite:** not completed. The background run died with the previous session, "
        "and per Taproot's instruction I didn't rerun it. Taproot runs the full gate on "
        "`1dedf93`."
    ),
    # Blocked on the requester, not on background work.
    "waiting_on_your_answer": (
        "**Status: blocked at the base check in step 0. I stopped there and made no fixes.**\n\n"
        "**Completion report**\n"
        "- **Status:** blocked.\n"
        "- **Found:** the base mismatch above.\n"
        "- **Files changed:** none by me.\n"
        "- **Remaining:** all of round eight, items 1-11, is waiting on your answer."
    ),
    "no_finite_wait": (
        "- **Risks:**\n"
        "  - Readiness timing is at most 100ms by design. No finite wait catches every slow "
        "producer, and the docs say so."
    ),
    "someone_elses_process": (
        "## Unsure about\n\n"
        "- Another `pytest -q` process was running in the background during my work (started "
        "6:28, not mine). I left it alone. My shards may have been slower because of it, but "
        "their results are unaffected."
    ),
}


class WaitingTextTests(unittest.TestCase):
    def test_every_real_midjob_final_message_is_flagged(self):
        for name, text in MIDJOB_FINALS.items():
            with self.subTest(name):
                self.assertIsNotNone(degraded.waiting_on_unfinished_work(text), text)

    def test_quoted_or_code_text_is_not_the_childs_own_waiting(self):
        for text in (
            'Finished: the tool printed "Waiting on the gate." and exited zero.',
            "Finished. The tool printed \u201cWaiting on the gate.\u201d and exited zero.",
            "Finished: the log line `The suite is still running` was from an old run.",
            "Finished.\n```\nWaiting on the gate.\n```\nThat was the tool output.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(degraded.waiting_on_unfinished_work(text))

    def test_a_real_wait_beside_a_quote_is_still_flagged(self):
        text = 'The tool printed "done". Waiting on the gate.'
        self.assertIsNotNone(degraded.waiting_on_unfinished_work(text))

    def test_waiting_on_a_third_partys_independent_result_is_not_unfinished_work(self):
        for text in (
            "Done. Waiting for CI to post its independent result.",
            "All checks pass locally. I'll wait for the reviewer to finish.",
            "Finished. Waiting on upstream to release the fix.",
            "Done. Waiting on the design decision.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(degraded.waiting_on_unfinished_work(text))

    def test_each_third_party_marker_alone_excludes_a_wait_that_would_otherwise_match(self):
        # "to finish" would make each of these the child's own work; only the named
        # third party says otherwise.
        for who in (
            "CI",
            "the reviewers",
            "a reviewer",
            "the review",
            "a maintainer",
            "the humans",
            "upstream",
            "a third-party service",
            "an independent service",
            "an external service",
            "someone",
            "somebody",
            "my teammates",
            "the owner",
        ):
            with self.subTest(who=who):
                self.assertIsNone(
                    degraded.waiting_on_unfinished_work(f"Waiting on {who} to finish.")
                )

    def test_each_kind_of_own_work_target_alone_makes_a_wait_the_childs_own(self):
        for text in (
            "Waiting on the gate.",
            "Waiting on the suite.",
            "Waiting on the build.",
            "Waiting on it to finish.",
            "Waiting on it to complete.",
            "Waiting on them to be done.",
            "Waiting on the thing to return.",
            "Waiting on the thing to land.",
            "Waiting on the thing to exit.",
            "Waiting on the harness to wake me.",
            "Waiting on the harness to notify me.",
            "Waiting on the harness to ping me.",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(degraded.waiting_on_unfinished_work(text))

    def test_waiting_on_a_job_the_child_ran_is_still_flagged_after_a_done_claim(self):
        for text in (
            "Tests pass. Waiting on the gate.",
            "Done with the code. I'll wait for the build to finish.",
            "Committed. I'll wait for the harness to wake me.",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(degraded.waiting_on_unfinished_work(text))

    def test_each_waiting_pattern_family_is_exercised_by_a_message_only_it_matches(self):
        # The real fixtures above overlap (a message that says "still running" and
        # "will wake me" is caught by two patterns), so a dead pattern could hide.
        # One single-defect message per family, matched by exactly that pattern.
        family_messages = (
            "Waiting on the full gate.",
            "I'll wait for the build to finish.",
            "The full suite is still running.",
            "Monitor armed.",
            "The harness will wake me.",
            "I'll commit once it is done.",
            "The commit follows once both are back.",
        )
        self.assertEqual(len(family_messages), len(degraded._WAITING_PATTERNS))
        for index, message in enumerate(family_messages):
            with self.subTest(message=message):
                matching = [
                    i
                    for i, pattern in enumerate(degraded._WAITING_PATTERNS)
                    if pattern.search(message)
                ]
                self.assertEqual(matching, [index])
                self.assertIsNotNone(degraded.waiting_on_unfinished_work(message))

    def test_finished_and_blocked_reports_are_not_flagged(self):
        for name, text in FINISHED_OR_BLOCKED_REPORTS.items():
            with self.subTest(name):
                self.assertIsNone(degraded.waiting_on_unfinished_work(text), text)

    def test_short_messages_about_waiting_on_the_requester_are_not_flagged(self):
        for text in (
            "Blocked; I'm waiting on your answer before touching the schema.",
            "Waiting for your approval on the rename.",
            "I'll wait for the maintainer to decide.",
        ):
            with self.subTest(text):
                self.assertIsNone(degraded.waiting_on_unfinished_work(text))

    def test_waiting_on_the_requester_to_approve_a_job_is_not_flagged(self):
        # Each names a job the child would run, so only the requester guard (not the
        # own-work target) keeps these out.
        for text in (
            "Waiting on your go-ahead to run the gate.",
            "Waiting for approval to start the build.",
            "I'll wait for the operator's confirmation to run the suite.",
            "I'll wait for you to finish reviewing the build.",
        ):
            with self.subTest(text):
                self.assertIsNone(degraded.waiting_on_unfinished_work(text))

    def test_denials_are_not_flagged(self):
        for text in (
            "Done. There is no need to wait on the gate; it already passed.",
            "All checks passed. I am not waiting on anything.",
            "Finished without waiting for the second suite, which the brief excluded.",
        ):
            with self.subTest(text):
                self.assertIsNone(degraded.waiting_on_unfinished_work(text))

    def test_a_short_report_shaped_message_is_not_flagged(self):
        # The last bullet alone is a flaggable wait; the finished-report shape around
        # it is what keeps the message out.
        bullet = "- Waiting on the gate for the mirror only."
        self.assertIsNotNone(degraded.waiting_on_unfinished_work(bullet))
        text = f"**Status:** done.\n- Fixed the parser.\n{bullet}"
        self.assertIsNone(degraded.waiting_on_unfinished_work(text))

    def test_a_handoff_conditioned_on_someone_else_is_not_flagged(self):
        # "I'll <act> when/after X" is unfinished work only when X is the child's own job.
        for text in (
            "I'll commit when you approve.",
            "I'll report after CI posts the result.",
            "I'll proceed once the maintainer confirms.",
            "I'll follow up after review.",
            "The commit follows once you approve.",
            "The release notes follow after upstream tags the build.",
        ):
            with self.subTest(text):
                self.assertIsNone(degraded.waiting_on_unfinished_work(text))
        for text in (
            "The full suite is still running; I'll commit when it finishes.",
            "I'll report once the build completes.",
        ):
            with self.subTest(text):
                self.assertIsNotNone(degraded.waiting_on_unfinished_work(text))

    def test_a_report_shaped_message_that_says_its_gate_is_still_running_is_flagged(self):
        for text in (
            "Status: completed implementation; the full gate is still running",
            "**Status:** done.\n- Fixed the parser.\n- The full suite is still running.",
            # A real Codex report from the field (wade-litigation-discovery, 2026-08-16).
            "Status: **blocked** (implementation complete; global gate not yet green).\n\n"
            "- Focused Ruff, Pyright, and falsifier tests pass.\n"
            "- Full Pytest is still running; no final exit yet.",
        ):
            with self.subTest(text):
                self.assertIsNotNone(degraded.waiting_on_unfinished_work(text))

    def test_a_report_shaped_message_only_flags_the_childs_own_present_tense_job(self):
        for text in (
            "Status: completed. The full gate was still running when I checked, then passed.",
            "Status: completed. CI's full suite is still running upstream.",
            "Status: completed. The full gate is not still running.",
            '**Status:** done.\n- Fixed it.\n- The log said "the full suite is still running".',
            # Other waiting families stay off inside a report shape.
            "**Status:** done.\n- Fixed the parser.\n- Waiting on the gate for the mirror only.",
        ):
            with self.subTest(text):
                self.assertIsNone(degraded.waiting_on_unfinished_work(text))

    def test_a_long_message_is_never_flagged_even_if_it_says_waiting(self):
        text = "Waiting on the gate. " + ("Details of the finished work. " * 40)
        self.assertGreater(len(text), degraded.WAITING_TEXT_MAX_CHARS)
        self.assertIsNone(degraded.waiting_on_unfinished_work(text))

    def test_empty_text_is_not_flagged(self):
        self.assertIsNone(degraded.waiting_on_unfinished_work(None))
        self.assertIsNone(degraded.waiting_on_unfinished_work(""))
        self.assertIsNone(degraded.waiting_on_unfinished_work("   \n"))

    def test_the_matched_clause_is_returned_for_the_record(self):
        clause = degraded.waiting_on_unfinished_work(
            "Tests pass. Waiting on the gate. Nothing else left."
        )
        self.assertEqual(clause, "Waiting on the gate.")

    def test_the_matched_clause_is_bounded_and_redacted(self):
        secret = "sk-" + "a" * 40
        text = "Waiting on the gate that uses " + secret + " " + ("x" * 300)
        clause = degraded.waiting_on_unfinished_work(text)
        self.assertIsNotNone(clause)
        self.assertLessEqual(len(clause), 161)
        self.assertNotIn(secret, clause)


class AssessTests(unittest.TestCase):
    def test_waiting_text_alone_yields_the_waiting_reason_with_evidence(self):
        result = degraded.assess("Waiting on the gate.")
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, degraded.DEGRADED_ENDED_WAITING)
        self.assertEqual(result.reason, "ended_waiting_on_background_work")
        self.assertEqual(len(result.evidence), 1)
        self.assertIn("Waiting on the gate.", result.evidence[0])

    def test_stream_evidence_alone_yields_the_unfinished_reason(self):
        result = degraded.assess(
            "## Status: done\n\nAll work is committed.",
            background_tasks=["pytest -q full suite"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, degraded.DEGRADED_BACKGROUND_UNFINISHED)
        self.assertIn("pytest -q full suite", result.evidence[0])

    def test_both_signals_keep_the_waiting_reason_and_both_evidence_lines(self):
        result = degraded.assess(
            "Monitor armed; waiting for both suites to finish.",
            background_tasks=["watch suite A", "watch suite B"],
        )
        self.assertEqual(result.reason, degraded.DEGRADED_ENDED_WAITING)
        self.assertEqual(len(result.evidence), 2)
        self.assertIn("2 background task(s)", result.evidence[1])

    def test_a_finished_report_with_no_background_tasks_is_not_degraded(self):
        for name, text in FINISHED_OR_BLOCKED_REPORTS.items():
            with self.subTest(name):
                self.assertIsNone(degraded.assess(text))

    def test_task_descriptions_are_bounded_redacted_and_counted(self):
        secret = "sk-" + "b" * 40
        tasks = [f"job {index} {secret} " + "y" * 200 for index in range(5)]
        result = degraded.assess("Finished the whole plan.", background_tasks=tasks)
        evidence = result.evidence[0]
        self.assertNotIn(secret, evidence)
        self.assertIn("and 2 more", evidence)
        self.assertLess(len(evidence), 600)

    def test_extra_carries_the_record_fields(self):
        extra = degraded.assess("Waiting on the gate.").extra()
        self.assertIs(extra["degraded"], True)
        self.assertEqual(extra["degradedReason"], "ended_waiting_on_background_work")
        self.assertIsInstance(extra["degradedEvidence"], list)

    def test_warning_names_the_reason_and_says_the_run_still_counts_as_succeeded(self):
        for reason in degraded.DEGRADED_REASONS:
            with self.subTest(reason):
                warning = degraded.degraded_warning(reason)
                self.assertIn(reason, warning)
                self.assertIn("succeeded", warning)


def _tasks_changed(*descriptions):
    return {
        "type": "system",
        "subtype": "background_tasks_changed",
        "tasks": [
            {"task_id": f"t{index}", "task_type": "local_bash", "description": description}
            for index, description in enumerate(descriptions)
        ],
        "session_id": "s",
    }


def _result(text="done"):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text}


def _accumulate(events, harness="claude"):
    acc = harness_events.StreamAccumulator(harness=harness)
    for event in events:
        acc.ingest_line(json.dumps(event))
    return acc


class BackgroundTasksAtResultTests(unittest.TestCase):
    def test_tasks_still_listed_when_result_arrives_are_kept(self):
        acc = _accumulate([_tasks_changed("full gate", "watcher"), _result()])
        self.assertEqual(acc.background_tasks_at_result, ("full gate", "watcher"))

    def test_the_post_result_cleanup_snapshot_does_not_erase_them(self):
        acc = _accumulate([_tasks_changed("full gate"), _result(), _tasks_changed()])
        self.assertEqual(acc.background_tasks_at_result, ("full gate",))

    def test_a_task_that_finished_before_result_is_not_kept(self):
        acc = _accumulate([_tasks_changed("full gate"), _tasks_changed(), _result()])
        self.assertEqual(acc.background_tasks_at_result, ())

    def test_only_the_tasks_live_at_result_are_kept(self):
        acc = _accumulate([_tasks_changed("a", "b"), _tasks_changed("b"), _result()])
        self.assertEqual(acc.background_tasks_at_result, ("b",))

    def test_no_result_event_means_no_verdict(self):
        acc = _accumulate([_tasks_changed("full gate")])
        self.assertIsNone(acc.background_tasks_at_result)

    def test_the_last_result_wins_for_a_multi_result_stream(self):
        acc = _accumulate([_tasks_changed("a"), _result(), _tasks_changed(), _result("later")])
        self.assertEqual(acc.background_tasks_at_result, ())

    def test_other_harnesses_never_report_background_tasks(self):
        acc = _accumulate([_tasks_changed("full gate"), _result()], harness="cursor")
        self.assertIsNone(acc.background_tasks_at_result)

    def test_descriptions_are_bounded_and_count_capped(self):
        long_task = "x" * 5000
        acc = _accumulate([_tasks_changed(*([long_task] * 50)), _result()])
        self.assertLessEqual(len(acc.background_tasks_at_result), 20)
        self.assertTrue(all(len(item) <= 300 for item in acc.background_tasks_at_result))

    def test_malformed_task_entries_are_tolerated(self):
        event = {
            "type": "system",
            "subtype": "background_tasks_changed",
            "tasks": ["nope", None, {"task_id": "t9"}, {"description": ""}],
        }
        acc = _accumulate([event, _result()])
        self.assertEqual(len(acc.background_tasks_at_result), 2)
        acc = _accumulate([{**event, "tasks": "not a list"}, _result()])
        self.assertEqual(acc.background_tasks_at_result, ())


class DegradedFieldsTests(unittest.TestCase):
    def test_copies_the_flag_and_reason_from_a_record(self):
        record = {
            "degraded": True,
            "degradedReason": "ended_waiting_on_background_work",
            "degradedEvidence": ["x"],
            "status": "succeeded",
        }
        self.assertEqual(
            degraded.degraded_fields(record),
            {"degraded": True, "degradedReason": "ended_waiting_on_background_work"},
        )

    def test_absent_or_false_flag_yields_nothing(self):
        self.assertEqual(degraded.degraded_fields({"status": "succeeded"}), {})
        self.assertEqual(degraded.degraded_fields({"degraded": False}), {})
        self.assertEqual(degraded.degraded_fields({"degraded": "yes"}), {})
        self.assertEqual(degraded.degraded_fields(None), {})


class CancelledRunIsNotDegradedTests(unittest.TestCase):
    """A Run cancelled while it was being finalized is not "succeeded but degraded"."""

    def _degraded_extra(self):
        verdict = degraded.assess("Waiting on the gate.")
        assert verdict is not None
        extra = dict(verdict.extra())
        extra["warnings"] = ["kept: unrelated warning", verdict.warning()]
        return extra

    def test_strip_degraded_drops_flag_reason_evidence_and_its_warning_only(self):
        extra = self._degraded_extra()
        self.assertTrue(extra["degraded"])
        degraded.strip_degraded(extra)
        for key in degraded.DEGRADED_KEYS:
            self.assertNotIn(key, extra)
        self.assertEqual(extra["warnings"], ["kept: unrelated warning"])

    def test_strip_degraded_tolerates_records_without_warnings(self):
        extra = {"status": "succeeded"}
        degraded.strip_degraded(extra)
        self.assertEqual(extra, {"status": "succeeded"})

    def test_operator_cancel_override_removes_the_degraded_verdict(self):
        extra = self._degraded_extra()
        terminal_states.apply_operator_cancel_override(extra)
        for key in degraded.DEGRADED_KEYS:
            self.assertNotIn(key, extra)
        self.assertEqual(extra["warnings"], ["kept: unrelated warning"])
        self.assertEqual(extra["failureReason"], terminal_states.OPERATOR_CANCEL_REASON)


if __name__ == "__main__":
    unittest.main()
