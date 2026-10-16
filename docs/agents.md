# Running with an agent

Set up Telegram and run the hourly monitoring loop with any coding agent. Back to the [README](../README.md).

## Telegram setup

1. In Telegram, create a bot with [@BotFather](https://t.me/BotFather) and put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send any message to your bot, run `uv run arb tg chat-id`, and put the id in `.env` as `TELEGRAM_CHAT_ID`.
3. Test it: `uv run arb tg send "test"`.
4. In the VOOI app, connect your venues (e.g. Hyperliquid and Lighter) with "Connect" and "Enable trading", which includes the builder approval.

## The VOOI token

`.env` holds the MCP endpoint (`https://perps-api.vooi.io/mcp`) and your access token. Without a valid token the server answers `401 Invalid session id`, and an expired token gives HTTP 401. `VOOI_MCP_AUTH_HEADER` and `VOOI_MCP_AUTH_SCHEME` set how the token is sent. If you use Claude Code with the VOOI MCP server, `claude mcp get vooi-perps` shows your configuration.

## Instruction files

| File | Read by | Purpose |
|---|---|---|
| [`AGENTS.md`](../AGENTS.md) | Codex, Cursor, Copilot, Gemini CLI, others | Rules and analyst playbook (canonical) |
| [`CLAUDE.md`](../CLAUDE.md) | Claude Code | Imports `AGENTS.md` |
| [`docs/cycle.md`](cycle.md) | Every agent, each run | Procedure for one monitoring cycle |
| `.claude/skills/cycle/` | Claude Code | The `/cycle` command, pointing to `docs/cycle.md` |

The agent only runs `uv run arb ...`. It does not need MCP access: the CLI has its own VOOI client.

## Running the loop

**Claude Code, interactive**: open a session in the project folder and run

```text
/loop 1h /cycle
```

**Any agent, headless, from cron**: `scripts/run-cycle.sh` runs one cycle with the command in `ARB_AGENT_CMD` (default `claude -p`). Add a line with `crontab -e`:

```cron
# Claude Code
0 * * * * cd /path/to/vooi-funding-arbitrage && claude -p "/cycle" >> data/cycle.log 2>&1
# Codex
0 * * * * cd /path/to/vooi-funding-arbitrage && ARB_AGENT_CMD="codex exec" scripts/run-cycle.sh >> data/cycle.log 2>&1
# Gemini CLI
0 * * * * cd /path/to/vooi-funding-arbitrage && ARB_AGENT_CMD="gemini -p" scripts/run-cycle.sh >> data/cycle.log 2>&1
```

## Permissions

A headless agent must be allowed to run `uv run arb ...` without a confirmation prompt, or the cycle stalls. Allow that command only. For Claude Code, create `.claude/settings.local.json` (git-ignored):

```json
{"permissions": {"allow": ["Bash(uv run arb *)"]}}
```

Other agents have their own allowlist or approval settings. Even with this permission, orders in `execute` mode still need your Telegram tap, and `arb execute` re-checks the limits in `[execution]`.

## Message policy

To avoid spam, the cycle messages you only when it matters:

- **Immediately**: an instruction or proposal for each `[ACTION]` from the monitor (exit, rebalance, unhedged leg, margin), and for each new opportunity or rotation above the thresholds.
- **Immediately**: a plain message when manual intervention is needed, for example a failed safety close.
- **Once a day** (first cycle after 9:00): `arb daily --send` builds a formatted summary with positions, spreads, balances and the top 3 opportunities. The agent can add a one-line note.
- Otherwise, nothing.

## Moving to another machine

You need the project folder, your `.env` and, for Claude Code, `.claude/settings.local.json`. Run `uv sync`. `data/arb.db` rebuilds itself, but the journal and proposals live there: copy it to keep them. An interactive loop runs only while the session is open and the computer is awake.
