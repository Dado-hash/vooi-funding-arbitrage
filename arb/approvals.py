"""Proposte di operazione con approvazione via Telegram.

Una proposta salva i parametri ESATTI degli ordini. L'approvazione vale solo per quella proposta,
entro la scadenza; `arb execute` esegue solo proposte approvate e ricontrolla i limiti.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

from .client import VooiClient
from .monitor import _asset_key, fetch_positions
from .plan import build_plan, _market
from .store import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    created_ts TEXT NOT NULL, expires_ts TEXT NOT NULL,
    kind TEXT NOT NULL,              -- open | close
    payload TEXT NOT NULL,           -- ordini esatti + contesto (JSON)
    summary TEXT NOT NULL,
    status TEXT NOT NULL,            -- pending | approved | rejected | expired | stale | executed | failed
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


def propose_open(client: VooiClient, conn, long_spec: str, short_spec: str, notional: float, leverage: float,
                 reason: str, ttl_min: float) -> dict:
    long_sym, long_ex = long_spec.rsplit(":", 1)
    short_sym, short_ex = short_spec.rsplit(":", 1)
    lm, sm = _market(client, long_ex, long_sym), _market(client, short_ex, short_sym)
    plan = build_plan(lm, sm, notional, leverage)
    if plan["problems"]:
        raise ValueError("; ".join(plan["problems"]))
    size = _fmt_size(plan["size"], plan["decimals"])
    orders = [{"exchange": o["exchange"], "asset": o["symbol"], "quote": o["quote"], "side": o["side"].lower(),
               "size": size, "reduceOnly": False, "ref_price": o["price"]} for o in plan["order"]]
    payload = {
        "pair": f"{long_sym}:{long_ex}>{short_sym}:{short_ex}",
        "leverage": leverage, "notional_per_leg": notional, "orders": orders,
        "ref_basis": plan["basis_entry"],
        "ref_spread_h": float(sm["fundingRate"]) - float(lm["fundingRate"]),
        "reason": reason,
    }
    summary = (
        f"APRI {payload['pair']}\n"
        f"Leva {leverage:g}x · ${notional:,.0f} per gamba · size {size}\n"
        + "\n".join(f"{i}. {o['exchange']}: MARKET {o['side'].upper()} {o['size']} {o['asset']}" for i, o in enumerate(orders, 1))
        + f"\nSpread funding ora: {payload['ref_spread_h'] * 8760:+.0%} APR · basis {plan['basis_entry'] * 1e4:+.1f} bps"
        + (f"\n\nPerché: {reason}" if reason else "")
    )
    return _insert(conn, "open", payload, summary, ttl_min)


def propose_close(client: VooiClient, conn, long_spec: str, short_spec: str, reason: str, ttl_min: float) -> dict:
    long_sym, long_ex = long_spec.rsplit(":", 1)
    short_sym, short_ex = short_spec.rsplit(":", 1)
    positions = fetch_positions(client)

    def find(ex, sym, side):
        hits = [p for p in positions if p["exchange"] == ex and _asset_key(p["symbol"]) == _asset_key(sym)
                and p["side"] == side]
        if not hits:
            raise ValueError(f"posizione {side} {sym} su {ex} non trovata")
        return hits[0]

    lp, sp = find(long_ex, long_sym, "long"), find(short_ex, short_sym, "short")
    # Chiusura: prima la gamba col notional minore (book presumibilmente più sottile).
    legs = sorted([(long_ex, long_sym, "sell", lp), (short_ex, short_sym, "buy", sp)], key=lambda x: x[3]["notional"] or 0)
    orders = [{"exchange": ex, "asset": sym, "quote": p.get("quote"), "side": side, "size": f"{p['size']:g}",
               "reduceOnly": True, "ref_price": p["mark_price"]} for ex, sym, side, p in legs]
    payload = {"pair": f"{long_sym}:{long_ex}>{short_sym}:{short_ex}", "orders": orders, "reason": reason}
    summary = (
        f"CHIUDI {payload['pair']}\n"
        + "\n".join(f"{i}. {o['exchange']}: MARKET {o['side'].upper()} {o['size']} {o['asset']} (reduce-only)"
                    for i, o in enumerate(orders, 1))
        + f"\nuPnL attuale: {(lp['upnl'] or 0) + (sp['upnl'] or 0):+.2f}$ · funding incassato: "
          f"{(lp['funding_fee'] or 0) + (sp['funding_fee'] or 0):+.2f}$"
        + (f"\n\nPerché: {reason}" if reason else "")
    )
    return _insert(conn, "close", payload, summary, ttl_min)
