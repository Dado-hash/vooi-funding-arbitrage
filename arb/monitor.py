"""Controllo delle posizioni aperte: accoppia le gambe, valuta rischio e spread, propone azioni.

`normalize_position` prova più nomi di campo e conserva sempre il record grezzo in `raw`.
Formato reale di VOOI (Hyperliquid e Lighter) in `tests/fixtures/positions.json`: niente mark price,
che si ricava da prezzo d'entrata e uPnL.
"""

from collections import defaultdict
from datetime import datetime, timezone

from .client import MCPError, VooiClient
from .config import taker_fee
from .metrics import expected_cum, spread_stats
from .scan import spread_chart
from .store import journal_list, last_scan, now_iso

LEVELS = ["OK", "INFO", "ATTENZIONE", "AZIONE"]


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def normalize_position(p: dict) -> dict:
    size = _num(_first(p, "size", "positionAmt", "quantity", "qty"))
    side = str(_first(p, "side", "positionSide", "direction") or "").lower()
    if side in ("buy", "long"):
        side = "long"
    elif side in ("sell", "short"):
        side = "short"
    elif size is not None:
        side = "long" if size > 0 else "short"
    entry = _num(_first(p, "entryPrice", "avgEntryPrice"))
    upnl = _num(_first(p, "unrealizedPnl", "unrealizedPnL", "pnl", "upnl"))
    mark = _num(_first(p, "markPrice", "price", "lastPrice"))
    if mark is None and entry and upnl is not None and size and side in ("long", "short"):
        # VOOI non espone il mark: si ricava da uPnL = (mark − entrata) × size con segno.
        mark = entry + upnl / (abs(size) if side == "long" else -abs(size))
    notional = _num(_first(p, "notional", "positionValue", "notionalUsd", "value"))
    if notional is None and size is not None and mark is not None:
        notional = abs(size) * mark
    symbol = str(_first(p, "baseSymbol", "asset", "symbol", "coin") or "")
    return {
        "exchange": _first(p, "exchange", "venue"),
        "symbol": symbol,
        "quote": _first(p, "quoteSymbol", "quote"),
        "side": side,
        "size": abs(size) if size is not None else None,
        "notional": abs(notional) if notional is not None else None,
        "entry_price": entry,
        "mark_price": mark,
        "liq_price": _num(_first(p, "liquidationPrice", "liqPrice", "liquidationPx")),
        "leverage": _num(_first(p, "leverage")),
        "upnl": upnl,
        "funding_fee": _num(_first(p, "fundingFee", "cumFunding")),
        "raw": p,
    }


def fetch_positions(client: VooiClient) -> list[dict]:
    res = client.call("get_positions", {})
    items = res if isinstance(res, list) else (res.get("items") or res.get("positions") or [])
    return [normalize_position(p) for p in items]


