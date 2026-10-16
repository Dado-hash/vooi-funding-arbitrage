#!/usr/bin/env bash
# Run one VOOI monitoring cycle, headless, with any coding-agent CLI.
#
#   scripts/run-cycle.sh                          # default agent: Claude Code (claude -p)
#   ARB_AGENT_CMD="codex exec" scripts/run-cycle.sh
#   ARB_AGENT_CMD="gemini -p"  scripts/run-cycle.sh
#
# The agent command receives the prompt as its last argument. The headless agent must be allowed to
# run `uv run arb ...` without asking for confirmation (see docs/agents.md, "Permissions").
set -euo pipefail

cd "$(dirname "$0")/.."

AGENT_CMD="${ARB_AGENT_CMD:-claude -p}"
PROMPT="Run one monitoring cycle following docs/cycle.md."

read -r -a agent <<< "$AGENT_CMD"
exec "${agent[@]}" "$PROMPT"
