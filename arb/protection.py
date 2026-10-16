"""Protective TP/SL for a delta-neutral pair.

The two legs sit on different venues: on a big price move one leg approaches liquidation while the
other gains, and the gain does not cover the loss without a transfer. The TP/SL orders close BOTH
legs at the same price, before liquidation:

- price falls to `down`: long stop-loss + short take-profit;
- price rises to `up`: short stop-loss + long take-profit.

Levels sit at `fraction` (e.g. 70%) of the liquidation distance estimated in isolated margin, which
is closer than the real one in cross margin: the estimate is conservative.
"""

import math

from .portfolio import isolated_liq_move


def fmt_price(x: float, max_decimals: int, toward_up: bool) -> str:
    """Price with at most 5 significant figures and `max_decimals` decimals (Hyperliquid rules, also
    accepted by Lighter). Rounds toward the current price, i.e. on the safe side."""
    sig_decimals = 4 - math.floor(math.log10(x))
    d = max(0, min(max_decimals, sig_decimals))
    f = 10 ** d
    v = (math.ceil(x * f) if toward_up else math.floor(x * f)) / f
    return f"{v:.{d}f}"


def tp_sl_levels(long_m: dict, short_m: dict, leverage: float, fraction: float) -> dict:
    lp, sp = float(long_m["price"]), float(short_m["price"])
    mid = (lp + sp) / 2
    liq_long = isolated_liq_move(leverage, float(long_m["maxLeverage"]))    # the long is liquidated if price falls
    liq_short = isolated_liq_move(leverage, float(short_m["maxLeverage"]))  # the short if it rises
    max_dec = min(6 - int(m.get("baseDecimals") or 0) for m in (long_m, short_m))
    down = fmt_price(mid * (1 - fraction * liq_long), max_dec, toward_up=True)
    up = fmt_price(mid * (1 + fraction * liq_short), max_dec, toward_up=False)
    return {
        "down": down, "up": up,
        "down_move": float(down) / mid - 1, "up_move": float(up) / mid - 1,
        "liq_long_move": -liq_long, "liq_short_move": liq_short,
        "fraction": fraction,
    }


def bracket(side: str, levels: dict) -> dict:
    """`create_order` `takeProfit`/`stopLoss` fields for the leg that opens with `side`."""
    if side == "buy":   # long: loses if price falls
        return {"stopLoss": {"triggerPrice": levels["down"]}, "takeProfit": {"triggerPrice": levels["up"]}}
    return {"stopLoss": {"triggerPrice": levels["up"]}, "takeProfit": {"triggerPrice": levels["down"]}}


def check_against_liq(levels: dict, long_liq: float | None, short_liq: float | None) -> list[str]:
    """With positions open: each leg's stop must trigger before its real liquidation."""
    problems = []
    if long_liq and float(levels["down"]) <= long_liq:
        problems.append(f"long stop {levels['down']} beyond liquidation {long_liq:g}")
    if short_liq and float(levels["up"]) >= short_liq:
        problems.append(f"short stop {levels['up']} beyond liquidation {short_liq:g}")
    return problems
