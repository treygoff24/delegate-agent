import json
import unittest

from delegate_agent import config as delegate_config
from delegate_agent import request_build as request_api
from delegate_agent import request_models as request_types
from delegate_agent.errors import DelegateError


def _build(mode: str, **kwargs):
    config = json.loads(json.dumps(delegate_config.embedded_default_config()))
    return request_api.build_request(
        "grok",
        mode,
        None,
        request_types.ResolvedWorkspace("/repo", "git"),
        "review task",
        config,
        dry_run=True,
        **kwargs,
    )


class GrokSafeRefusedTests(unittest.TestCase):
    def test_grok_safe_is_refused_with_worktree_work_guidance(self):
        with self.assertRaises(DelegateError) as ctx:
            _build("safe")
        self.assertEqual(ctx.exception.error, "grok_safe_unsupported")
        message = str(ctx.exception.message)
        self.assertIn("grok safe mode is not supported", message)
        self.assertIn(
            "delegate --isolation worktree grok work --forbid-commit "
            "--model grok-4.7 --reasoning-effort xhigh",
            message,
        )

    def test_grok_work_and_call_are_still_accepted(self):
        self.assertEqual(_build("work").engine, "grok")
        self.assertEqual(_build("call").engine, "grok")


if __name__ == "__main__":
    unittest.main()
