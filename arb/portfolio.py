"""Simulazione di portafoglio: capitale diviso in parti uguali tra le venue, leva fissa,
da 1 a K strategie in parallelo con lo stesso notional.

Ogni strategia ha una gamba per venue. Con 2 venue e K strategie, su ogni venue ci sono K gambe
che condividono il margine (cross margin).
"""

import copy
from dataclasses import replace

from .metrics import HOURS_PER_YEAR
from .scan import ScanParams, deepen_many, fetch_markets_index, is_viable, run_scan

# Non bloccanti nello scan, ma esclusi dal portafoglio
PORTFOLIO_EXCLUDE = ("spread attuale negativo", "stessa venue", "orari limitati", "sotto obiettivo")


def maintenance_rate(max_leverage: float) -> float:
    """Margine di mantenimento = 1/(2 × leva massima): regola Hyperliquid, approssimazione per le altre venue."""
    return 1 / (2 * max_leverage)


def isolated_liq_move(leverage: float, max_leverage: float) -> float:
    """Movimento avverso del prezzo che liquida una gamba in isolated margin."""
    return 1 / leverage - maintenance_rate(max_leverage)


def _risk(chosen: list[dict], n: float, margin: float, leverage: float) -> dict:
    by_exchange: dict[str, list[dict]] = {}
    for c in chosen:
        for role in ("long", "short"):
            by_exchange.setdefault(c[role]["exchange"], []).append(c[role])
    iso = min(isolated_liq_move(leverage, leg["max_leverage"]) for legs in by_exchange.values() for leg in legs)
    # Cross margin: shock su un solo asset, le altre gambe della venue restano ferme.
    cross = {
        ex: (margin - sum(maintenance_rate(l["max_leverage"]) * n for l in legs)) / n
        for ex, legs in by_exchange.items()
    }
    return {"isolated_liq_move": iso, "cross_single_shock": min(cross.values()), "cross_by_exchange": cross}


def simulate(client, cfg: dict, conn, p_base: ScanParams, capital: float, leverage: float,
             max_k: int, utilization: float) -> dict:
    venues = p_base.venues
    margin = capital / len(venues)
    leg_capacity = margin * leverage * utilization
    p = replace(p_base, notional_usd=leg_capacity)

    min_apr = cfg.get("strategy", {}).get("min_apr_on_capital")
    # Costi stimati al notional massimo: è il caso K = 1.
    result, _ = run_scan(client, cfg, p, conn)
    pool = sorted(
        (c for c in result["top"]
         if is_viable(c)
         and not any(x in f for f in c["flags"] for x in PORTFOLIO_EXCLUDE)
         and min(c["long"]["max_leverage"], c["short"]["max_leverage"]) >= leverage
         and (min_apr is None or c["eval"]["net_blend"] * leverage / 2 * HOURS_PER_YEAR / p.horizon_h >= min_apr)),
        key=lambda c: c["eval"]["net_blend"], reverse=True,
    )
    markets = fetch_markets_index(client, venues)

    scenarios = []
    for k in range(1, min(max_k, len(pool)) + 1):
        n = leg_capacity / k
        chosen = [copy.deepcopy(c) for c in pool[:k]]
        deepen_many(client, cfg, replace(p, notional_usd=n), conn, chosen, markets)
        strategies = [{
            "id": c["id"],
            "notional": n,
            "exp_usd": c["eval"]["exp_return"] * n,
            "cons_usd": c["eval"]["cons_return"] * n,
            "cost_usd": c["eval"]["cost_rt"] * n,
            "basis_entry": c["costs"]["basis_entry"],
            "net_usd": c["eval"]["net_blend"] * n,
            "breakeven_h": c["eval"]["breakeven_h"],
            "flags": c["flags"],
        } for c in chosen]
        net = sum(s["net_usd"] for s in strategies)
        scenarios.append({
            "k": k,
            "notional_per_leg": n,
            "strategies": strategies,
            "exp_usd": sum(s["exp_usd"] for s in strategies),
            "cons_usd": sum(s["cons_usd"] for s in strategies),
            "cost_usd": sum(s["cost_usd"] for s in strategies),
            "net_usd": net,
            "net_cons_usd": sum(s["cons_usd"] - s["cost_usd"] for s in strategies),
            "roi_horizon": net / capital,
            "apr_on_capital": net / capital * HOURS_PER_YEAR / p.horizon_h,
            **_risk(chosen, n, margin, leverage),
        })
    return {
        "params": {"capital": capital, "leverage": leverage, "utilization": utilization,
                   "margin_per_venue": margin, "leg_capacity": leg_capacity, "venues": venues,
                   "horizon_h": p.horizon_h},
        "pool": [c["id"] for c in pool],
        "scenarios": scenarios,
    }


def render(sim: dict) -> str:
    pr = sim["params"]
    lines = [
        f"portafoglio ${pr['capital']:.0f} · ${pr['margin_per_venue']:.0f} per venue · leva {pr['leverage']:g}x · "
        f"utilizzo {pr['utilization']:.0%} → max ${pr['leg_capacity']:.0f} di notional per venue · "
        f"orizzonte {pr['horizon_h']:.0f}h",
        f"candidate valide: {len(sim['pool'])}",
        f"{'K':>2} {'$/gamba':>8} {'atteso$':>8} {'pessim$':>8} {'costi$':>7} {'netto$':>7} {'netPes$':>8} "
        f"{'ROI':>6} {'APR':>6} {'liqIso':>7} {'shockX':>7}",
    ]
    for s in sim["scenarios"]:
        lines.append(
            f"{s['k']:>2} {s['notional_per_leg']:>8.0f} {s['exp_usd']:>8.2f} {s['cons_usd']:>8.2f} {s['cost_usd']:>7.2f} "
            f"{s['net_usd']:>7.2f} {s['net_cons_usd']:>8.2f} {s['roi_horizon']:>6.2%} {s['apr_on_capital']:>6.1%} "
            f"{s['isolated_liq_move']:>7.0%} {s['cross_single_shock']:>7.0%}"
        )
    if sim["scenarios"]:
        best = max(sim["scenarios"], key=lambda s: s["net_usd"])
        lines.append(f"\nmigliore per netto atteso: K={best['k']}")
        for st in best["strategies"]:
            b = st["basis_entry"]
            lines.append(
                f"  {st['id']:<40} ${st['notional']:.0f}/gamba  atteso {st['exp_usd']:+.2f}  costi {st['cost_usd']:.2f}  "
                f"netto {st['net_usd']:+.2f}  basis {b * 1e4 if b is not None else float('nan'):+.1f}bps  "
                f"BE {st['breakeven_h']}h  {'; '.join(st['flags'])}"
            )
    return "\n".join(lines)
