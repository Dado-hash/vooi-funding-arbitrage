"""Venue combination comparison: where trading pays off."""

import statistics
from collections import Counter, defaultdict

from .scan import fmt_pct, is_viable, venue_pair


def summarize(result: dict, universe: Counter) -> list[dict]:
    """`universe`: number of pairs in the universe per venue combination."""
    notional = result["params"]["notional_usd"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in result["top"] + result["rest"]:
        groups[venue_pair(c)].append(c)
    rows = []
    for vp in set(universe) | set(groups):
        cs = groups.get(vp, [])
        deep = [c for c in cs if c.get("deep")]
        viable = sorted((c for c in deep if is_viable(c)), key=lambda c: c["eval"]["net_blend"], reverse=True)
        top3 = viable[:3]
        # Structural spreads: mean over the whole history ≥ 15% APR and positive ≥ 75% of hours.
        structural = sorted(
            (c for c in cs if c["stats"]["mean_all"] * 8760 >= 0.15 and c["stats"]["pos_all"] >= 0.75),
            key=lambda c: c["stats"]["mean_all"], reverse=True,
        )
        rows.append({
            "n_structural": len(structural),
            "structural_top3_apr": statistics.fmean(c["stats"]["mean_all"] * 8760 for c in structural[:3])
            if structural else 0.0,
            "structural": [c["id"] for c in structural],
            "venues": vp,
            "n_universe": universe.get(vp, 0),
            "n_hist_ok": len(cs),
            "n_deep": len(deep),
            "n_viable": len(viable),
            "top3_net_sum": sum(c["eval"]["net_blend"] for c in top3),
            "top3_usd": sum(c["eval"]["net_blend"] for c in top3) * notional,
            "best": top3[0]["id"] if top3 else None,
            "best_net": top3[0]["eval"]["net_blend"] if top3 else None,
            "median_cost": statistics.median(c["eval"]["cost_rt"] for c in deep) if deep else None,
            "viable": [c["id"] for c in viable],
        })
    rows.sort(key=lambda r: r["top3_net_sum"], reverse=True)
    return rows


def render(rows: list[dict], result: dict) -> str:
    p = result["params"]
    lines = [f"venues · horizon {p['horizon_h']:.0f}h · ${p['notional_usd']:.0f}/leg · "
             f"top3 = summed net of the 3 best valid pairs (they can be held in parallel)",
             f"struct = pairs with mean APR ≥15% and positive ≥75% of hours over the whole history ({p['history_days']}d)",
             f"{'venues':<22} {'univ':>5} {'hist':>5} {'struct':>6} {'sAPR3':>6} {'deep':>5} {'valid':>5} "
             f"{'top3':>7} {'top3$':>7} {'costMed':>7}  best"]
    for r in rows:
        lines.append(
            f"{r['venues']:<22} {r['n_universe']:>5} {r['n_hist_ok']:>5} {r['n_structural']:>6} "
            f"{fmt_pct(r['structural_top3_apr'], 0):>6} {r['n_deep']:>5} {r['n_viable']:>5} "
            f"{fmt_pct(r['top3_net_sum'], 2):>7} {r['top3_usd']:>7.2f} {fmt_pct(r['median_cost'], 2):>7}  "
            f"{r['best'] or '-'} {fmt_pct(r['best_net'], 2) if r['best_net'] is not None else ''}"
        )
    return "\n".join(lines)
