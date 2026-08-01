from __future__ import annotations

import tempfile
import unittest

from delegate_agent import (
    config,
    prompt_transport,
    request_build,
    resume_command,
    safe_workspace,
    worktree_execution,
)
from delegate_agent.errors import DelegateError
from delegate_agent.isolation import IsolationContext
from delegate_agent.request_models import ParsedCommand, Request, ResolvedWorkspace


class ResumeSizeGuardTests(unittest.TestCase):
    def test_final_prompt_uses_shared_utf8_argv_boundary_for_all_argv_engines(self):
        limit = prompt_transport.ARGV_PROMPT_GUARD_BYTES
        self.assertEqual(resume_command.ARGV_PROMPT_GUARD_BYTES, limit)
        self.assertEqual(
            prompt_transport.ARGV_PROMPT_TRANSPORT_ENGINES,
            ("cursor", "kimi", "omp"),
        )

        # One 2-byte code point makes the boundary test distinguish characters
        # from the actual UTF-8 byte transport limit.
        exactly_limit = "a" * (limit - 2) + "é"
        over_limit = "a" * (limit - 1) + "é"
        self.assertEqual(len(exactly_limit.encode("utf-8")), limit)
        self.assertEqual(len(over_limit.encode("utf-8")), limit + 1)
        for engine in prompt_transport.ARGV_PROMPT_TRANSPORT_ENGINES:
            with self.subTest(engine=engine):
                resume_command.enforce_resume_prompt_size(engine, exactly_limit)
                with self.assertRaises(DelegateError) as ctx:
                    resume_command.enforce_resume_prompt_size(engine, over_limit)
                self.assertEqual(ctx.exception.error, "resume_prompt_too_large")

    def test_apply_resume_guard_rejects_oversize_final_materialized_prompt(self):
        limit = prompt_transport.ARGV_PROMPT_GUARD_BYTES
        for engine in ("cursor", "kimi", "omp"):
            with self.subTest(engine=engine):
                request = Request(
                    engine=engine,
                    mode="safe",
                    workspace="/workspace",
                    prompt="x" * (limit + 1),
                    argv=[engine],
                    model=None,
                )
                plan = resume_command.ResumePlan(
                    parsed=ParsedCommand("resume"),
                    resumed_from={"runId": "del_source", "alias": "cursor-1"},
                )
                with self.assertRaises(DelegateError) as ctx:
                    resume_command.apply_resume_to_request(request, plan)
                self.assertEqual(ctx.exception.error, "resume_prompt_too_large")

    def test_guard_sees_safe_skill_framing_and_attached_worktree_framing(self):
        limit = prompt_transport.ARGV_PROMPT_GUARD_BYTES
        with tempfile.TemporaryDirectory() as workspace:
            for engine in ("cursor", "kimi", "omp"):
                with self.subTest(engine=engine):
                    request = request_build.build_request(
                        engine,
                        "safe",
                        None,
                        ResolvedWorkspace(workspace, "directory"),
                        "x" * limit,
                        config.embedded_default_config(),
                        False,
                    )
                    final_prompt = request.argv[-1]
                    final_bytes = len(final_prompt.encode("utf-8"))
                    if final_bytes > limit:
                        with self.assertRaises(DelegateError) as safe_ctx:
                            resume_command.apply_resume_to_request(
                                request,
                                resume_command.ResumePlan(
                                    parsed=ParsedCommand("resume"),
                                    resumed_from={"runId": "del_source"},
                                ),
                            )
                        self.assertEqual(safe_ctx.exception.error, "resume_prompt_too_large")
                    else:
                        self.assertEqual(engine, "omp")
                        resume_command.enforce_resume_prompt_size(engine, final_prompt)

                    attached_prompt = worktree_execution._persistent_prompt(
                        "x" * limit,
                        forbid_commit=False,
                    )
                    with self.assertRaises(DelegateError) as attached_ctx:
                        resume_command.enforce_resume_prompt_size(engine, attached_prompt)
                    self.assertEqual(attached_ctx.exception.error, "resume_prompt_too_large")

    def test_resume_of_resume_grows_until_the_shared_guard_refuses_it(self):
        chain = "seed prompt"
        hops = 0
        while len(chain.encode("utf-8")) <= prompt_transport.ARGV_PROMPT_GUARD_BYTES:
            chain = resume_command.build_continuation(
                alias="cursor-1",
                run_id="del_source",
                engine="cursor",
                status="failed",
                source_prompt=chain,
                history_kind="DIGEST",
                history_text="previous output",
                run_output_command="delegate run-output cursor-1",
                extra_instructions="continue the same task",
            )
            hops += 1
            if hops > 200:
                self.fail("resume continuation did not reach its documented argv bound")
        self.assertGreater(hops, 1)
        with self.assertRaises(DelegateError) as ctx:
            resume_command.enforce_resume_prompt_size("cursor", chain)
        self.assertEqual(ctx.exception.error, "resume_prompt_too_large")

    def test_safe_isolation_final_argv_guard_rejects_post_rewrite_overflow(self):
        limit = prompt_transport.ARGV_PROMPT_GUARD_BYTES
        with tempfile.TemporaryDirectory() as workspace:
            prompt = "x" * (limit - 1)
            request = Request(
                engine="cursor",
                mode="safe",
                workspace=workspace,
                prompt=prompt,
                argv=["cursor", prompt],
                model=None,
                isolation_context=IsolationContext(
                    source_workspace=workspace,
                    effective_isolation="worktree",
                    isolation_mode="auto",
                    isolation_lifecycle="temporary",
                    preserved_workspace=False,
                ),
                resumed_from={"runId": "del_source"},
            )
            resume_command.enforce_resume_prompt_size("cursor", request.argv[-1])
            with safe_workspace.safe_isolated_request(request) as isolated:
                self.assertGreater(len(isolated.argv[-1].encode("utf-8")), limit)
                with self.assertRaises(DelegateError) as ctx:
                    resume_command.enforce_resume_prompt_size("cursor", isolated.argv[-1])
            self.assertEqual(ctx.exception.error, "resume_prompt_too_large")


if __name__ == "__main__":
    unittest.main()
