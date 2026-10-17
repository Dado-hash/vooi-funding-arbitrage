"""Strategy selection funnel.

1. Universe: `get_funding_strategies` (every pair, not only those with a high current APR),
   filtered by venue and volume.
2. History: hourly funding per market (`get_funding_rate_history`, SQLite cache). Each pair's spread
   is computed locally, so persistence is evaluated over the WHOLE universe.
3. For the best `deep` pairs: OI and prices (`get_markets`, once per venue) and `estimate_slippage`
   for real costs.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .client import MCPError, VooiClient
from .config import taker_fee
from .history import FundingHistory, pair_spread
from .metrics import HOURS_PER_YEAR, SpreadStats, evaluate, spread_stats
from .store import basis_mean, now_iso, save_prices

WORKERS = 6
BLOCKING_FLAGS = ("costs not covered", "thin book", "negative 24h mean spread",
                  "unstable", "cost analysis failed", "high price impact", "unfavorable entry basis",
                  "unfavorable basis at size")
DEFAULT_MAX_IMPACT_BPS = 25
DEFAULT_MIN_ENTRY_BASIS = 0.0


@dataclass
class ScanParams:
    venues: list[str] = field(default_factory=list)
    both_legs: bool = True
    notional_usd: float = 1000
    horizon_h: float = 168
    min_volume_usd: float = 100_000
    min_oi_usd: float = 50_000
    min_hist_apr: float = 0.05
    deep: int = 15
    history_days: int = 7
    assets: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: dict, **overrides) -> "ScanParams":
        base = {k: v for k, v in cfg.get("scan", {}).items() if k in cls.__dataclass_fields__}
        # Costs and price impact are estimated at the size actually traded: one setting, in [strategy].
        if "notional_per_strategy" in cfg.get("strategy", {}):
            base["notional_usd"] = float(cfg["strategy"]["notional_per_strategy"])
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**base)


def _legs(s: dict) -> tuple[dict, dict]:
    return s["longMarketData"], s["shortMarketData"]


def leg_key(m: dict) -> tuple[str, str, str]:
    return (m["exchange"], m["baseSymbol"], m["quoteSymbol"])


def pair_id(s: dict) -> str:
    l, sh = _legs(s)
    return f"{l['baseSymbol']}:{l['exchange']}>{sh['baseSymbol']}:{sh['exchange']}"


def venue_pair(c: dict) -> str:
    return "+".join(sorted((c["long"]["exchange"], c["short"]["exchange"])))


def is_viable(c: dict) -> bool:
    """Positive expected net return and no blocking flag (see BLOCKING_FLAGS)."""
    flags = c.get("flags", [])
    return c["eval"]["net_blend"] > 0 and not any(b in f for f in flags for b in BLOCKING_FLAGS)


def fetch_strategies(client: VooiClient, p: ScanParams) -> list[dict]:
    args: dict = {}
    if p.venues:
        args["exchanges"] = p.venues
    if p.assets:
        args["assets"] = p.assets
    items = client.paginate("get_funding_strategies", args, max_items=100_000)
    venues = set(p.venues)
    out = []
    for s in items:
        l, sh = _legs(s)
        if (s.get("volume24h") or 0) < p.min_volume_usd:
            continue
        if venues and p.both_legs and not (l["exchange"] in venues and sh["exchange"] in venues):
            continue
        out.append(s)
    return out


def fetch_markets_index(client: VooiClient, venues: list[str]) -> dict[tuple, dict]:
    args = {"exchanges": venues} if venues else {}
    items = client.paginate("get_markets", args, max_items=100_000)
    return {leg_key(m): m for m in items}


# --- per-pair spread chart (used by the monitor and for spot checks) ---

_AMBIGUOUS = re.compile(r'Ambiguous asset "([^"]+)" on (\w+)')


def _disambiguate(err: MCPError, legs: list[dict]) -> bool:
    """On "Ambiguous asset" (e.g. Aster XAG/USDT and XAG/USD1) pass the full pair `XAGUSDT`."""
    m = _AMBIGUOUS.search(str(err))
    if not m:
        return False
    fixed = False
    for leg in legs:
        if leg["asset"] == m.group(1) and leg["exchange"] == m.group(2) and leg.get("quote"):
            leg["asset"] = f"{leg['asset']}{leg['quote']}"
            fixed = True
    return fixed


def spread_chart(client: VooiClient, long: dict, short: dict, days: int) -> list[float]:
    """`long`/`short`: {"asset", "exchange", "quote"?}. Returns the hourly spread, oldest first."""
    legs = [dict(long), dict(short)]
    today = datetime.now(timezone.utc).date()
    for _ in range(3):
        try:
            points = client.call("get_funding_spread_chart", {
                "longAsset": legs[0]["asset"], "longExchange": legs[0]["exchange"],
                "shortAsset": legs[1]["asset"], "shortExchange": legs[1]["exchange"],
                "fromDate": (today - timedelta(days=days)).isoformat(),
                "toDate": today.isoformat(),
            })
            break
        except MCPError as e:
            if not _disambiguate(e, legs):
                raise
    else:
        raise MCPError("ambiguous asset not resolved")
    points = sorted(points or [], key=lambda x: x["date"])
    return [float(x["spread"]) for x in points]


def fetch_slippage(client: VooiClient, exchange: str, symbol: str, notional: float, side: str,
                   quote: str | None = None) -> dict | None:
    args = {"asset": symbol, "exchange": exchange, "notionalUsd": str(notional), "side": side}
    for asset in (symbol, f"{symbol}{quote}" if quote else None):
        if asset is None:
            break
        try:
            return client.call("estimate_slippage", {**args, "asset": asset})
        except MCPError as e:
            if "Ambiguous" not in str(e):
                return None
    return None


def fetch_quote(client: VooiClient, exchange: str, symbol: str, quote: str, notional: float,
                side: str) -> dict | None:
    """`get_quotes` for one market: real fees (feesBps, VOOI fees included) and slippage."""
    try:
        res = client.call("get_quotes", {
            "asset": symbol, "exchanges": [exchange], "quoteSize": str(notional), "side": side, "leverage": "1",
        })
    except MCPError:
        return None
    quotes = [r["quote"] for r in res or [] if r.get("exchange") == exchange and r.get("quote")]
    exact = [q for q in quotes if q.get("quoteSymbol") == quote and q.get("baseSymbol") == symbol]
    same_quote = [q for q in quotes if q.get("quoteSymbol") == quote]
    return (exact or same_quote or [None])[0]


def _minutes_to(iso: str | None) -> float | None:
    if not iso:
        return None
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t - datetime.now(timezone.utc)).total_seconds() / 60


def _base_candidate(s: dict, series: list[float], cfg: dict, p: ScanParams) -> dict | None:
    stats = spread_stats(series, now=float(s["grossSpreadHourly"]))
    if stats is None:
        return None
    l, sh = _legs(s)
    fees_rt = 2 * (taker_fee(cfg, l["exchange"]) + taker_fee(cfg, sh["exchange"]))
    ev = evaluate(stats, p.horizon_h, fees_rt)
    return {
        "id": pair_id(s),
        "asset": s["asset"],
        "long": {"exchange": l["exchange"], "symbol": l["baseSymbol"], "quote": l["quoteSymbol"],
                 "funding_h": float(l["fundingRate"]), "max_leverage": l["maxLeverage"]},
        "short": {"exchange": sh["exchange"], "symbol": sh["baseSymbol"], "quote": sh["quoteSymbol"],
                  "funding_h": float(sh["fundingRate"]), "max_leverage": sh["maxLeverage"]},
        "apr_now": float(s["netApr"]),
        "volume_24h": s.get("volume24h"),
        "stats": stats.as_dict(),
        "costs": {"fees_rt": fees_rt, "slippage_rt": None, "basis_entry": None, "basis_at_size": None},
        "eval": ev.as_dict(),
        "_stats": stats,
    }


def _deepen(client: VooiClient, c: dict, cfg: dict, p: ScanParams, markets: dict,
            basis_hist: tuple[float | None, int]) -> dict:
    stats = c.get("_stats") or SpreadStats(**c["stats"])
    max_impact = float(cfg.get("execution", {}).get("max_impact_bps", DEFAULT_MAX_IMPACT_BPS))
    min_basis = min_entry_basis(cfg)
    fees = []
    # Entry side (long buys, short sells) and exit side (long sells, short buys), both at the notional.
    for role, side, exit_side in (("long", "buy", "sell"), ("short", "sell", "buy")):
        leg = c[role]
        m = markets.get((leg["exchange"], leg["symbol"], leg["quote"]), {})
        slip = fetch_slippage(client, leg["exchange"], leg["symbol"], p.notional_usd, side, leg["quote"])
        exit_slip = fetch_slippage(client, leg["exchange"], leg["symbol"], p.notional_usd, exit_side, leg["quote"])
        q = fetch_quote(client, leg["exchange"], leg["symbol"], leg["quote"], p.notional_usd, side)
        fee = float(q["feesBps"]) / 1e4 if q and q.get("feesBps") is not None else taker_fee(cfg, leg["exchange"])
        fees.append(fee)
        leg.update({
            "taker_fee": fee,
            "fee_source": "get_quotes" if q and q.get("feesBps") is not None else "config",
            "price": float(m["price"]) if m.get("price") else None,
            "open_interest": float(m["openInterest"]) if m.get("openInterest") else None,
            "leg_volume_24h": m.get("volume24h"),
            "funding_interval_h": m.get("fundingInterval"),
            "next_funding_min": _minutes_to(m.get("nextFundingTime")),
            "category": m.get("category"),
            "session": (m.get("schedule") or {}).get("session"),
            "slippage_bps": slip.get("slippageBps") if slip else None,
            "entry_avg_price": slip.get("avgPrice") if slip else None,
            "exit_slippage_bps": exit_slip.get("slippageBps") if exit_slip else None,
            "filled_pct": slip.get("filledPercent") if slip else None,
        })

    # Entry and exit impact of both legs: each round trip pays both sides of both legs.
    impacts = [c[r][k] for r in ("long", "short") for k in ("slippage_bps", "exit_slippage_bps")]
    slippage_rt = sum(x for x in impacts if x is not None) / 1e4
    lp, sp = c["long"]["price"], c["short"]["price"]
    basis = (sp - lp) / ((sp + lp) / 2) if lp and sp else None
    la, sa = c["long"]["entry_avg_price"], c["short"]["entry_avg_price"]
    basis_at_size = (sa - la) / ((sa + la) / 2) if la and sa else None
    fees_rt = 2 * sum(fees)
    cost_rt = fees_rt + slippage_rt
    b_mean, b_points = basis_hist
    c["costs"].update({"fees_rt": fees_rt, "slippage_rt": slippage_rt, "basis_entry": basis,
                       "basis_at_size": basis_at_size,
                       "basis_mean": b_mean, "basis_points": b_points, "total_rt": cost_rt})
    c["notional_usd"] = p.notional_usd
    c["eval"] = evaluate(stats, p.horizon_h, cost_rt, basis, b_mean).as_dict()
    c["roi_on_margin"] = _roi_on_margin(c)
    c["apr_on_capital"] = apr_on_capital(c["eval"]["net_blend"], p.horizon_h, strategy_leverage(cfg))
    c["flags"] = _flags(c, p, max_impact, min_basis)
    target = cfg.get("strategy", {}).get("min_apr_on_capital")
    if target is not None and c["apr_on_capital"] < target:
        c["flags"].append(f"below target: {c['apr_on_capital']:.0%} on capital (min {target:.0%})")
    c["deep"] = True
    return c


def deepen_many(client: VooiClient, cfg: dict, p: ScanParams, conn, cands: list[dict], markets: dict) -> None:
    """Stage 3 in parallel. Historical basis is read first, on the main thread (SQLite)."""
    hist = {
        c["id"]: basis_mean(conn, (c["long"]["exchange"], c["long"]["symbol"], c["long"]["quote"]),
                            (c["short"]["exchange"], c["short"]["symbol"], c["short"]["quote"]))
        for c in cands
    }
    with ThreadPoolExecutor(WORKERS) as ex:
        outcomes = list(ex.map(lambda c: _safe(_deepen, client, c, cfg, p, markets, hist[c["id"]]), cands))
    for c, out in zip(cands, outcomes):
        if isinstance(out, Exception):
            c["flags"] = [f"cost analysis failed: {out}"]


def strategy_leverage(cfg: dict) -> float:
    return float(cfg.get("strategy", {}).get("leverage", 2))


def apr_on_capital(net_return: float, horizon_h: float, leverage: float) -> float:
    """Annualized net return on committed capital. Total margin = 2 legs × notional / leverage,
    so return on capital = net per leg × leverage / 2. At 2x it equals the net per leg."""
    return net_return * leverage / 2 * HOURS_PER_YEAR / horizon_h


def _roi_on_margin(c: dict) -> dict:
    """Net return on total margin (2 legs) at the highest leverage both legs allow."""
    lev = min(c["long"]["max_leverage"], c["short"]["max_leverage"])
    return {"leverage": lev, "net_blend_on_margin": c["eval"]["net_blend"] * lev / 2}


def min_entry_basis(cfg: dict) -> float:
    """Lowest accepted entry basis (short − long)/mid: below it the pair is not opened."""
    return float(cfg.get("execution", {}).get("min_entry_basis", DEFAULT_MIN_ENTRY_BASIS))


def _flags(c: dict, p: ScanParams, max_impact_bps: float = DEFAULT_MAX_IMPACT_BPS,
           min_basis: float = DEFAULT_MIN_ENTRY_BASIS) -> list[str]:
    st, ev, flags = c["stats"], c["eval"], []
    if st["n_hours"] < 72:
        flags.append(f"short history ({st['n_hours']}h)")
    if st["now"] < 0:
        flags.append("negative current spread: entering now pays funding")
    if st["mean_24h"] < 0:
        flags.append("negative 24h mean spread")
    if st["pos_72h"] < 0.6:
        flags.append(f"unstable: positive {st['pos_72h']:.0%} of hours over 72h")
    if st["n_hours"] >= 14 * 24 and st["pos_all"] < 0.5:
        flags.append(f"recent regime: positive only {st['pos_all']:.0%} of hours over {st['n_hours'] // 24}d")
    if st["mean_7d"] > 0 and st["now"] > 3 * st["mean_7d"]:
        flags.append("spike: current spread >3x the 7d mean")
    if ev["breakeven_h"] is None or ev["breakeven_h"] > p.horizon_h:
        flags.append("costs not covered within the horizon")
    for role in ("long", "short"):
        leg = c[role]
        if leg.get("open_interest") is not None and leg["open_interest"] < p.min_oi_usd:
            flags.append(f"low OI on {leg['exchange']} (${leg['open_interest']:,.0f})")
        if leg.get("slippage_bps") is None:
            flags.append(f"slippage not estimated on {leg['exchange']}")
        if leg.get("exit_slippage_bps") is None:
            flags.append(f"exit impact not estimated on {leg['exchange']}")
        impacts = [(leg[k], side) for k, side in (("slippage_bps", "entry"), ("exit_slippage_bps", "exit"))
                   if leg.get(k) is not None]
        if impacts:
            worst, side = max(impacts, key=lambda x: x[0])
            if worst > max_impact_bps:
                flags.append(f"high price impact on {leg['exchange']} ({worst:.0f} bps {side})")
        if leg.get("filled_pct") is not None and leg["filled_pct"] < 100:
            flags.append(f"thin book on {leg['exchange']} ({leg['filled_pct']:.0f}% filled)")
        interval, mins = leg.get("funding_interval_h"), leg.get("next_funding_min")
        if interval and interval >= 4 and mins is not None and mins < 60:
            flags.append(f"settlement on {leg['exchange']} ({interval}h) in {mins:.0f}m")
        if leg.get("session") and leg["session"] != "24x7":
            flags.append(f"limited hours on {leg['exchange']} ({leg['session']}): gap risk")
    if c["long"]["exchange"] == c["short"]["exchange"]:
        flags.append("same venue, different underlyings: basis risk")
    basis = c["costs"].get("basis_entry")
    if basis is not None and basis < min_basis:
        flags.append(f"unfavorable entry basis ({basis:.2%})")
    basis_size = c["costs"].get("basis_at_size")
    if basis_size is not None and basis_size < min_basis:
        flags.append(f"unfavorable basis at size ({basis_size:.2%})")
    return flags


def top_per_venue_pair(k: int) -> Callable[[list[dict]], list[dict]]:
    """Stage 3 selector: the best `k` pairs of each venue combination."""
    def select(candidates: list[dict]) -> list[dict]:
        seen: dict[str, int] = {}
        out = []
        for c in candidates:
            vp = venue_pair(c)
            if seen.get(vp, 0) < k:
                seen[vp] = seen.get(vp, 0) + 1
                out.append(c)
        return out
    return select


def run_scan(client: VooiClient, cfg: dict, p: ScanParams, conn,
             strategies: list[dict] | None = None,
             select_deep: Callable[[list[dict]], list[dict]] | None = None) -> tuple[dict, list[dict]]:
    """Returns (result, raw stage-1 strategies).

    `strategies` skips stage 1. `select_deep` picks the pairs for stage 3
    (default: the first `p.deep` by score).
    """
    if strategies is None:
        strategies = fetch_strategies(client, p)

    history = FundingHistory(client, conn)
    legs = {leg_key(m) for s in strategies for m in _legs(s)}
    refresh = history.refresh(legs, p.history_days + 1)
    cache: dict = {}
    series_of = lambda k: cache.setdefault(k, history.series(k, p.history_days))

    candidates, skipped, below = [], [], 0
    for s in strategies:
        l, sh = _legs(s)
        c = _base_candidate(s, pair_spread(series_of(leg_key(l)), series_of(leg_key(sh))), cfg, p)
        if c is None:
            skipped.append({"id": pair_id(s), "reason": "history missing or too short"})
        elif c["stats"]["mean_7d"] * HOURS_PER_YEAR < p.min_hist_apr:
            below += 1
        else:
            candidates.append(c)
    candidates.sort(key=lambda c: c["eval"]["score_apr"], reverse=True)

    top = (select_deep or (lambda cs: cs[: p.deep]))(candidates)
    ts = now_iso()
    exchanges = sorted(set(p.venues) | {c[r]["exchange"] for c in top for r in ("long", "short")})
    markets = fetch_markets_index(client, exchanges)
    save_prices(conn, ts, markets)
    deepen_many(client, cfg, p, conn, top, markets)
    top.sort(key=lambda c: c["eval"]["score_apr"], reverse=True)
    top_ids = {c["id"] for c in top}
    rest = [c for c in candidates if c["id"] not in top_ids]

    for c in candidates:
        c.pop("_stats", None)
    result = {
        "ts": ts,
        "params": asdict(p),
        "n_strategies": len(strategies),
        "n_analyzed": len(candidates),
        "n_below_hist_apr": below,
        "history": {**refresh, "legs": len(legs)},
        "top": top,
        "rest": rest,
        "skipped": skipped,
    }
    return result, strategies


def _safe(fn, *args):
    try:
        return fn(*args)
    except MCPError as e:
        return e


def fmt_pct(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x * 100:.{digits}f}%"


def render_table(result: dict, rows: list[dict] | None = None) -> str:
    """Compact table meant for analysis (not for the end user)."""
    h = result["params"]["horizon_h"]
    hist = result.get("history", {})
    head = (f"{'#':>2} {'pair':<36} {'APRnow':>7} {'APR24h':>7} {'APR72h':>7} {'APR7d':>7} "
            f"{'pos72':>5} {'strk':>4} {'hl':>5} {'exp':>6} {'cost':>6} {'net':>6} {'score':>7} {'BE':>4}  flags")
    lines = [f"scan {result['ts']} · {result['n_strategies']} strategies · {result['n_analyzed']} with APR7d ≥ "
             f"{result['params']['min_hist_apr']:.0%} · history {hist.get('legs')} markets "
             f"({hist.get('fetched')} fetched, {hist.get('cached')} cached, {len(hist.get('errors', {}))} errors) "
             f"· horizon {h:.0f}h",
             head]
    ann = lambda x: x * HOURS_PER_YEAR
    for i, c in enumerate(rows if rows is not None else result["top"], 1):
        st, ev = c["stats"], c["eval"]
        hl = "-" if st["half_life_h"] is None else f"{st['half_life_h']:.0f}h"
        lines.append(
            f"{i:>2} {c['id']:<36} {fmt_pct(c['apr_now'],0):>7} {fmt_pct(ann(st['mean_24h']),0):>7} "
            f"{fmt_pct(ann(st['mean_72h']),0):>7} {fmt_pct(ann(st['mean_7d']),0):>7} {st['pos_72h']:>5.0%} "
            f"{st['streak_h']:>4} {hl:>5} {fmt_pct(ev['exp_return'],2):>6} {fmt_pct(ev['cost_rt'],2):>6} "
            f"{fmt_pct(ev['net_blend'],2):>6} {fmt_pct(ev['score_apr'],0):>7} {ev['breakeven_h'] or '-':>4}  "
            f"{'; '.join(c.get('flags', []))}"
        )
    return "\n".join(lines)
