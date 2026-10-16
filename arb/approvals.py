"""Operation proposals with approval via Telegram.

A proposal stores the EXACT order parameters (TP/SL included). Approval applies only to that proposal,
until it expires; `arb execute` runs only approved proposals and re-checks the limits.
"""

import json
import secrets
from html import escape as esc
from datetime import datetime, timedelta, timezone

from .client import VooiClient
from .monitor import _asset_key, fetch_positions
from .plan import build_plan, _market
from .protection import bracket, tp_sl_levels
from .scan import fetch_slippage
from .store import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    created_ts TEXT NOT NULL, expires_ts TEXT NOT NULL,
    kind TEXT NOT NULL,              -- open | close
    payload TEXT NOT NULL,           -- exact orders + context (JSON)
    summary TEXT NOT NULL,
    status TEXT NOT NULL,            -- pending | notified | approved | rejected | expired | stale | executed | failed
    decided_ts TEXT, executed_ts TEXT, result TEXT, tg_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def get(conn, pid: str) -> dict | None:
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    if not row:
        return None
    p = dict(row)
    p["payload"] = json.loads(p["payload"])
    p["result"] = json.loads(p["result"]) if p["result"] else None
    return p


def list_proposals(conn, status: str | None = None) -> list[dict]:
    sql = "SELECT id FROM proposals" + (" WHERE status = ?" if status else "") + " ORDER BY created_ts DESC LIMIT 50"
    return [get(conn, r["id"]) for r in conn.execute(sql, (status,) if status else ())]


def set_status(conn, pid: str, status: str, **fields) -> None:
    cols = {"status": status, **fields}
    if "result" in cols and not isinstance(cols["result"], str):
        cols["result"] = json.dumps(cols["result"], default=str)
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE proposals SET {sets} WHERE id = ?", (*cols.values(), pid))
    conn.commit()


def expire_old(conn) -> list[str]:
    now = datetime.now(timezone.utc)
    expired = [p["id"] for p in list_proposals(conn, "pending") if _parse(p["expires_ts"]) < now]
    for pid in expired:
        set_status(conn, pid, "expired")
    return expired


def is_expired(p: dict) -> bool:
    return _parse(p["expires_ts"]) < datetime.now(timezone.utc)


def _insert(conn, kind: str, payload: dict, summary: str, ttl_min: float) -> dict:
    pid = secrets.token_hex(3)
    created = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO proposals (id, created_ts, expires_ts, kind, payload, summary, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, created.isoformat(timespec="seconds"), (created + timedelta(minutes=ttl_min)).isoformat(timespec="seconds"),
         kind, json.dumps(payload), summary, "pending"),
    )
    conn.commit()
    return get(conn, pid)


def _fmt_size(size: float, decimals: int) -> str:
    return f"{size:.{decimals}f}"


def local_hm(ts: str) -> str:
    """Local HH:MM of an ISO UTC timestamp."""
    return _parse(ts).astimezone().strftime("%H:%M")


def _venue(ex: str) -> str:
    return {"hyperliquid": "Hyperliquid", "lighter": "Lighter", "aster": "Aster"}.get(ex, ex.capitalize())


def _tp_sl_lines(pay: dict) -> list[str]:
    """HTML lines with the TP/SL per leg and the reason."""
    lv = pay.get("protection")
    if not lv:
        return []
    lo = next(o for o in pay["orders"] if o["side"] == "buy")
    so = next(o for o in pay["orders"] if o["side"] == "sell")
    return [
        f"• Long {_venue(lo['exchange'])}: SL <code>{lv['down']}</code> · TP <code>{lv['up']}</code>",
        f"• Short {_venue(so['exchange'])}: SL <code>{lv['up']}</code> · TP <code>{lv['down']}</code>",
        f"<i>Same two prices, swapped: at {lv['down_move']:+.0%} or {lv['up_move']:+.0%} both legs close together, "
        f"before liquidation (estimated at {lv['liq_long_move']:+.0%} / {lv['liq_short_move']:+.0%}).</i>",
    ]


def _estimate_impact(client: VooiClient, order: dict) -> None:
    """Stores on the order the price impact (`impact_bps`) and the executable price (`avg_price`)
    for its size and side, as estimated by VOOI's `estimate_slippage`. Both None if unavailable."""
    est = fetch_slippage(client, order["exchange"], order["asset"],
                         float(order["size"]) * float(order["ref_price"]), order["side"], order.get("quote"))
    order["impact_bps"] = est.get("slippageBps") if est else None
    order["avg_price"] = est.get("avgPrice") if est else None


def _basis_at_size(long_o: dict, short_o: dict) -> float | None:
    """(short executable price − long executable price) / mid, at the orders' size."""
    lp, sp = long_o.get("avg_price"), short_o.get("avg_price")
    return (sp - lp) / ((sp + lp) / 2) if lp and sp else None