def _asset_key(symbol: str) -> str:
    """Chiave di accoppiamento: toglie prefissi HIP-3 (`xyz:`) e suffissi stable (`USDT`)."""
    s = symbol.split(":")[-1].upper()
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def group_pairs(positions: list[dict]) -> tuple[list[dict], list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in positions:
        groups[_asset_key(p["symbol"])].append(p)
    pairs, unmatched = [], []
    for asset, legs in groups.items():
        longs = [p for p in legs if p["side"] == "long"]
        shorts = [p for p in legs if p["side"] == "short"]
        if len(longs) == 1 and len(shorts) == 1:
            pairs.append({"asset": asset, "long": longs[0], "short": shorts[0]})
        else:
            unmatched.extend(legs)
    return pairs, unmatched


def _liq_distance(leg: dict) -> float | None:
    if not leg["mark_price"] or not leg["liq_price"]:
        return None
    return abs(leg["mark_price"] - leg["liq_price"]) / leg["mark_price"]


def _spread_series(client: VooiClient, pair: dict, days: int = 7) -> list[float]:
    leg = lambda p: {"asset": p["symbol"], "exchange": p["exchange"], "quote": p.get("quote")}
    return spread_chart(client, leg(pair["long"]), leg(pair["short"]), days)


def assess_pair(client: VooiClient, cfg: dict, pair: dict, best: dict | None) -> dict:
    mcfg = cfg.get("monitor", {})
    horizon = cfg.get("scan", {}).get("horizon_h", 72)
    look = mcfg.get("exit_lookahead_h", 24)
    l, s = pair["long"], pair["short"]
    actions: list[tuple[str, str]] = []

    for leg in (l, s):
        d = _liq_distance(leg)
        leg["liq_distance"] = d
        if d is not None and d < mcfg.get("min_liq_distance", 0.15):
            actions.append(("AZIONE", f"margine: {leg['exchange']} a {d:.1%} dalla liquidazione"))

    imbalance = None
    if l["notional"] and s["notional"]:
        imbalance = (l["notional"] - s["notional"]) / ((l["notional"] + s["notional"]) / 2)
        if abs(imbalance) > mcfg.get("max_imbalance", 0.05):
            actions.append(("AZIONE", f"ribilancia: delta {imbalance:+.1%} (long vs short)"))

    stats = None
    try:
        stats = spread_stats(_spread_series(client, pair))
    except MCPError as e:
        actions.append(("ATTENZIONE", f"storico spread non disponibile: {e}"))

    exit_cost = taker_fee(cfg, l["exchange"]) + taker_fee(cfg, s["exchange"])
    hold_look = hold_h = None
    if stats:
        hold_look = expected_cum(stats, look)
        hold_h = expected_cum(stats, horizon)
        if hold_look < -exit_cost:
            actions.append(("AZIONE", f"esci: spread atteso {look}h {hold_look:+.3%} peggiore del costo di uscita {exit_cost:.3%}"))
        elif stats.mean_24h < 0:
            actions.append(("ATTENZIONE", f"spread medio 24h negativo ({stats.mean_24h * 8760:+.0%} APR)"))
        elif stats.now < 0:
            actions.append(("INFO", "spread dell'ultima ora negativo"))

        if best and best.get("id") and hold_h is not None:
            switch = best["eval"]["net_exp"] - exit_cost
            uplift = mcfg.get("rotate_min_uplift", 0.5)
            if switch > 0 and switch > max(hold_h, 0) * (1 + uplift):
                actions.append(("ATTENZIONE",
                                 f"valuta rotazione su {best['id']}: atteso {switch:+.3%} vs hold {hold_h:+.3%} su {horizon}h"))

    status = max((a[0] for a in actions), key=LEVELS.index, default="OK")
    return {
        "asset": pair["asset"],
        "id": f"{l['symbol']}:{l['exchange']}>{s['symbol']}:{s['exchange']}",
        "status": status,
        "actions": [f"[{lvl}] {msg}" for lvl, msg in actions],
        "imbalance": imbalance,
        "notional": {"long": l["notional"], "short": s["notional"]},
        "liq_distance": {"long": l.get("liq_distance"), "short": s.get("liq_distance")},
        "upnl": (l["upnl"] or 0) + (s["upnl"] or 0),
        "funding_collected": (l["funding_fee"] or 0) + (s["funding_fee"] or 0),
        "spread": stats.as_dict() if stats else None,
        "expected_next": {f"{look}h": hold_look, f"{horizon}h": hold_h},
    }


def run_monitor(client: VooiClient, cfg: dict, conn) -> dict:
    positions = fetch_positions(client)
    pairs, unmatched = group_pairs(positions)

    scan = last_scan(conn)
    best, scan_age_h = None, None
    if scan:
        scan_age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(scan["ts"])).total_seconds() / 3600
        held = {f"{p['long']['symbol']}:{p['long']['exchange']}>{p['short']['symbol']}:{p['short']['exchange']}" for p in pairs}
        best = next((c for c in scan["top"] if c["id"] not in held and not c.get("flags")), None)

    assessed = [assess_pair(client, cfg, p, best) for p in pairs]
    alerts = [f"[AZIONE] gamba scoperta: {p['side']} {p['symbol']} su {p['exchange']}" for p in unmatched]

    open_journal = journal_list(conn, open_only=True)
    live_ids = {a["id"] for a in assessed}
    for j in open_journal:
        jid = f"{j['long_sym']}:{j['long_ex']}>{j['short_sym']}:{j['short_ex']}"
        if jid not in live_ids:
            alerts.append(f"[ATTENZIONE] journal #{j['id']} {jid} aperto ma posizioni assenti (chiusa o liquidata?)")
    journal_ids = {f"{j['long_sym']}:{j['long_ex']}>{j['short_sym']}:{j['short_ex']}" for j in open_journal}
    for a in assessed:
        if a["id"] not in journal_ids:
            alerts.append(f"[INFO] {a['id']} non registrata nel journal")

    return {
        "ts": now_iso(),
        "n_positions": len(positions),
        "pairs": assessed,
        "alerts": alerts,
        "best_candidate": best["id"] if best else None,
        "scan_age_h": scan_age_h,
    }


def render_report(r: dict) -> str:
    lines = [f"monitor {r['ts']} · {r['n_positions']} posizioni · {len(r['pairs'])} coppie"]
    if r["scan_age_h"] is None:
        lines.append("nessuno scan salvato: esegui `arb scan` per i confronti di rotazione")
    elif r["scan_age_h"] > 6:
        lines.append(f"ultimo scan vecchio di {r['scan_age_h']:.1f}h: riesegui `arb scan`")
    for a in r["pairs"]:
        sp = a["spread"] or {}
        lines.append(
            f"- {a['status']:<10} {a['id']}  uPnL {a['upnl']:+.2f}  funding {a['funding_collected']:+.2f}  "
            f"spread now {sp.get('now', 0) * 8760:+.0%} APR, 24h {sp.get('mean_24h', 0) * 8760:+.0%}"
        )
        lines.extend(f"    {x}" for x in a["actions"])
    lines.extend(r["alerts"])
    return "\n".join(lines)
