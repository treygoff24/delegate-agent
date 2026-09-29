"""Delegate Workflows runtime."""

from __future__ import annotations

WORKFLOW_SCHEMA = "delegate.workflow.v1"
# Bump this whenever replay identity changes: how any step key, scope path, or
# gate key is derived. Resume refuses a workflow saved under a different
# version, and `workflow resume --repin` (moving a pin onto the live runtime)
# relies on that refusal as its only guarantee that a journal's step keys mean
# the same thing to the new code. tests/test_workflow_key_stability.py pins
# the derivations so a change cannot ship without this bump.
WORKFLOW_KEY_VERSION = 2
