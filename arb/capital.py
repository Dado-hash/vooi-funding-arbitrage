"""Controllo del capitale prima di proporre un trade: margine disponibile per venue e spostamenti
necessari. Gli spostamenti vengono solo SUGGERITI (l'utente li fa su VOOI): mai eseguiti.

Il formato di `get_accounts` va confermato con exchange collegati: `normalize_account` prova più
nomi di campo e conserva il record grezzo.
"""

import math

from .client import VooiClient

AVAILABLE_KEYS = ("availableBalance", "availableMargin", "available", "withdrawable", "freeMargin",
                  "free", "availableToTrade")
EQUITY_KEYS = ("equity", "totalEquity", "accountValue", "totalBalance", "balance", "total")
USED_KEYS = ("marginInUse", "marginUsed", "usedMargin", "totalMarginUsed", "initialMargin")
# Collaterale contato 1:1 in USD. Altri token (es. asset spot) vengono ignorati.
STABLES = {"USDC", "USDT", "USDH", "USD", "USDE", "USD1", "USDT0"}


def _num(d: dict, keys: tuple) -> float | None:
    for k in keys:
        if d.get(k) not in (None, ""):
            try:
                return float(d[k])
            except (TypeError, ValueError):
                pass
    return None


def normalize_account(a: dict) -> dict:
    equity = _num(a, EQUITY_KEYS)
    used = _num(a, USED_KEYS)
    available = _num(a, AVAILABLE_KEYS)
    if available is None and equity is not None:
        available = equity - (used or 0)
    return {"exchange": a.get("exchange") or a.get("venue"), "equity": equity, "used": used,
            "available": available, "token": a.get("token"), "type": a.get("type"), "raw": a}


def fetch_balances(client: VooiClient) -> dict[str, dict]:
    """Saldi aggregati per venue: {exchange: {"equity", "available", "used", "accounts"}}."""
    res = client.call("get_accounts", {})
    items = res if isinstance(res, list) else (res.get("items") or res.get("accounts") or [])
    accounts = [normalize_account(a) for a in items]
    # Hyperliquid: il saldo spot vale come margine perp solo con un abstraction mode unificato.
    hl_spot_usable = True
    if any(a["exchange"] == "hyperliquid" and a["type"] == "spot" for a in accounts):
        mode = (client.call("get_account_info", {"exchange": "hyperliquid"}) or {}).get("abstractionMode")
        hl_spot_usable = mode in ("unifiedAccount", "portfolioMargin")
    out: dict[str, dict] = {}
    for a in accounts:
        if not a["exchange"] or (a["token"] and a["token"].upper() not in STABLES):
            continue
        if a["exchange"] == "hyperliquid" and a["type"] == "spot" and not hl_spot_usable:
            continue
        v = out.setdefault(a["exchange"], {"equity": 0.0, "available": 0.0, "used": 0.0, "accounts": 0})
        v["equity"] += a["equity"] or 0
        v["available"] += a["available"] or 0
        v["used"] += a["used"] or 0
        v["accounts"] += 1
    return out


def _ceil5(x: float) -> float:
    return math.ceil(x / 5) * 5


def plan_capital(balances: dict[str, dict], needs: dict[str, float], buffer: float) -> dict:
    """`needs`: margine richiesto per venue (notional/leva). Applica `buffer` (fee + oscillazioni)
    e calcola gli spostamenti tra venue. Restituisce {"ok", "venues", "moves", "missing", "lines"}."""
    venues = {}
    for ex, need in needs.items():
        required = need * (1 + buffer)
        available = balances.get(ex, {}).get("available", 0.0)
        venues[ex] = {"required": required, "available": available, "delta": available - required,
                      "connected": ex in balances}

    moves, missing = [], 0.0
    surplus = {ex: v["delta"] for ex, v in venues.items() if v["delta"] > 0}
    for ex, v in venues.items():
        short = -v["delta"]
        if short <= 0:
            continue
        for src in sorted(surplus, key=surplus.get, reverse=True):
            if short <= 0:
                break
            amount = min(_ceil5(short), math.floor(surplus[src]))
            if amount <= 0:
                continue
            moves.append({"from": src, "to": ex, "amount": amount})
            surplus[src] -= amount
            short -= amount
        if short > 0:
            missing += short

    ok = all(v["delta"] >= 0 for v in venues.values())
    lines = [f"CAPITALE (margine richiesto +{buffer:.0%} di buffer)"]
    for ex, v in venues.items():
        state = "non collegato" if not v["connected"] else ("ok" if v["delta"] >= 0 else f"mancano ${-v['delta']:,.2f}")
        lines.append(f"- {ex}: disponibile ${v['available']:,.2f} · servono ${v['required']:,.2f} → {state}")
    for m in moves:
        lines.append(f"→ Sposta ${m['amount']:,.0f} USDC da {m['from']} a {m['to']} (VOOI → Transfer), poi apri.")
    if missing > 0:
        lines.append(f"→ Capitale totale insufficiente: mancano ${missing:,.2f}. Deposita o riduci il notional.")
    return {"ok": ok, "venues": venues, "moves": moves, "missing": missing, "lines": lines}


def check_capital(client: VooiClient, cfg: dict, exchanges: list[str], notional: float, leverage: float) -> dict:
    buffer = float(cfg.get("strategy", {}).get("capital_buffer", 0.1))
    needs: dict[str, float] = {}
    for ex in exchanges:
        needs[ex] = needs.get(ex, 0) + notional / leverage
    return plan_capital(fetch_balances(client), needs, buffer)