def propose_open(client: VooiClient, conn, long_spec: str, short_spec: str, notional: float, leverage: float,
                 reason: str, ttl_min: float, tp_sl_fraction: float | None = None,
                 max_impact_bps: float | None = None) -> dict:
    """`tp_sl_fraction`: if set, the opening orders carry TP/SL at that fraction of the distance
    to liquidation (see `protection`). `max_impact_bps`: limit shown in the proposal (checked at execution)."""
    long_sym, long_ex = long_spec.rsplit(":", 1)
    short_sym, short_ex = short_spec.rsplit(":", 1)
    lm, sm = _market(client, long_ex, long_sym), _market(client, short_ex, short_sym)
    plan = build_plan(lm, sm, notional, leverage)
    if plan["problems"]:
        raise ValueError("; ".join(plan["problems"]))
    size = _fmt_size(plan["size"], plan["decimals"])
    levels = tp_sl_levels(lm, sm, leverage, tp_sl_fraction) if tp_sl_fraction else None
    orders = [{"exchange": o["exchange"], "asset": o["symbol"], "quote": o["quote"], "side": o["side"].lower(),
               "size": size, "reduceOnly": False, "ref_price": o["price"],
               **(bracket(o["side"].lower(), levels) if levels else {})} for o in plan["order"]]
    for o in orders:
        _estimate_impact(client, o)
    long_o = next(o for o in orders if o["side"] == "buy")
    short_o = next(o for o in orders if o["side"] == "sell")
    payload = {
        "pair": f"{long_sym}:{long_ex}>{short_sym}:{short_ex}",
        "leverage": leverage, "notional_per_leg": notional, "orders": orders,
        "ref_basis": plan["basis_entry"],
        "ref_basis_at_size": _basis_at_size(long_o, short_o),
        "max_impact_bps": max_impact_bps,
        "ref_spread_h": float(sm["fundingRate"]) - float(lm["fundingRate"]),
        "protection": levels,
        "reason": reason,
    }
    lines = [
        f"🟢 <b>OPEN {esc(long_sym)}</b> · long {_venue(long_ex)} / short {_venue(short_ex)}",
        f"Leverage <b>{leverage:g}x</b> · <b>${notional:,.0f}</b> per leg · size <code>{size}</code>",
        "",
        "<b>Orders</b> (market, one at a time)",
    ] + [f"{i}. {_venue(o['exchange'])}: {o['side'].upper()} {o['size']} {esc(o['asset'])}"
         for i, o in enumerate(orders, 1)]
    if levels:
        lines += ["", "<b>TP/SL included in the orders</b>"] + _tp_sl_lines(payload)
    lines += ["", f"📊 Funding spread now: <b>{payload['ref_spread_h'] * 8760:+.0%} APR</b> · "
                  f"basis {plan['basis_entry'] * 1e4:+.1f} bps"]
    lines += _impact_lines(payload)
    if reason:
        lines += ["", "<b>Why</b>", esc(reason)]
    return _insert(conn, "open", payload, "\n".join(lines), ttl_min)


def propose_close(client: VooiClient, conn, long_spec: str, short_spec: str, reason: str, ttl_min: float) -> dict:
    long_sym, long_ex = long_spec.rsplit(":", 1)
    short_sym, short_ex = short_spec.rsplit(":", 1)
    positions = fetch_positions(client)

    def find(ex, sym, side):
        hits = [p for p in positions if p["exchange"] == ex and _asset_key(p["symbol"]) == _asset_key(sym)
                and p["side"] == side]
        if not hits:
            raise ValueError(f"{side} position {sym} on {ex} not found")
        return hits[0]

    lp, sp = find(long_ex, long_sym, "long"), find(short_ex, short_sym, "short")
    # Closing: smaller-notional leg first (its book is presumably thinner).
    legs = sorted([(long_ex, long_sym, "sell", lp), (short_ex, short_sym, "buy", sp)], key=lambda x: x[3]["notional"] or 0)
    orders = [{"exchange": ex, "asset": sym, "quote": p.get("quote"), "side": side, "size": f"{p['size']:g}",
               "reduceOnly": True, "ref_price": p["mark_price"]} for ex, sym, side, p in legs]
    for o in orders:  # informational: a close is never blocked by its impact
        _estimate_impact(client, o)
    payload = {"pair": f"{long_sym}:{long_ex}>{short_sym}:{short_ex}", "orders": orders, "reason": reason,
               "upnl": (lp["upnl"] or 0) + (sp["upnl"] or 0),
               "funding": (lp["funding_fee"] or 0) + (sp["funding_fee"] or 0)}
    lines = [
        f"🔴 <b>CLOSE {esc(long_sym)}</b> · long {_venue(long_ex)} / short {_venue(short_ex)}",
        "",
        "<b>Orders</b> (market, reduce-only)",
    ] + [f"{i}. {_venue(o['exchange'])}: {o['side'].upper()} {o['size']} {esc(o['asset'])}"
         for i, o in enumerate(orders, 1)] + [
        "Then I cancel the TP/SL left on these legs.",
        "",
        f"💰 uPnL {payload['upnl']:+.2f}$ · funding collected {payload['funding']:+.2f}$",
    ] + _impact_lines(payload, exit=True)
    if reason:
        lines += ["", "<b>Why</b>", esc(reason)]
    return _insert(conn, "close", payload, "\n".join(lines), ttl_min)


