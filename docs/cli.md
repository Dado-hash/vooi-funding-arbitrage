# CLI and configuration

Every `arb` command and `config.toml` option. Back to the [README](../README.md). Run `uv run arb <command> --help` for all flags.

## Analysis

| Command | What it does |
|---|---|
| `arb scan` | Full funnel and table of the best pairs. Saves to `data/arb.db` and `data/scans/latest.json` |
| `arb scan --dex --notional 500` | Same scan on all on-chain venues, with parameter overrides |
| `arb pair CHIP:aster CHIP:hyperliquid` | Full analysis of one pair (`SYM:exchange` long, then short) |
| `arb venues --dex --days 30` | Compares venue combinations: universe, structural spreads, best 3 valid pairs net of costs |
| `arb portfolio [--capital N] --leverage 2 --max-k 5` | Splits capital across venues, simulates 1 to K strategies, $ return and liquidation distance. Without `--capital`, uses the equity on the configured venues (`get_accounts`) |
| `arb plan MET:hyperliquid MET:lighter --notional 450 [--send]` | Order plan to run by hand: same size on both legs, thinnest book first. `--send` posts it to Telegram |

Flags for `scan` and `venues`: `--venues a,b`, `--dex`, `--all-venues`, `--assets`, `--days`, `--horizon`, `--notional`, `--min-hist-apr`, `--deep`, `--json`.

## Positions and capital

| Command | What it does |
|---|---|
| `arb monitor [--scan] [--json]` | Pairs open positions, checks risk and spreads, suggests actions. `--scan` refreshes the scan first |
| `arb capital [--notional 500 --leverage 2]` | Balances per venue. With `--notional`: required margin and transfers needed |
| `arb journal open LONG SHORT --notional N --thesis "..."` | Records an opened strategy |
| `arb journal close <id> --reason "..." [--realized 4.2]` | Closes a journal entry |
| `arb journal list [--open]` | Lists journal entries |

## Proposals and Telegram

| Command | What it does |
|---|---|
| `arb mode` | Shows `TRADING_MODE` (`notify` or `execute`) |
| `arb propose open LONG SHORT --notional N --reason "..."` | Opening proposal: buttons in `execute`, instructions in `notify` |
| `arb propose close LONG SHORT --reason "..."` | Closing proposal (reduce-only, sizes from the positions) |
| `arb tg wait <id>` | Waits for the tap on a proposal. Run it in the background |
| `arb tg poll` | Reads the taps received and updates the proposals |
| `arb execute <id> [--dry-run]` | Executes an approved proposal. `--dry-run` only runs the checks |
| `arb proposals [--status pending]` | Lists proposals |
| `arb tg send "text"` | Sends a message (`-` or no text reads stdin) |
| `arb tg chat-id` | Shows the chats that wrote to the bot, to find `TELEGRAM_CHAT_ID` |
| `arb daily` | Prints `due` if today's summary is not sent yet (after 9:00 local time), else `done` |
| `arb daily --send [--note "..."]` | Builds the daily summary, sends it to Telegram and marks it sent. `--show` only prints it |
| `arb call <tool> '<json>'` | Raw call to any VOOI MCP tool (use read-only tools only) |

All commands run as `uv run arb ...`.

## Configuration

Secrets and the trading mode live in `.env` (see [`.env.example`](../.env.example)). Everything else is in `config.toml`. CLI flags override it.

| Section | Options |
|---|---|
| `[scan]` | `venues`, `both_legs`, `horizon_h`, `min_hist_apr`, `min_volume_usd`, `min_oi_usd`, `deep`, `history_days` |
| `[venues]` | `dex`: on-chain venues used by `--dex` |
| `[fees.taker]` | Estimated taker fee per venue (stage 2 only; stage 4 reads real fees from `get_quotes`) |
| `[execution]` | Limits for `arb execute`, applied whatever a proposal contains: `venues`, `max_notional_per_leg`, `max_leverage`, `max_price_drift`, `max_basis_worsening`, `max_impact_bps` (max price impact per side, in bps, for the scan flag and the pre-trade check; closes are never blocked), `proposal_ttl_min`, `tp_sl`, `tp_sl_liq_fraction` |
| `[strategy]` | Rules for opening new strategies in the cycle: `max_strategies`, `notional_per_strategy` (also the size at which `arb scan` estimates costs and price impact; `--notional` overrides it), `leverage`, `min_apr_on_capital`, `max_breakeven_h`, `capital_buffer`. The defaults fit about $120 of capital: one strategy, $100 per leg |
| `[monitor]` | Position alarms and rotation: `min_liq_distance`, `max_imbalance`, `exit_lookahead_h`, `rotate_min_uplift` |
