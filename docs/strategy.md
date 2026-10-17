# Strategy and analysis

How `arb` finds, scores and sizes funding-arbitrage pairs. Back to the [README](../README.md).

- [Scan funnel](#scan-funnel)
- [Metrics](#metrics)
- [Basis](#basis)
- [Flags](#flags)
- [Venue comparison](#venue-comparison)
- [Portfolio sizing](#portfolio-sizing)
- [Example findings](#example-findings)
- [Known limitations](#known-limitations)

## Scan funnel

`arb scan` runs four stages:

1. **Universe**: `get_funding_strategies` returns **all** pairs, filtered only by volume, with no threshold on the current APR. If `both_legs = true`, both legs must be in `venues`, as in the VOOI UI. The MCP `exchanges` filter instead accepts a pair even if only one leg is in the list.
2. **History**: for each market involved, it downloads hourly funding with `get_funding_rate_history`, cached in the SQLite table `funding_history`. The spread of each pair is computed locally as `funding_short − funding_long`, which matches `get_funding_spread_chart` exactly. Duration can therefore be evaluated over the whole universe with about N calls instead of N². Ranking only the pairs with the highest current APR selects mostly spikes.
3. **Ranking**: only pairs with a 7-day mean APR ≥ `min_hist_apr` enter, sorted by `score` using the configured fees.
4. **Real costs**: for the top `deep` pairs:
   - `get_markets` (one paginated call per venue) for price, OI, interval and next settlement;
   - `estimate_slippage` for the price impact and fill of each leg, on both sides: entry (long buys, short sells) and exit (long sells, short buys), at `notional_per_strategy`;
   - `get_quotes` for the real fees of the single market (`feesBps`, VOOI fees included).

A first scan of all DEX venues takes about 3 minutes, because it downloads 30 days of history for about 740 markets. Later scans only update the latest hours.

## Metrics

Spread = `short_rate − long_rate`, hourly. Returns are a fraction of the notional of **one** leg, as in the VOOI UI.

| Field | Meaning |
|---|---|
| `APRnow` | Current spread, annualized (VOOI's `netApr`, which is actually gross) |
| `APR24h/72h/7d` | Mean spread over the window, annualized |
| `pos72` | Share of hours with spread > 0 in the last 72h: measures stability |
| `strk` | Consecutive hours with positive spread up to now |
| `hl` | Half-life: hours for the current deviation from the mean to halve (from `phi`, lag-1 autocorrelation). Low = short-lived spike |
| `exp` | Expected funding over the horizon with an AR(1) model (the spread starts at the current value and reverts toward the mean: 50% 72h + 50% 7d), plus the expected basis P&L |
| `cons` | Pessimistic scenario: the lowest mean among 24h, 72h and 7d held for the whole horizon, plus the pessimistic basis P&L |
| `cost` | Round-trip costs: taker fee × 2 legs × (entry + exit) + price impact of entry and exit on both legs, at `notional_per_strategy` |
| `net` | `(exp + cons)/2 − cost`, i.e. the expected net return |
| `score` | `net` annualized over the horizon. **This is the ranking metric** |
| `BE` | Holding hours needed to cover the costs |
| `APR30` / `pos_all` (JSON: `mean_all`, `pos_all`) | Mean and share of positive hours over the whole history (`history_days`, default 30): separates a structural spread from a recent regime |
| `apr_on_capital` | Net annualized return on committed capital, leverage included: `net × leverage/2 × 8760/horizon`. Total margin is 2 legs × notional / leverage, so at 2x it equals the per-leg net. **Minimum target: 20%** (`min_apr_on_capital`) |
| `roi_on_margin` | `net` × common leverage ÷ 2: return on the total margin of both legs |
| `basis_entry` | `(short price − long price)/mid` at entry, from market prices. Positive = favorable |
| `basis_at_size` | Same, from the executable average prices of the entry orders at `notional_per_strategy` (`estimate_slippage`). This is the basis you actually get |
| `basis_mean` | Historical mean basis from price snapshots (`price_snapshots`, one per scan). Used from 24 snapshots onward |

## Basis

The price P&L of a pair is `b₀ − b_exit`, with b = (short price − long price)/mid:

- **expected**: with history, `b₀ − basis_mean` (the pair returns to its normal level). Without history, `b₀ × 0.5` (partial convergence toward zero);
- **pessimistic**: an unfavorable basis converges (a loss), a favorable one persists (zero gain).

`basis_at_size` uses the average execution prices of the entry orders instead of the market prices, so it includes the price impact. A negative basis, at mid or at size (below `[execution] min_entry_basis`, default 0), is a blocking flag: the pair is never opened, whatever the funding spread.

Every scan stores the prices of all markets on the configured venues, so the estimate of the normal basis improves over time. On liquid Hyperliquid/Lighter pairs the entry basis is typically 5-15 bps: small compared to weekly funding, but comparable to the fees.

## Flags

| Flag | Meaning | Blocking |
|---|---|---|
| `costs not covered within the horizon` | Break-even beyond the horizon | ✅ |
| `thin book on <ex> (N% filled)` | Slippage estimate could not fill the notional | ✅ |
| `high price impact on <ex> (N bps entry\|exit)` | Worst of the entry and exit impact of a leg is above `max_impact_bps` (25) | ✅ |
| `negative 24h mean spread` | Paying funding on average over the last day | ✅ |
| `unstable: positive X% of hours over 72h` | `pos72` < 60% | ✅ |
| `cost analysis failed: ...` | Stage 4 errored | ✅ |
| `unfavorable entry basis (X%)` | Entry basis (market prices) below `[execution] min_entry_basis` (0): the long would be bought above the short's price | ✅ |
| `unfavorable basis at size (X%)` | `basis_at_size` below `min_entry_basis` (0) | ✅ |
| `below target: X on capital (min Y)` | `apr_on_capital` under 20%: `arb portfolio` and the cycle skip it | portfolio |
| `negative current spread: entering now pays funding` | | portfolio |
| `same venue, different underlyings: basis risk` | e.g. USBOND against TLT: not pure delta-neutral | portfolio |
| `limited hours on <ex> ...: gap risk` | Market not 24x7 | portfolio |
| `recent regime: ...` | Positive in less than 50% of the hours over the whole history | |
| `spike: current spread >3x the 7d mean` | | |
| `short history (Nh)` | Less than 72 hours of data | |
| `low OI on <ex>` | Open interest under `min_oi_usd` | |
| `slippage not estimated on <ex>` / `exit impact not estimated on <ex>` | `estimate_slippage` failed for the entry or exit side | |
| `settlement on <ex> ...` | 4h/8h venue with the next payment within 60 minutes | |

## Venue comparison

`arb venues` groups pairs by venue combination and analyses the best `--per-pair` of each in depth.

| Column | Meaning |
|---|---|
| `univ` | Pairs in the universe |
| `hist` | Pairs above `min_hist_apr` |
| `struct` | Pairs with mean APR ≥ 15% and positive in ≥ 75% of hours over the whole history |
| `sAPR3` | Mean APR of the 3 best structural pairs |
| `valid` | Pairs with positive net and no blocking flag |
| `top3` | Summed net of the 3 best valid pairs over the horizon (they can be held in parallel) |
| `costMed` | Median round-trip cost |

## Portfolio sizing

`arb portfolio` splits the capital equally across the venues. Without `--capital` it uses the equity reported by `get_accounts` on the configured venues. The maximum notional per venue is `margin × leverage × utilization` (default utilization 90%, leaving a buffer for fees and P&L). With K strategies each leg is worth `maximum notional / K`.

Candidates are valid pairs without the flags `negative current spread`, `same venue`, `limited hours` or `below target`, and whose common maximum leverage is ≥ the chosen leverage. Risk measures:

- `liqIso`: adverse move that liquidates one leg in isolated margin, `1/leverage − 1/(2 × max leverage)`;
- `shockX`: adverse move of a single asset that liquidates a venue's account in cross margin, with the other legs unchanged.

The two legs' P&L sits on different venues: a gain on one does not cover a loss on the other without a transfer. This is why the monitor checks each leg's distance to liquidation and why every opening carries [protective TP/SL](execution.md#protective-tpsl).

## Example findings

Dated analyses from 2026-10-15, included as an example of the output. They are not recommendations and the numbers have moved since.

**Venue comparison** (on-chain venues only, 7-day horizon):

| Venues | Top3 at $1000/leg | Top3 at $5000/leg | Median cost | Notes |
|---|---|---|---|---|
| Aster + Hyperliquid | +2.35% | +1.45% | 0.58% → 1.02% | Driven by RSR and BLUR on thin Aster books (OI $20-28k, slippage 35-50 bps) |
| **Hyperliquid + Lighter** | +1.32% | +1.17% | 0.30% → 0.40% | Lowest fees (4.5 + 2.5 bps), deeper books, 6 structural pairs. Scales best |
| Lighter + Ondo | +0.70% | - | 0.22% | Many valid but small pairs (stocks) |
| Aster + Lighter | +0.09% | +0.07% | 0.63% | Almost nothing |

Hyperliquid + Lighter was chosen. Many excellent one-week spreads (MET, BLUR) turned out to be recent regimes, positive in only 11-32% of hours over 30 days. Opportunities rotate: a broad universe with low costs matters more than any single pair.

**Portfolio simulation** ($1000, $500 per venue, 2x leverage, 7-day horizon):

| K | $/leg | Expected net | Pessimistic net | 7d ROI | APR on capital | Single-asset shock (cross) |
|---|---|---|---|---|---|---|
| 1 | 900 | $8.35 | $6.96 | 0.83% | 43% | 39% |
| 2 | 450 | $5.59 | $4.40 | 0.56% | 29% | 89% |
| 3 | 300 | $4.35 | $3.36 | 0.44% | 23% | 135% |

K=1 is all MET, a recent regime. K=2 adds SAMSUNG: lower return, much lower risk.

## Known limitations

- The fees in `config.toml` come from `get_quotes` without connected exchanges. With a connected account the tier may differ; stage 4 still uses `get_quotes`.
- The basis does not enter the score (only the costs and the flags do).
- Spread history on venues with 4h/8h funding is a step function, so `phi` comes out higher.
- Costs assume taker orders, by design: no limit orders, so that one leg is never filled alone.
- Bridge and collateral transfer costs between venues are not included.
- With `--all-venues` the first scan downloads the history of several thousand markets (several minutes).
- Some Aster assets have two markets (`XAG/USDT` and `XAG/USD1`). The scan resolves the ambiguity by passing the full symbol (`XAGUSDT`).
