# vooi-funding-arbitrage

Toolkit to choose and monitor **delta-neutral funding-arbitrage** strategies (long on one venue, short on the same underlying on another) through the VOOI perps MCP server. It scans the funding markets, ranks pairs by expected net return over your holding horizon, tracks open positions, and can propose, approve (via Telegram) and execute the orders.

> **Disclaimer.** This is experimental software and not financial advice. Trading perpetual futures with leverage can lose all of your capital, including through liquidation, fees, funding and slippage. Use it at your own risk, read the code before running it, and start with a very small size. The authors are not responsible for any losses.

The goal is not the highest APR right now. It is the best **expected net return over the holding horizon**, accounting for how long a spread lasts and for entry and exit costs. A 1000% APR spread that closes in 20 minutes does not cover the fees.

## Works with any coding agent

The toolkit is a plain Python CLI (`uv run arb ...`). An AI coding agent acts as the **analyst**: it runs scans, interprets results and checks positions periodically. It works with any agent that can run shell commands:

- **Claude Code**: reads `CLAUDE.md` (which imports `AGENTS.md`); the monitoring loop is `/loop 1h /cycle`.
- **Codex, Gemini CLI, Cursor, GitHub Copilot and others**: read `AGENTS.md`, the canonical instructions; the monitoring procedure is `docs/cycle.md`.
- **Headless / cron**: `scripts/run-cycle.sh` runs one cycle with any agent CLI.

The agent never needs MCP access: the CLI has its own client for the VOOI MCP server.

Behavior depends on `TRADING_MODE` in `.env`:

- `notify` (default): the agent sends analysis and step-by-step instructions to Telegram, and you place the orders by hand on VOOI. The agent never places orders (`arb execute` refuses).
- `execute`: every operation arrives as a **proposal with Approve/Reject buttons**. Only after you tap Approve does the agent execute it, via `arb execute`.

Check the active mode with `uv run arb mode`. To change it, edit `.env`.

## Quickstart

```bash
uv sync
cp .env.example .env   # then fill in the VOOI token (and Telegram, optional)
uv run pytest -q       # offline tests on saved real MCP responses
uv run arb scan        # first scan (needs the VOOI token in .env)
```

`.env` holds the MCP endpoint (`https://perps-api.vooi.io/mcp`) and your access token. Without a valid token the server answers `401 Invalid session id`. The header and scheme the token uses are set by `VOOI_MCP_AUTH_HEADER` and `VOOI_MCP_AUTH_SCHEME`; if you use Claude Code, `claude mcp get vooi-perps` shows your configuration.

Default parameters (venues, notional, horizon, thresholds, fees) live in `config.toml`. CLI flags override them.

## Telegram setup (optional, needed for `notify` delivery and `execute` approvals)

1. In Telegram, create a bot with @BotFather and put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send any message to your bot, then run `uv run arb tg chat-id` and put the id in `.env` as `TELEGRAM_CHAT_ID`.
3. Test it with `uv run arb tg send "test"`.
4. Connect Hyperliquid and Lighter in the VOOI app ("Connect" and "Enable trading", which includes the builder approval).

## Running the monitoring loop

The full cycle procedure is in [`docs/cycle.md`](docs/cycle.md). Agents read it and follow it exactly.

**Claude Code**: open a session in the project folder and run

```
/loop 1h /cycle
```

The `/cycle` skill lives in `.claude/skills/cycle/` and points to `docs/cycle.md`.

**Other agents, headless, on a schedule**: `scripts/run-cycle.sh` runs one cycle with the agent command in `ARB_AGENT_CMD` (default `claude -p`). The prompt is `Run one monitoring cycle following docs/cycle.md.`. Examples for cron (`crontab -e`), run every hour:

```cron
# Claude Code (headless)
0 * * * * cd /path/to/vooi-funding-arbitrage && claude -p "/cycle" >> data/cycle.log 2>&1
# Codex
0 * * * * cd /path/to/vooi-funding-arbitrage && ARB_AGENT_CMD="codex exec" scripts/run-cycle.sh >> data/cycle.log 2>&1
# Gemini CLI
0 * * * * cd /path/to/vooi-funding-arbitrage && ARB_AGENT_CMD="gemini -p" scripts/run-cycle.sh >> data/cycle.log 2>&1
```

A headless agent must be allowed to run `uv run arb ...` without asking for confirmation, otherwise the cycle stalls. For Claude Code, put this in `.claude/settings.local.json` (git-ignored):

