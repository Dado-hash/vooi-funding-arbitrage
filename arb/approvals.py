"""Proposte di operazione con approvazione via Telegram.

Una proposta salva i parametri ESATTI degli ordini (TP/SL compresi). L'approvazione vale solo per quella proposta,
entro la scadenza; `arb execute` esegue solo proposte approvate e ricontrolla i limiti.
"""

import json
import secrets
from html import escape as esc
from datetime import datetime, timedelta, timezone

from .client import VooiClient
from .monitor import _asset_key, fetch_positions
from .plan import build_plan, _market
from .protection import bracket, tp_sl_levels
from .store import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    created_ts TEXT NOT NULL, expires_ts TEXT NOT NULL,
    kind TEXT NOT NULL,              -- open | close
    payload TEXT NOT NULL,           -- ordini esatti + contesto (JSON)
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
    """Ora locale HH:MM di un timestamp ISO UTC."""
    return _parse(ts).astimezone().strftime("%H:%M")


def _venue(ex: str) -> str:
    return {"hyperliquid": "Hyperliquid", "lighter": "Lighter", "aster": "Aster"}.get(ex, ex.capitalize())


def _tp_sl_lines(pay: dict) -> list[str]:
    """Righe HTML con i TP/SL per gamba e il perché."""
    lv = pay.get("protection")
    if not lv:
        return []
    lo = next(o for o in pay["orders"] if o["side"] == "buy")
    so = next(o for o in pay["orders"] if o["side"] == "sell")
    return [
        f"• Long {_venue(lo['exchange'])}: SL <code>{lv['down']}</code> · TP <code>{lv['up']}</code>",
        f"• Short {_venue(so['exchange'])}: SL <code>{lv['up']}</code> · TP <code>{lv['down']}</code>",
        f"<i>Stessi due prezzi, invertiti: a {lv['down_move']:+.0%} o {lv['up_move']:+.0%} si chiudono entrambe le gambe "
        f"insieme, prima della liquidazione (stimata a {lv['liq_long_move']:+.0%} / {lv['liq_short_move']:+.0%}).</i>",
    ]


def propose_open(client: VooiClient, conn, long_spec: str, short_spec: str, notional: float, leverage: float,
                 reason: str, ttl_min: float, tp_sl_fraction: float | None = None) -> dict:
    """`tp_sl_fraction`: se impostato, gli ordini d'apertura portano TP/SL a quella frazione della distanza
    di liquidazione (vedi `protection`)."""
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
    payload = {
        "pair": f"{long_sym}:{long_ex}>{short_sym}:{short_ex}",
        "leverage": leverage, "notional_per_leg": notional, "orders": orders,
        "ref_basis": plan["basis_entry"],
        "ref_spread_h": float(sm["fundingRate"]) - float(lm["fundingRate"]),
        "protection": levels,
        "reason": reason,
    }
    lines = [
        f"🟢 <b>APRI {esc(long_sym)}</b> · long {_venue(long_ex)} / short {_venue(short_ex)}",
        f"Leva <b>{leverage:g}x</b> · <b>${notional:,.0f}</b> per gamba · size <code>{size}</code>",
        "",
        "<b>Ordini</b> (market, uno alla volta)",
    ] + [f"{i}. {_venue(o['exchange'])}: {o['side'].upper()} {o['size']} {esc(o['asset'])}"
         for i, o in enumerate(orders, 1)]
    if levels:
        lines += ["", "<b>TP/SL inclusi negli ordini</b>"] + _tp_sl_lines(payload)
    lines += ["", f"📊 Spread funding ora: <b>{payload['ref_spread_h'] * 8760:+.0%} APR</b> · "
                  f"basis {plan['basis_entry'] * 1e4:+.1f} bps"]
    if reason:
        lines += ["", "<b>Perché</b>", esc(reason)]
    return _insert(conn, "open", payload, "\n".join(lines), ttl_min)


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
    payload = {"pair": f"{long_sym}:{long_ex}>{short_sym}:{short_ex}", "orders": orders, "reason": reason,
               "upnl": (lp["upnl"] or 0) + (sp["upnl"] or 0),
               "funding": (lp["funding_fee"] or 0) + (sp["funding_fee"] or 0)}
    lines = [
        f"🔴 <b>CHIUDI {esc(long_sym)}</b> · long {_venue(long_ex)} / short {_venue(short_ex)}",
        "",
        "<b>Ordini</b> (market, reduce-only)",
    ] + [f"{i}. {_venue(o['exchange'])}: {o['side'].upper()} {o['size']} {esc(o['asset'])}"
         for i, o in enumerate(orders, 1)] + [
        "Poi cancello i TP/SL rimasti su queste gambe.",
        "",
        f"💰 uPnL {payload['upnl']:+.2f}$ · funding incassato {payload['funding']:+.2f}$",
    ]
    if reason:
        lines += ["", "<b>Perché</b>", esc(reason)]
    return _insert(conn, "close", payload, "\n".join(lines), ttl_min)


