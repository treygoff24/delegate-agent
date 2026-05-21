#!/usr/bin/env bash
# PostToolUse hook: run the delegate-agent unittest suite after any edit to
# src/delegate_agent/*.py or tests/*.py. Surface failures back to Claude via
# stderr + exit 2 (PostToolUse's "feedback to model" path).
#
# Stdin: JSON from Claude Code with .tool_input.file_path (or .tool_response.filePath)
# Env:   CLAUDE_PROJECT_DIR — repo root

set -u

f=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')

case "$f" in
  */src/delegate_agent/*.py|*/tests/*.py) ;;
  *) exit 0 ;;
esac

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

if ! out=$(python3 -m unittest discover -s tests 2>&1); then
  {
    echo "delegate-agent unittest suite failed after edit to $f:"
    printf '%s\n' "$out" | tail -60
  } >&2
  exit 2
fi

exit 0
