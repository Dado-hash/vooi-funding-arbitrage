"""Piano d'ordine da eseguire a mano su VOOI: size identica in base asset sulle due gambe,
arrotondata ai decimali di entrambe le venue, e ordine di esecuzione (prima il book più sottile)."""

import math

from .client import VooiClient


def round_down(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f) / f


def _market(client: VooiClient, exchange: str, symbol: str) -> dict:
    items = client.call("get_markets", {"assets": [symbol], "exchanges": [exchange]}).get("items", [])
    exact = [m for m in items if m["exchange"] == exchange and m["baseSymbol"] == symbol]
    if not exact:
        raise ValueError(f"mercato {symbol} non trovato su {exchange}")
    return exact[0]


def build_plan(long_m: dict, short_m: dict, notional: float, leverage: float) -> dict:
    lp, sp = float(long_m["price"]), float(short_m["price"])
    mid = (lp + sp) / 2
    decimals = min(int(long_m["baseDecimals"]), int(short_m["baseDecimals"]))
    size = round_down(notional / mid, decimals)
    if size <= 0:
        raise ValueError(f"notional ${notional} troppo piccolo per i decimali ({decimals})")
    problems = [
        f"leva {leverage:g}x oltre il massimo di {m['exchange']} ({m['maxLeverage']}x)"
        for m in (long_m, short_m) if m["maxLeverage"] < leverage
    ]
    # Prima la gamba col volume più basso: è quella che rischia di più di non riempirsi bene.
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
    lines = [f"PIANO D'ORDINE{(' · ' + title) if title else ''}"]
    if plan["problems"]:
        lines += ["NON ESEGUIRE:"] + [f"- {p}" for p in plan["problems"]]
    lines += [
        f"1. Imposta leva {plan['leverage']:g}x (cross) su {o[0]['symbol']} su {o[0]['exchange']} "
        f"e su {o[1]['symbol']} su {o[1]['exchange']}.",
        f"2. {o[0]['exchange']}: MARKET {o[0]['side']} {plan['size']:g} {o[0]['symbol']} "
        f"(~${plan['size'] * o[0]['price']:,.2f}) — prima questa, book più sottile.",
        f"3. Subito dopo, {o[1]['exchange']}: MARKET {o[1]['side']} {plan['size']:g} {o[1]['symbol']} "
        f"(~${plan['size'] * o[1]['price']:,.2f}).",
        f"4. Verifica: stessa size ({plan['size']:g}) sulle due gambe, nessun ordine rimasto aperto. "
        f"Margine ~${plan['margin_per_leg']:,.2f} per exchange.",
        "Se il passo 3 fallisce: chiudi subito la gamba del passo 2 (market, reduce-only) e avvisami.",
        f"Basis all'ingresso: {plan['basis_entry'] * 1e4:+.1f} bps (positivo = favorevole).",
    ]
    return "\n".join(lines)