def _desk_url(order_long: dict, order_short: dict) -> str | None:
    """Link alla pagina della coppia nell'Arbitrage Desk VOOI (schema osservato:
    /arbitrage-desk/<asset>-<long_ex>-<quote>-<short_ex>-<quote>). Non per i mercati HIP-3."""
    if ":" in order_long["asset"] or ":" in order_short["asset"] or not (order_long.get("quote") and order_short.get("quote")):
        return None
    return (f"https://ultra.vooi.io/arbitrage-desk/{order_long['asset'].lower()}-{order_long['exchange']}-"
            f"{order_long['quote'].lower()}-{order_short['exchange']}-{order_short['quote'].lower()}")


def manual_instructions(p: dict) -> str:
    """Testo HTML per la modalità notify: cosa fare a mano su VOOI."""
    pay = p["payload"]
    if p["kind"] == "open":
        lo = next(o for o in pay["orders"] if o["side"] == "buy")
        so = next(o for o in pay["orders"] if o["side"] == "sell")
        url = _desk_url(lo, so)
        desk = f'<a href="{esc(url)}">Arbitrage Desk → {esc(lo["asset"])}</a>' if url else f"Arbitrage Desk → {esc(lo['asset'])}"
        lines = [
            f"🟢 <b>APRI {esc(lo['asset'])}</b> · long {_venue(lo['exchange'])} / short {_venue(so['exchange'])}",
            f"<i>rif. #{p['id']} · valido fino alle {local_hm(p['expires_ts'])}</i>",
            "",
            "<b>1. Apri la coppia</b>",
            desk,
            f"• Leva <b>{pay['leverage']:g}x</b> su entrambe le gambe (cross)",
            "• Tipo Taker–Taker",
            f"• Size <code>{lo['size']}</code> {esc(lo['asset'])} per gamba (~${pay['notional_per_leg']:,.0f})",
        ]
        if pay.get("protection"):
            lines += ["", "<b>2. Imposta TP/SL</b>"] + _tp_sl_lines(pay)
        lines += [
            "",
            f"<b>{3 if pay.get('protection') else 2}. Controlla</b>",
            "• stessa size sulle due gambe",
        ] + (["• lo SL di ogni gamba sta tra il prezzo attuale e la liquidazione mostrata da VOOI"]
             if pay.get("protection") else []) + [
            "",
            f"📊 Spread funding ora: <b>{pay['ref_spread_h'] * 8760:+.0%} APR</b> · basis {pay['ref_basis'] * 1e4:+.1f} bps",
            "⚠️ Non aprire se lo spread è diventato negativo o il prezzo si è mosso più dell'1%: aspetta il prossimo ciclo.",
        ]
    else:
        long_sym = pay["pair"].split(":", 1)[0]
        lines = [
            f"🔴 <b>CHIUDI {esc(long_sym)}</b> · {esc(pay['pair'])}",
            f"<i>rif. #{p['id']}</i>",
            "",
            "<b>1. Chiudi entrambe le gambe</b> (Arbitrage Positions, market)",
        ] + [f"• {_venue(o['exchange'])}: {o['side'].upper()} {o['size']} {esc(o['asset'])} (reduce-only)"
             for o in pay["orders"]] + [
            "",
            "<b>2. Cancella i TP/SL rimasti</b> su queste gambe (Open orders): altrimenti restano attivi "
            "e possono colpire una posizione futura sullo stesso asset.",
        ]
        if "upnl" in pay:
            lines += ["", f"💰 uPnL {pay['upnl']:+.2f}$ · funding incassato {pay['funding']:+.2f}$"]
    if pay.get("reason"):
        lines += ["", "<b>Perché</b>", esc(pay["reason"])]
    return "\n".join(lines)
