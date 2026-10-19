# Event schema fixtures

These are minimal synthetic schema fixtures, not recordings of installed CLIs.
Fields/mappings were checked against the following upstream source commits:

- Codex: `openai/codex@c3bb20602d77b0c8e62f84e9956e8086f439dfeb`, `sdk/typescript/src/events.ts`
- Claude: `anthropics/claude-agent-sdk-python@c01a99416a02805b0a050d058da51bde53f6df64`, `src/claude_agent_sdk/types.py` (`ResultMessage`, SDK control requests)
- OpenCode: `anomalyco/opencode@d85746a054924241a7edde736eefd079dff9bc00`, `packages/schema/src/session-status-event.ts`
- AGY screen wording: captured `REAL_TRUST_PROMPT` / `REAL_IDLE` in `tests/test_alerts.py` (AGY 1.1.7).

Interactive Codex/Claude/OpenCode text has no verified screen rules and is UNKNOWN.
JSONL ingestion is opt-in via locally selected structured argv modes; OpenCode's
server-event adapter is available programmatically, not automatically inferred
from its TUI. An idle event does not mean that a task succeeded.