def _impact_lines(pay: dict, exit: bool = False) -> list[str]:
    """HTML lines with the price impact of each order at its size (and the basis at size on openings).
    Empty for proposals created before the impact was stored."""
    if not any("impact_bps" in o for o in pay["orders"]):
        return []
    label = "Exit price impact" if exit else "Price impact"
    parts = [f"{_venue(o['exchange'])} {o['impact_bps']:.0f} bps" if o.get("impact_bps") is not None
             else f"{_venue(o['exchange'])} n/a" for o in pay["orders"]]
    line = f"💧 {label}: " + " · ".join(parts)
    if not exit and pay.get("ref_basis_at_size") is not None:
        line += f" · basis at size {pay['ref_basis_at_size'] * 1e4:+.1f} bps"
    lines = [line]
    limit = pay.get("max_impact_bps")
    over = [o for o in pay["orders"] if limit is not None and not exit
            and o.get("impact_bps") is not None and o["impact_bps"] > limit]
    if over:
        lines.append(f"⚠️ Above the limit of {limit:.0f} bps on " + ", ".join(_venue(o["exchange"]) for o in over)
                     + ": the execution check will block it. Re-propose at the next cycle.")
    return lines


def _desk_url(order_long: dict, order_short: dict) -> str | None:
    """Link to the pair's page in the VOOI Arbitrage Desk (observed schema:
    /arbitrage-desk/<asset>-<long_ex>-<quote>-<short_ex>-<quote>). Not for HIP-3 markets."""
    if ":" in order_long["asset"] or ":" in order_short["asset"] or not (order_long.get("quote") and order_short.get("quote")):
        return None
    return (f"https://ultra.vooi.io/arbitrage-desk/{order_long['asset'].lower()}-{order_long['exchange']}-"
            f"{order_long['quote'].lower()}-{order_short['exchange']}-{order_short['quote'].lower()}")


def manual_instructions(p: dict) -> str:
    """HTML text for notify mode: what to do by hand on VOOI."""
    pay = p["payload"]
    if p["kind"] == "open":
        lo = next(o for o in pay["orders"] if o["side"] == "buy")
        so = next(o for o in pay["orders"] if o["side"] == "sell")
        url = _desk_url(lo, so)
        desk = f'<a href="{esc(url)}">Arbitrage Desk → {esc(lo["asset"])}</a>' if url else f"Arbitrage Desk → {esc(lo['asset'])}"
        lines = [
            f"🟢 <b>OPEN {esc(lo['asset'])}</b> · long {_venue(lo['exchange'])} / short {_venue(so['exchange'])}",
            f"<i>ref #{p['id']} · valid until {local_hm(p['expires_ts'])}</i>",
            "",
            "<b>1. Open the pair</b>",
            desk,
            f"• Leverage <b>{pay['leverage']:g}x</b> on both legs (cross)",
            "• Taker–Taker",
            f"• Size <code>{lo['size']}</code> {esc(lo['asset'])} per leg (~${pay['notional_per_leg']:,.0f})",
        ]
        if pay.get("protection"):
            lines += ["", "<b>2. Set TP/SL</b>"] + _tp_sl_lines(pay)
        lines += [
            "",
            f"<b>{3 if pay.get('protection') else 2}. Check</b>",
            "• same size on both legs",
        ] + (["• each leg's SL sits between the current price and the liquidation price shown by VOOI"]
             if pay.get("protection") else []) + [
            "",
            f"📊 Funding spread now: <b>{pay['ref_spread_h'] * 8760:+.0%} APR</b> · basis {pay['ref_basis'] * 1e4:+.1f} bps",
        ] + _impact_lines(pay) + [
            "⚠️ Do not open if the spread turned negative or the price moved more than 1%: wait for the next cycle.",
        ]
    else:
        long_sym = pay["pair"].split(":", 1)[0]
        lines = [
            f"🔴 <b>CLOSE {esc(long_sym)}</b> · {esc(pay['pair'])}",
            f"<i>ref #{p['id']}</i>",
            "",
            "<b>1. Close both legs</b> (Arbitrage Positions, market)",
        ] + [f"• {_venue(o['exchange'])}: {o['side'].upper()} {o['size']} {esc(o['asset'])} (reduce-only)"
             for o in pay["orders"]] + [
            "",
            "<b>2. Cancel the leftover TP/SL</b> on these legs (Open orders): otherwise they stay active "
            "and can hit a future position on the same asset.",
        ]
        lines += _impact_lines(pay, exit=True)
        if "upnl" in pay:
            lines += ["", f"💰 uPnL {pay['upnl']:+.2f}$ · funding collected {pay['funding']:+.2f}$"]
    if pay.get("reason"):
        lines += ["", "<b>Why</b>", esc(pay["reason"])]
    return "\n".join(lines)
