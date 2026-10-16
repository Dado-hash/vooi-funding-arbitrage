"""Capital check before proposing a trade: available margin per venue and the transfers needed.
Transfers are only SUGGESTED (the user makes them on VOOI): never executed.

`normalize_account` tries several field names and keeps the raw record (format verified on
Hyperliquid and Lighter, see tests/fixtures/accounts.json).
"""

import math

from .client import VooiClient

AVAILABLE_KEYS = ("availableBalance", "availableMargin", "available", "withdrawable", "freeMargin",
                  "free", "availableToTrade")
EQUITY_KEYS = ("equity", "totalEquity", "accountValue", "totalBalance", "balance", "total")
USED_KEYS = ("marginInUse", "marginUsed", "usedMargin", "totalMarginUsed", "initialMargin")
# Collateral counted 1:1 in USD. Other tokens (e.g. spot assets) are ignored.
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
    """Balances aggregated per venue: {exchange: {"equity", "available", "used", "accounts"}}."""
    res = client.call("get_accounts", {})
    items = res if isinstance(res, list) else (res.get("items") or res.get("accounts") or [])
    accounts = [normalize_account(a) for a in items]
    # Hyperliquid: spot balance counts as perp margin only with a unified abstraction mode.
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
    """`needs`: required margin per venue (notional/leverage). Applies `buffer` (fees + swings)
    and works out transfers between venues. Returns {"ok", "venues", "moves", "missing", "lines"}."""
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
    lines = [f"CAPITAL (required margin + {buffer:.0%} buffer)"]
    for ex, v in venues.items():
        state = "not connected" if not v["connected"] else ("ok" if v["delta"] >= 0 else f"missing ${-v['delta']:,.2f}")
        lines.append(f"- {ex}: available ${v['available']:,.2f} · needed ${v['required']:,.2f}: {state}")
    for m in moves:
        lines.append(f"- Move ${m['amount']:,.0f} USDC from {m['from']} to {m['to']} (VOOI > Transfer), then open.")
    if missing > 0:
        lines.append(f"- Total capital insufficient: missing ${missing:,.2f}. Deposit or reduce the notional.")
    return {"ok": ok, "venues": venues, "moves": moves, "missing": missing, "lines": lines}


def check_capital(client: VooiClient, cfg: dict, exchanges: list[str], notional: float, leverage: float) -> dict:
    buffer = float(cfg.get("strategy", {}).get("capital_buffer", 0.1))
    needs: dict[str, float] = {}
    for ex in exchanges:
        needs[ex] = needs.get(ex, 0) + notional / leverage
    return plan_capital(fetch_balances(client), needs, buffer)
