"""Order plan to run by hand on VOOI: identical base-asset size on both legs, rounded to the
decimals of both venues, and execution order (thinner book first)."""

import math

from .client import VooiClient


def round_down(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f) / f


def _market(client: VooiClient, exchange: str, symbol: str) -> dict:
    items = client.call("get_markets", {"assets": [symbol], "exchanges": [exchange]}).get("items", [])
    exact = [m for m in items if m["exchange"] == exchange and m["baseSymbol"] == symbol]
    if not exact:
        raise ValueError(f"market {symbol} not found on {exchange}")
    return exact[0]


def build_plan(long_m: dict, short_m: dict, notional: float, leverage: float) -> dict:
    lp, sp = float(long_m["price"]), float(short_m["price"])
    mid = (lp + sp) / 2
    decimals = min(int(long_m["baseDecimals"]), int(short_m["baseDecimals"]))
    size = round_down(notional / mid, decimals)
    if size <= 0:
        raise ValueError(f"notional ${notional} too small for the size decimals ({decimals})")
    problems = [
        f"leverage {leverage:g}x above the {m['exchange']} maximum ({m['maxLeverage']}x)"
        for m in (long_m, short_m) if m["maxLeverage"] < leverage
    ]
    # Lowest-volume leg first: it is the one most likely to fill badly.
    legs = [("long", long_m, "BUY", lp), ("short", short_m, "SELL", sp)]
    legs.sort(key=lambda x: x[1].get("volume24h") or 0)
    return {
        "size": size,
        "decimals": decimals,
        "notional_long": size * lp,
        "notional_short": size * sp,
        "margin_per_leg": size * mid / leverage,
        "leverage": leverage,
        "basis_entry": (sp - lp) / mid,
        "order": [{"role": r, "exchange": m["exchange"], "symbol": m["baseSymbol"], "quote": m["quoteSymbol"],
                   "side": side, "price": p, "volume24h": m.get("volume24h")} for r, m, side, p in legs],
        "problems": problems,
    }


def order_plan(client: VooiClient, long_spec: str, short_spec: str, notional: float, leverage: float) -> dict:
    long_sym, long_ex = long_spec.rsplit(":", 1)
    short_sym, short_ex = short_spec.rsplit(":", 1)
    return build_plan(_market(client, long_ex, long_sym), _market(client, short_ex, short_sym), notional, leverage)


def render_plan(plan: dict, title: str = "") -> str:
    o = plan["order"]
    lines = [f"ORDER PLAN{(' · ' + title) if title else ''}"]
    if plan["problems"]:
        lines += ["DO NOT EXECUTE:"] + [f"- {p}" for p in plan["problems"]]
    lines += [
        f"1. Set leverage {plan['leverage']:g}x (cross) on {o[0]['symbol']} on {o[0]['exchange']} "
        f"and on {o[1]['symbol']} on {o[1]['exchange']}.",
        f"2. {o[0]['exchange']}: MARKET {o[0]['side']} {plan['size']:g} {o[0]['symbol']} "
        f"(~${plan['size'] * o[0]['price']:,.2f}). This one first: thinner book.",
        f"3. Right after, {o[1]['exchange']}: MARKET {o[1]['side']} {plan['size']:g} {o[1]['symbol']} "
        f"(~${plan['size'] * o[1]['price']:,.2f}).",
        f"4. Check: same size ({plan['size']:g}) on both legs, no order left open. "
        f"Margin ~${plan['margin_per_leg']:,.2f} per exchange.",
        "If step 3 fails: close the step 2 leg immediately (market, reduce-only).",
        f"Entry basis: {plan['basis_entry'] * 1e4:+.1f} bps (positive = favorable).",
    ]
    return "\n".join(lines)