```json
{"permissions": {"allow": ["Bash(uv run arb *)"]}}
```

Other agents have their own allowlist or approval settings: allow `uv run arb *` only.

Notes:
- The loop runs as long as the session stays open and the computer is on.
- Orders still require a Telegram tap in `execute` mode, and `arb execute` re-checks the limits in `[execution]`.
- To run the project on another machine you need the folder, your `.env` (VOOI token, Telegram, `TRADING_MODE`), `uv` and `uv sync`.
- `data/arb.db` is rebuilt automatically, but the journal and proposals are lost: copy it if you want to keep them.

Messages, to avoid spam:
- **Immediately**: a proposal with buttons for each `[ACTION]` from the monitor (exit, rebalance, unhedged leg), and for each new opportunity or rotation above the playbook thresholds.
- **Immediately**: a message without buttons when manual intervention is needed (for example, a failed safety close).
- **Once a day** (first cycle after 9:00): a summary with positions, funding collected, current spreads and the top 3 opportunities.
- Otherwise, no message.

## Commands

| Command | What it does |
|---|---|
| `uv run arb scan` | Full funnel and table of the best pairs. Saves to `data/arb.db` and `data/scans/latest.json` |
| `uv run arb scan --dex --notional 500` | Same scan on all on-chain venues, with parameter overrides |
| `uv run arb venues --dex --days 30` | Compares venue combinations: universe, structural spreads over 30 days, best 3 valid pairs net of costs |
| `uv run arb portfolio --capital 1000 --leverage 2 --max-k 5` | Simulates the portfolio: capital split across venues, 1 to K strategies in parallel, $ return and liquidation distance |
| `uv run arb plan MET:hyperliquid MET:lighter --notional 450 --leverage 2 [--send]` | Order plan to execute by hand: identical size on both legs (rounded to both venues' decimals), thinnest book first. `--send` posts it to Telegram |
| `uv run arb propose open MET:hyperliquid MET:lighter --notional 1000 --reason "..."` | Creates an opening proposal and sends it to Telegram with Approve/Reject |
| `uv run arb propose close MET:hyperliquid MET:lighter --reason "..."` | Closing proposal (reduce-only, sizes taken from the positions) |
| `uv run arb tg wait <id>` | Waits for the tap on a proposal (long polling). Run in the background: when it ends, the agent executes |
| `uv run arb tg poll` | Reads the taps received and updates the proposals |
| `uv run arb execute <id> [--dry-run]` | Executes an APPROVED proposal (`--dry-run` only runs the checks) and sends the outcome to Telegram |
| `uv run arb proposals [--status pending]` | Lists proposals |
| `uv run arb mode` | Shows `TRADING_MODE` |
| `uv run arb capital [--notional 500 --leverage 2]` | Balances per venue. With `--notional`: required margin and transfers needed to open a strategy |
| `uv run arb daily [--mark]` | Says whether the daily summary is due (`due`/`done`). `--mark` after sending it |
| `uv run arb tg send "text"` / `echo text \| uv run arb tg send` | Sends a message on Telegram |
| `uv run arb tg chat-id` | Shows the chats that wrote to the bot (to find `TELEGRAM_CHAT_ID`) |
| `uv run arb pair CHIP:aster CHIP:hyperliquid` | Full analysis of a single pair (`SYM:exchange` long, then short) |
| `uv run arb monitor [--scan]` | Pairs open positions, checks risk and spreads, proposes actions |
| `uv run arb journal open CHIP:aster CHIP:hyperliquid --notional 1000 --thesis "..."` | Records an opened strategy |
| `uv run arb journal close 1 --reason "negative spread" --realized 4.2` | Closes it |
| `uv run arb journal list [--open]` | Lists journal entries |
| `uv run arb call get_markets '{"assets":["ETH"]}'` | Raw call to any MCP tool |

Add `--json` to `scan`, `venues` and `monitor` for full output. Useful flags for `scan` and `venues`: `--venues a,b`, `--dex`, `--all-venues`, `--days`, `--horizon`, `--notional`, `--min-hist-apr`, `--deep`.

A first scan of all DEX venues takes about 3 minutes, because it downloads 30 days of history for about 740 markets. Later scans only update the latest hours.

## How the scan works

1. **Universe**: `get_funding_strategies` returns **all** pairs, filtered only by volume, with no threshold on the current APR. If `both_legs = true`, both legs must be in `venues`, as in the VOOI UI. The MCP `exchanges` filter instead accepts a pair even if only one leg is in the list.
2. **History**: for each market involved, it downloads hourly funding with `get_funding_rate_history`, cached in the SQLite table `funding_history`. The spread of each pair is computed locally as `funding_short − funding_long`, which matches `get_funding_spread_chart` exactly. Duration can therefore be evaluated over the whole universe with about N calls instead of N². An earlier version analysed only the pairs with the highest current APR, which on all venues selected mostly spikes.
3. **Ranking**: only pairs with a 7-day mean APR ≥ `min_hist_apr` enter, sorted by `score` using the configured fees.
4. **Real costs**: for the top `deep` pairs:
   - `get_markets` (one paginated call per venue) for price, OI, interval and next settlement;
   - `estimate_slippage` for slippage and fill;
   - `get_quotes` for the real fees of the single market (`feesBps`, VOOI fees included).

### Metrics (per pair)

Spread = `short_rate − long_rate`, hourly. Returns are a fraction of the notional of **one** leg, as in the VOOI UI.

| Field | Meaning |
|---|---|
| `APRnow` | Current spread, annualized (VOOI's `netApr`, which is actually gross) |
| `APR24h/72h/7d` | Mean spread over the window, annualized |
| `pos72` | Share of hours with spread > 0 in the last 72h: measures stability |
| `strk` | Consecutive hours with positive spread up to now |
| `hl` | Half-life: hours for the current deviation from the mean to halve (from `phi`, lag-1 autocorrelation). Low = ephemeral peak |
| `exp` | Expected funding over the horizon with an AR(1) model (the spread starts at the current value and reverts toward the mean: 50% 72h + 50% 7d), plus the expected basis P&L |
| `cons` | Pessimistic scenario: the lowest mean among 24h, 72h and 7d held for the whole horizon, plus the pessimistic basis P&L |
| `cost` | Round-trip costs: taker fee × 2 legs × (entry + exit) + slippage × 2 |
| `net` | `(exp + cons)/2 − cost`, i.e. the expected net return |
| `score` | `net` annualized over the horizon. **This is the ranking metric** |
| `BE` | Holding hours needed to cover the costs |
| `APR30`/`pos_all` (JSON: `mean_all`, `pos_all`) | Mean and share of positive hours over the whole history (`history_days`, default 30): separates a structural spread from a recent regime |
| `apr_on_capital` | Net annualized return on committed capital, leverage included: `net × leverage/2 × 8760/horizon`. Total margin is 2 legs × notional / leverage, so at 2x it equals the per-leg net. **Minimum target: 20%** (`min_apr_on_capital` in `[strategy]`) |
| `roi_on_margin` | `net` × common leverage ÷ 2: return on the total margin of both legs |
| `basis_entry` | `(short price − long price)/mid` at entry. Positive = favorable |
| `basis_mean` | Historical mean basis from price snapshots (`price_snapshots`, one per scan). Used from 24 snapshots onward |

### Basis (price difference between venues)

The price P&L of a pair is `b₀ − b_exit`, with b = (short price − long price)/mid:
- **expected**: if there is history, `b₀ − basis_mean` (the pair returns to its normal level). Without history, `b₀ × 0.5` (partial convergence toward zero);
- **pessimistic**: an unfavorable basis converges (a loss), a favorable one persists (zero gain).

Every scan stores the prices of all markets on the configured venues, so the estimate of the normal basis improves over time. On liquid HL/Lighter pairs the entry basis is typically 5-15 bps: small compared to weekly funding, but comparable to the fees.

### Flags

- `short history (Nh)`: not enough history;
- `negative current spread: entering now pays funding`;
- `recent regime: ...` (positive in less than 50% of the hours over the whole history);
- `negative 24h mean spread`;
- `unstable: positive X% of hours over 72h` (pos72 < 60%);
- `spike: current spread >3x the 7d mean`;
- `costs not covered within the horizon`;
- `low OI on <exchange>`;
- `slippage not estimated on <exchange>` / `high slippage on <exchange>` (> 15 bps);
- `thin book on <exchange> (N% filled)`;
- `settlement on <exchange> ...`: imminent payment (venues with a 4h or 8h interval, next payment within 60 minutes);
- `limited hours on <exchange> ...: gap risk` (market not 24x7);
- `same venue, different underlyings: basis risk` (e.g. USBOND against TLT: not pure delta-neutral);
- `unfavorable entry basis`;
- `below target: X on capital (min Y)` (`apr_on_capital` under 20%: does not block the scan, but `arb portfolio` and the cycle do not open these pairs);
- `cost analysis failed: ...`.

Blocking flags (`is_viable` excludes them): `costs not covered`, `thin book`, `negative 24h mean spread`, `unstable`, `cost analysis failed`.

## Venue comparison (`arb venues`)

Groups pairs by venue combination and analyses the best `--per-pair` of each in depth. Columns:
- `univ`: pairs in the universe;
- `hist`: pairs above `min_hist_apr`;
- `struct`: pairs with mean APR ≥ 15% and positive in ≥ 75% of the hours over the whole history;
- `sAPR3`: mean APR of the 3 best structural pairs;
- `valid`: pairs with positive net and no blocking flag;
- `top3`: sum of the net of the 3 best valid pairs over the horizon (they can be held in parallel);
- `costMed`: median round-trip cost.

## Portfolio (`arb portfolio`)

Capital is split equally across the venues. The maximum notional per venue is `margin × leverage × utilization` (default utilization 90%, leaving a buffer for fees and P&L). With K strategies each leg is worth `maximum notional / K`. Candidates are valid pairs without the flags `negative current spread`, `same venue` or `limited hours`, and whose common maximum leverage is ≥ the chosen leverage. Risk measures:
- `liqIso`: adverse move that liquidates one leg in isolated margin, `1/leverage − 1/(2 × max leverage)`;
- `shockX`: adverse move of a single asset that liquidates a venue's account in cross margin, with the other legs unchanged.

The two legs' P&L sits on different venues: a gain on one does not cover a loss on the other without a transfer. For this reason the monitor checks each leg's distance to liquidation.

## Monitoring (`arb monitor`)

- pairs positions by asset (strips HIP-3 prefixes and `USDT`/`USDC` suffixes);
- reports unhedged legs;
- checks distance to liquidation (`min_liq_distance`) and notional imbalance (`max_imbalance`);
- computes the expected spread over the next `exit_lookahead_h` hours. If it is worse than the exit cost, it proposes **exit**;
- compares each position with the best candidate from the last scan without flags. It proposes a **rotation** if the net gain, after exit and new-entry costs, beats holding by `rotate_min_uplift`;
- compares positions with the journal (positions that disappeared, positions not recorded).

## Telegram, approvals and execution

### Proposal flow

1. The agent creates the proposal with `arb propose`. The exact order parameters are stored in SQLite, and the proposal expires after `proposal_ttl_min` (15 minutes).
2. A summary with ✅ Approve and ❌ Reject arrives on Telegram.
3. The agent runs `arb tg wait <id>` in the background. Your tap ends the process and wakes the agent.
4. If you approved, the agent runs `arb execute <id>`.
5. The outcome arrives on Telegram.

### Security

- **Sender**: only taps from your `TELEGRAM_CHAT_ID` count. Anyone else gets "Not authorized".
- **Buttons only**: text messages are ignored and are never instructions.
- **Validity**: an approval covers one proposal only, only before it expires, and the proposal runs at most once.
- **Independent limits**: `arb execute` re-checks `[execution]` in `config.toml` (allowed venues, maximum notional per leg, maximum leverage) whatever the proposal contains.
- **Market conditions**: before executing it re-checks the price (maximum drift 1%) and, for openings, that the funding spread is still positive and the basis has not worsened by more than 20 bps. If anything fails, the proposal becomes `stale` and a new one is needed.

### Execution

- **Leverage**: set on both legs.
- **TP/SL**: the opening orders already carry them (`stopLoss`/`takeProfit` brackets of `create_order`). After opening, `arb execute` checks that there are 2 orders per leg and that the stops trigger before the real liquidation. If something does not match, the outcome says so.
- **Legs**: one at a time, market orders, thinnest book first. After each order, `get_positions` verifies the size.
- **Second leg failed**: the first leg is closed immediately (reduce-only), so you are never left uncovered.
- **Closes**: always reduce-only. After a close, and also after a safety close, the reduce-only orders (TP/SL) left on the closed legs are cancelled: they would stay active and could hit a future position on the same asset.
- **Journal**: successful openings are recorded automatically.
- **Position format**: verified against a real position on Hyperliquid and Lighter (`tests/fixtures/positions.json`). VOOI does not return a mark price: `normalize_position` derives it from the entry price and uPnL, and computes notional and liquidation distance from there. The `arb execute` flow has not yet been tested with live orders.

### Capital check

Before each opening proposal, `arb propose` reads balances with `get_accounts`. For each venue it compares the available balance with the required margin: `notional / leverage × (1 + capital_buffer)`, with a 10% buffer for fees and swings.
- **Funds unbalanced across venues**: it suggests the transfer ("move $X USDC from Lighter to Hyperliquid", on VOOI → Transfer), rounded to $5.
  - `notify`: the transfer is the first step of the message.
  - `execute`: the proposal with buttons is not created until the funds are in place, so you never approve a trade that cannot be executed.
- **Total capital insufficient**: no proposal, only a warning with the shortfall.
- **Execution**: `arb execute` re-checks the margin before placing orders, with a 2% buffer.
- **Transfers**: the agent only suggests them, never executes them.
- **To validate**: the `get_accounts` format has not yet been checked with many connected exchanges (`normalize_account` tries several field names). Check it on the first `arb capital` run with real funds. Hyperliquid HIP-3 markets (`xyz:`, …) have margin separate from the main account: the cycle does not open them.

### Protective TP/SL

The two legs sit on different venues: if the price moves a lot, one leg approaches liquidation while the other gains, and that gain does not cover it without a transfer. For this reason each opening has two price levels, common to both legs (`arb/protection.py`):
- **down** (`down`): stop-loss of the long and take-profit of the short;
- **up** (`up`): stop-loss of the short and take-profit of the long.

Both legs close together at the same price. The levels sit at `tp_sl_liq_fraction` (70%) of the estimated isolated liquidation distance, `1/leverage − 1/(2 × max leverage)`, computed for each leg's venue. In cross margin the real liquidation is further away, so the estimate is conservative. At 2x on MET Hyperliquid/Lighter: −23% and +28%, against estimated liquidations at −33% and +40%. Prices have at most 5 significant digits and are rounded toward the current price.

Example of a mistake this prevents: a short stop-loss at 0.5306 with liquidation at 0.5170. The short would have been liquidated before its stop, leaving the long open alone. You can disable it with `tp_sl = false` in `[execution]`.

### Telegram messages

Instructions (`notify`) and proposals (`execute`) are Telegram HTML: bold titles, sizes and prices in `code` (copyable with a tap), a link to the Arbitrage Desk, and the local expiry time. After a tap, the note "Approved"/"Rejected" is appended at the end and the formatting is kept. Free-form cycle messages (`arb tg send`) stay plain text. In the terminal the same text is printed without tags.

### notify mode

With `TRADING_MODE=notify`, `arb propose` does not send buttons but instructions to follow on VOOI:
- pair and direction, with a link to the Arbitrage Desk page;
- leverage, Taker–Taker order type, size per leg;
- TP/SL per leg, with the prices to enter;
- what to check after opening;
- conditions under which not to open, if the spread or price has changed.

Closing instructions remind you to cancel the leftover TP/SL.

At the next cycle the agent finds the open position with `arb monitor` and records it in the journal.

## Configuration

All defaults are in `config.toml`:

- `[scan]`: venues, notional, horizon, thresholds (`min_hist_apr`, `min_volume_usd`, `min_oi_usd`), depth of cost analysis (`deep`), history length (`history_days`).
- `[venues]`: list of on-chain (`dex`) venues used by `--dex`.
- `[fees.taker]`: estimated taker fee per venue (used in stage 2; stage 3 reads real fees from `get_quotes`).
- `[execution]`: limits for `arb execute` (venues, max notional per leg, max leverage, max price drift, max basis worsening, proposal TTL, TP/SL settings). These apply whatever a proposal contains.
- `[strategy]`: the rules for opening new strategies (`max_strategies`, `notional_per_strategy`, `leverage`, `min_apr_on_capital`, `max_breakeven_h`, `capital_buffer`). The defaults are an example for about $120 of total capital: one strategy, $100 per leg.
- `[monitor]`: thresholds for position alarms and rotation (`min_liq_distance`, `max_imbalance`, `exit_lookahead_h`, `rotate_min_uplift`).

Command-line flags take precedence over `config.toml`. Secrets (VOOI token, Telegram token and chat id) and the trading mode live in `.env`, never in `config.toml`.

## Analyst playbook

The full playbook for the agent is in [`AGENTS.md`](AGENTS.md). In short:

1. Run `arb scan`. Discard pairs with blocking flags.
2. Among the rest, prefer a high `score`, `pos72` ≥ 80%, `BE` well below the horizon, a long half-life, and enough OI and volume for the notional.
3. Be wary of an `APRnow` far above `APR7d`: it is usually a spike.
4. For the best 2-3, run `arb pair` and look at the individual legs' funding history.
5. Size with `arb portfolio`.
6. Propose to the user, with `arb plan` attached.
7. Every operation goes through `arb propose`, then `arb tg wait`, then `arb execute` only if approved.

## Example findings

These are dated analyses from 2026-10-15, included as an example of what the tool produces. They are not recommendations and the numbers have moved since.

### Venue comparison (on-chain venues only, 7-day horizon)

| Venue | Top3 at $1000/leg | Top3 at $5000/leg | Median cost | Notes |
|---|---|---|---|---|
| Aster + Hyperliquid | +2.35% | +1.45% | 0.58% → 1.02% | Driven by RSR and BLUR: thin Aster books (OI $20-28k, slippage 35-50 bps). Without them only MET remains (+0.29% at $5k) |
| **Hyperliquid + Lighter** | +1.32% | +1.17% | 0.30% → 0.40% | Lower fees (4.5 + 2.5 bps), deeper books, 6 structural pairs. Scales better |
| Lighter + Ondo | +0.70% | - | 0.22% | Many valid but small pairs (stocks) |
| Aster + Lighter | +0.09% | +0.07% | 0.63% | Almost nothing |

The choice was **Hyperliquid + Lighter**. Aster can serve as a third venue for outliers such as RSR (941% mean APR over 30 days, positive in 94% of hours), but only with small sizes. Hyperliquid appears in all the best combinations.

Lesson from the 30 days: many excellent one-week spreads (MET, BLUR) are recent regimes, positive in only 11-32% of hours over 30 days. Opportunities rotate: having venues with a broad universe and low costs matters more than any single pair.

### Portfolio simulation (2026-10-15: $1000, $500 per venue, 2x leverage, 7-day horizon)

| K | $/leg | Expected net | Pessimistic net | 7d ROI | APR on capital | Single-asset shock (cross) |
|---|---|---|---|---|---|---|
| 1 | 900 | $8.35 | $6.96 | 0.83% | 43% | 39% |
| 2 | 450 | $5.59 | $4.40 | 0.56% | 29% | 89% |
| 3 | 300 | $4.35 | $3.36 | 0.44% | 23% | 135% |

K=1 is all MET, a recent regime (positive in only 33% of hours over 30 days). With K=2, SAMSUNG is added: it returns less but carries much less risk.

## Known limitations

- The fees in `config.toml` come from `get_quotes` without connected exchanges. With a connected account the tier may differ: stage 3 still uses `get_quotes`.
- TP/SL brackets on market orders have not yet been exercised by `arb execute` with live orders. VOOI created them correctly when opening from the Arbitrage Desk: on Hyperliquid as separate trigger orders, on Lighter attached to the position. On first use, check the outcome ("TP/SL ok" or WARNING).
- The mark price of positions is derived from VOOI's entry price and uPnL: it can differ by a few bps from the venue's mark (on Lighter `data.positionValue` uses a slightly different price).
- Exit slippage is assumed equal to entry slippage. The basis does not enter the score.
- Spread history on venues with 4h/8h funding is a step function: `phi` comes out higher.
- Costs assume taker orders, by design: no limit orders, so that only one leg is never filled alone.
- Bridge and collateral transfer costs between venues are not included.
- With `--all-venues` the first scan downloads the history of several thousand markets (several minutes).
- On Aster some assets have two markets (`XAG/USDT` and `XAG/USD1`). The scan resolves the ambiguity by passing the full pair symbol (`XAGUSDT`). Pairs with both legs on Aster (USDT against USD1) also exist.

## Contributing

Contributions are welcome. Read [`AGENTS.md`](AGENTS.md) for the project rules, which apply to every contributor and every coding agent:

- code, comments, messages and docs are in English;
- run `uv run pytest -q` after every code change;
- update this README (relevant sections) and add an entry to [`CHANGELOG.md`](CHANGELOG.md) for any change to code, parameters or analysis method;
- never commit `.env`, `data/` or `.claude/settings.local.json`.

Tests run offline against saved real responses in `tests/fixtures/` and need no credentials. CI runs them on every push and pull request.

## License

MIT. See [`LICENSE`](LICENSE).
