"""Portfolio simulation: capital split equally across venues, fixed leverage,
1 to K strategies in parallel with the same notional.

Each strategy has one leg per venue. With 2 venues and K strategies, each venue holds K legs
that share margin (cross margin).
"""

import copy
from dataclasses import replace

from .metrics import HOURS_PER_YEAR
from .scan import ScanParams, deepen_many, fetch_markets_index, is_viable, run_scan

# Not blocking in the scan, but excluded from the portfolio
PORTFOLIO_EXCLUDE = ("negative current spread", "same venue", "limited hours", "below target")


def maintenance_rate(max_leverage: float) -> float:
    """Maintenance margin = 1/(2 × max leverage): the Hyperliquid rule, an approximation elsewhere."""
    return 1 / (2 * max_leverage)


def isolated_liq_move(leverage: float, max_leverage: float) -> float:
    """Adverse price move that liquidates one leg in isolated margin."""
    return 1 / leverage - maintenance_rate(max_leverage)


def _risk(chosen: list[dict], n: float, margin: float, leverage: float) -> dict:
    by_exchange: dict[str, list[dict]] = {}
    for c in chosen:
        for role in ("long", "short"):
            by_exchange.setdefault(c[role]["exchange"], []).append(c[role])
    iso = min(isolated_liq_move(leverage, leg["max_leverage"]) for legs in by_exchange.values() for leg in legs)
    # Cross margin: shock on a single asset, the venue's other legs stay still.
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
    # Costs estimated at the maximum notional: the K = 1 case.
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
        f"portfolio ${pr['capital']:.0f} from {pr.get('capital_source', '--capital')} · "
        f"${pr['margin_per_venue']:.0f} per venue · leverage {pr['leverage']:g}x · "
        f"utilization {pr['utilization']:.0%}, max ${pr['leg_capacity']:.0f} notional per venue · "
        f"horizon {pr['horizon_h']:.0f}h",
        f"valid candidates: {len(sim['pool'])}",
        f"{'K':>2} {'$/leg':>8} {'exp$':>8} {'pess$':>8} {'cost$':>7} {'net$':>7} {'netPess$':>8} "
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
        lines.append(f"\nbest by expected net: K={best['k']}")
        for st in best["strategies"]:
            b = st["basis_entry"]
            lines.append(
                f"  {st['id']:<40} ${st['notional']:.0f}/leg  exp {st['exp_usd']:+.2f}  cost {st['cost_usd']:.2f}  "
                f"net {st['net_usd']:+.2f}  basis {b * 1e4 if b is not None else float('nan'):+.1f}bps  "
                f"BE {st['breakeven_h']}h  {'; '.join(st['flags'])}"
            )
    return "\n".join(lines)
