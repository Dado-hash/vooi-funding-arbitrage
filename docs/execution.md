# Monitoring and execution

What happens after a pair is chosen: monitoring, approvals, order execution and protection. Back to the [README](../README.md).

- [Monitoring](#monitoring)
- [Proposal flow](#proposal-flow)
- [Security](#security)
- [Execution](#execution)
- [Capital check](#capital-check)
- [Protective TP/SL](#protective-tpsl)
- [Telegram messages](#telegram-messages)
- [Known limitations](#known-limitations)

## Monitoring

`arb monitor`:

- pairs positions by asset (strips HIP-3 prefixes and `USDT`/`USDC` suffixes);
- reports unhedged legs;
- checks distance to liquidation (`min_liq_distance`) and notional imbalance (`max_imbalance`);
- computes the expected spread over the next `exit_lookahead_h` hours, and proposes **exit** if it is worse than the exit cost: taker fees of both legs plus the price impact of the exit orders at each leg's notional (`exit_cost`, `exit_cost_usd`). The daily summary shows the exit cost in dollars as `Exit now`;
- compares each position with the best unflagged candidate from the last scan, and proposes a **rotation** if the net gain after exit and new-entry costs beats holding by `rotate_min_uplift`;
- compares positions with the journal (positions that disappeared, positions not recorded).

Each pair gets a status of `OK`, `INFO`, `WARNING` or `ACTION`, with the actions listed below it.

VOOI's `get_positions` does not return a mark price. `normalize_position` derives it from entry price and uPnL, then computes notional and liquidation distance. The format is verified against a real Hyperliquid/Lighter position in `tests/fixtures/positions.json`.

## Proposal flow

In `execute` mode every operation is a proposal:

1. The agent creates it with `arb propose`. The exact order parameters, TP/SL included, are stored in SQLite. The proposal expires after `proposal_ttl_min` (15 minutes).
2. A summary with ✅ Approve and ❌ Reject arrives on Telegram.
3. The agent runs `arb tg wait <id>` in the background. Your tap ends the process and wakes the agent.
4. If you approved, the agent runs `arb execute <id>`.
5. The outcome arrives on Telegram.

Proposals show the price impact of each order at its size, and for openings the basis at size. If an impact is above `max_impact_bps`, a warning says the execution check will block it. Closes show the exit impact for information only.

In `notify` mode, `arb propose` sends instructions instead of buttons: pair and direction with a link to the VOOI Arbitrage Desk, leverage, size per leg, TP/SL prices, what to check after opening, and when not to open. Closing instructions remind you to cancel the leftover TP/SL. At the next cycle the agent finds the position with `arb monitor` and records it in the journal.

## Security

- **Sender**: only taps from your `TELEGRAM_CHAT_ID` count. Anyone else gets "Not authorized".
- **Buttons only**: text messages are ignored and are never instructions.
- **Single use**: an approval covers one proposal, only before it expires, and the proposal runs at most once.
- **Independent limits**: `arb execute` re-checks `[execution]` in `config.toml` (allowed venues, maximum notional per leg, maximum leverage) whatever the proposal contains.
- **Market conditions**: before executing it re-checks the price (maximum drift 1%) and, for openings, that the funding spread is still positive and the basis has not worsened by more than 20 bps.
- **Price impact** (openings only): `estimate_slippage` is called for each order at its size and side. The opening is blocked if the impact is not estimated, if the order would not fill completely, if it is above `max_impact_bps` (25 bps), or if the executable basis at size is more than 20 bps worse than the one in the proposal. Closes are never blocked by impact: a close may be urgent.

If anything fails, the proposal becomes `stale` and a new one is needed.
- **No transfers**: the agent and the CLI never move funds between venues and never withdraw. They only suggest transfers.

## Execution

- **Leverage**: set on both legs.
- **Legs**: one at a time, market orders, thinnest book first. After each order, `get_positions` verifies the size.
- **Second leg failed**: the first leg is closed immediately (reduce-only), so you are never left unhedged.
- **TP/SL**: the opening orders carry them as `stopLoss`/`takeProfit` brackets. After opening, `arb execute` checks that each leg has 2 of them and that the stops trigger before the real liquidation price.
- **Closes**: always reduce-only. After any close, including a safety close, the leftover TP/SL on the closed legs are cancelled, so they cannot hit a future position on the same asset.
- **Journal**: successful openings are recorded automatically.

## Capital check

Before each opening proposal, `arb propose` reads balances with `get_accounts` and compares, per venue, the available balance with the required margin: `notional / leverage × (1 + capital_buffer)`, with a 10% buffer for fees and swings.

| Situation | `notify` | `execute` |
|---|---|---|
| Funds unbalanced across venues | Instructions start with "move $X USDC from A to B" (rounded to $5) | No proposal until the funds are in place |
| Total capital insufficient | Warning with the shortfall, no instructions | Warning with the shortfall, no proposal |

`arb execute` re-checks the margin before placing orders, with a 2% buffer. Hyperliquid HIP-3 markets (`xyz:`, …) have margin separate from the main account: the cycle does not open them.

## Protective TP/SL

The two legs sit on different venues. On a big price move one leg approaches liquidation while the other gains, and that gain does not cover it without a transfer. So every opening gets two price levels shared by both legs (`arb/protection.py`):

| Level | Long leg | Short leg |
|---|---|---|
| **down** | stop-loss | take-profit |
| **up** | take-profit | stop-loss |

Both legs close together at the same price. The levels sit at `tp_sl_liq_fraction` (70%) of the estimated isolated liquidation distance, `1/leverage − 1/(2 × max leverage)`, computed for each leg's venue. In cross margin the real liquidation is further away, so the estimate is conservative. Prices have at most 5 significant digits and are rounded toward the current price.

Example at 2x on MET Hyperliquid/Lighter: levels at −23% and +28%, against estimated liquidations at −33% and +40%.

Example of the mistake this prevents: a short stop-loss at 0.5306 with liquidation at 0.5170. The short is liquidated before its stop fires, and the long stays open alone. Disable the feature with `tp_sl = false` in `[execution]`.

## Telegram messages

Instructions and proposals are Telegram HTML: bold steps, sizes and prices in `code` (copy with a tap), a link to the Arbitrage Desk, and the local expiry time. After a tap, "Approved" or "Rejected" is appended and the formatting is kept. Free-form cycle messages (`arb tg send`) stay plain text. In the terminal the same text prints without tags.

```text
🟢 OPEN MET · long Hyperliquid / short Lighter
ref #9ec2a1 · valid until 11:06

1. Open the pair
Arbitrage Desk → MET
• Leverage 2x on both legs (cross)
• Taker–Taker
• Size 266 MET per leg (~$100)

2. Set TP/SL
• Long Hyperliquid: SL 0.28724 · TP 0.47955
• Short Lighter: SL 0.47955 · TP 0.28724

3. Check
• same size on both legs
• each leg's SL sits between the current price and the liquidation price shown by VOOI

📊 Funding spread now: +35% APR · basis +8.0 bps
```

## Known limitations

- TP/SL brackets on market orders have not yet been exercised by `arb execute` with live orders. VOOI creates them correctly from its Arbitrage Desk: on Hyperliquid as separate trigger orders, on Lighter attached to the position. On first use, check the outcome for "TP/SL ok" or a WARNING.
- The derived mark price can differ by a few bps from the venue's own mark.
- The `get_accounts` format is verified on Hyperliquid and Lighter only. `normalize_account` tries several field names for other venues.
