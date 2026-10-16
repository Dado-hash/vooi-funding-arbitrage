"""Execution of an APPROVED proposal via the VOOI MCP.

Checks before every order:
- proposal in status `approved` and not expired;
- `[execution]` limits in config.toml (venue, notional, leverage), independent of the proposal;
- price drift from the reference and, for openings, funding spread still positive
  and basis not worsened beyond the threshold. If something changed too much: status `stale`, to be re-proposed.

Opening: leverage on both legs, then one leg at a time (thinnest book first),
position check after each order. If the second leg fails, the first is closed.
The opening orders carry the proposal's TP/SL (bracket); after opening, the code checks that they are
active and that the stops trigger before the real liquidation. After every close (including safety closes)
the reduce-only orders left on the closed legs are cancelled.
"""

import time

from . import approvals
from .client import MCPError, VooiClient
from .config import trading_mode
from .monitor import _asset_key, fetch_positions
from .plan import _market
from .protection import check_against_liq
from .store import journal_open, now_iso


class ExecutionError(RuntimeError):
    pass


def _limits(cfg: dict) -> dict:
    e = cfg.get("execution", {})
    return {
        "venues": set(e.get("venues", [])),
        "max_notional_per_leg": float(e.get("max_notional_per_leg", 0)),
        "max_leverage": float(e.get("max_leverage", 0)),
        "max_price_drift": float(e.get("max_price_drift", 0.01)),
        "max_basis_worsening": float(e.get("max_basis_worsening", 0.002)),
    }


def _position_size(client: VooiClient, exchange: str, asset: str, side: str) -> float:
    for p in fetch_positions(client):
        if p["exchange"] == exchange and _asset_key(p["symbol"]) == _asset_key(asset) and p["side"] == side:
            return p["size"] or 0.0
    return 0.0


def _wait_size(client: VooiClient, exchange: str, asset: str, side: str, target: float, tol: float,
               tries: int = 6) -> float:
    size = 0.0
    for _ in range(tries):
        size = _position_size(client, exchange, asset, side)
        if abs(size - target) <= tol:
            return size
        time.sleep(1.5)
    return size


def check(client: VooiClient, cfg: dict, p: dict) -> list[str]:
    """Pre-execution checks. Empty list = ok."""
    lim, pay, problems = _limits(cfg), p["payload"], []
    if p["status"] != "approved":
        problems.append(f"status {p['status']}, not approved")
    if approvals.is_expired(p):
        problems.append("proposal expired")
    for o in pay["orders"]:
        if o["exchange"] not in lim["venues"]:
            problems.append(f"venue {o['exchange']} not enabled in [execution].venues")
        m = _market(client, o["exchange"], o["asset"])
        price = float(m["price"])
        if o["ref_price"]:
            drift = abs(price - o["ref_price"]) / o["ref_price"]
            if drift > lim["max_price_drift"]:
                problems.append(f"price of {o['asset']} on {o['exchange']} moved {drift:.2%} from the reference")
        notional = float(o["size"]) * price
        if not o["reduceOnly"] and notional > lim["max_notional_per_leg"] * 1.02:
            problems.append(f"notional ${notional:,.0f} over the limit ${lim['max_notional_per_leg']:,.0f}")
        o["_market"] = m
    if p["kind"] == "open":
        from .capital import fetch_balances, plan_capital

        needs: dict[str, float] = {}
        for o in pay["orders"]:
            needs[o["exchange"]] = needs.get(o["exchange"], 0) + float(o["size"]) * float(o["_market"]["price"]) / pay["leverage"]
        cap = plan_capital(fetch_balances(client), needs, buffer=0.02)
        if not cap["ok"]:
            problems += [l for l in cap["lines"][1:]]
        if pay["leverage"] > lim["max_leverage"]:
            problems.append(f"leverage {pay['leverage']}x over the limit {lim['max_leverage']}x")
        by_side = {o["side"]: o["_market"] for o in pay["orders"]}
        lm, sm = by_side["buy"], by_side["sell"]
        spread = float(sm["fundingRate"]) - float(lm["fundingRate"])
        if spread <= 0:
            problems.append(f"funding spread now {spread * 8760:+.0%} APR: not worth entering")
        lp, sp = float(lm["price"]), float(sm["price"])
        basis = (sp - lp) / ((sp + lp) / 2)
        if basis < pay["ref_basis"] - lim["max_basis_worsening"]:
            problems.append(f"basis worsened: {basis * 1e4:+.1f} bps against {pay['ref_basis'] * 1e4:+.1f}")
    return problems


def _ensure_leverage(client: VooiClient, exchange: str, asset: str, leverage: float, log: list) -> None:
    current = client.call("get_market_settings", {"asset": asset, "exchange": exchange})
    if float(current.get("leverage") or 0) != leverage:
        client.call("set_leverage", {"asset": asset, "exchange": exchange, "leverage": leverage})
        log.append(f"leverage {exchange} {asset}: {current.get('leverage')}x → {leverage:g}x")


def _order(client: VooiClient, o: dict, log: list) -> dict:
    args = {"exchange": o["exchange"], "asset": o["asset"], "side": o["side"], "size": o["size"],
            "reduceOnly": o["reduceOnly"]}
    args |= {k: o[k] for k in ("takeProfit", "stopLoss") if o.get(k) and not o["reduceOnly"]}
    res = client.call("create_order", args)
    tpsl = f" (SL {o['stopLoss']['triggerPrice']}, TP {o['takeProfit']['triggerPrice']})" if "stopLoss" in args else ""
    log.append(f"order {o['exchange']} {o['side']} {o['size']} {o['asset']}"
               f"{' reduce-only' if o['reduceOnly'] else ''}{tpsl}: ok")
    return res


def _open_orders(client: VooiClient, exchanges: list[str]) -> list[dict]:
    res = client.call("get_open_orders", {"exchanges": sorted(set(exchanges))})
    return res if isinstance(res, list) else (res.get("items") or [])


def _cancel_leftovers(client: VooiClient, legs: list[dict], log: list) -> None:
    """Cancels the reduce-only orders (TP/SL) left on legs that no longer have a position:
    they would stay active and could hit a future position on the same asset."""
    try:
        still_open = {(p["exchange"], _asset_key(p["symbol"])) for p in fetch_positions(client) if p["size"]}
        targets = {(o["exchange"], _asset_key(o["asset"])) for o in legs} - still_open
        for oo in _open_orders(client, [o["exchange"] for o in legs]):
            key = (oo.get("exchange"), _asset_key(str(oo.get("baseSymbol") or "")))
            if key in targets and oo.get("reduceOnly"):
                client.call("cancel_order", {"exchange": key[0], "asset": oo["baseSymbol"], "orderId": str(oo["orderId"])})
                log.append(f"cancelled {oo.get('type') or 'order'} {key[0]} {oo['baseSymbol']} #{oo['orderId']}")
    except MCPError as e:
        log.append(f"WARNING: TP/SL cleanup failed ({e}): cancel the leftover orders by hand")


def _check_protection(client: VooiClient, pay: dict, log: list) -> None:
    """After opening: TP/SL present on both legs and stops before the real liquidation."""
    levels = pay.get("protection")
    if not levels:
        return
    try:
        legs = {o["side"]: o for o in pay["orders"]}
        pos = {p["side"]: p for p in fetch_positions(client)
               for o in pay["orders"] if p["exchange"] == o["exchange"] and _asset_key(p["symbol"]) == _asset_key(o["asset"])}
        oos = _open_orders(client, [o["exchange"] for o in pay["orders"]])
        for side in ("buy", "sell"):
            o = legs[side]
            n = sum(1 for oo in oos if oo.get("exchange") == o["exchange"] and oo.get("reduceOnly")
                    and _asset_key(str(oo.get("baseSymbol") or "")) == _asset_key(o["asset"]))
            if n < 2:
                log.append(f"WARNING: on {o['exchange']} found {n} TP/SL instead of 2: set them manually")
        problems = check_against_liq(levels, (pos.get("long") or {}).get("liq_price"),
                                     (pos.get("short") or {}).get("liq_price"))
        log += [f"WARNING: {p}: fix manually" for p in problems]
        if not problems:
            log.append(f"TP/SL ok: close at {levels['down']} / {levels['up']}, before liquidation")
    except MCPError as e:
        log.append(f"WARNING: TP/SL check failed ({e}): verify by hand on VOOI")


def execute(client: VooiClient, cfg: dict, conn, pid: str) -> dict:
    if trading_mode() != "execute":
        return {"id": pid, "status": "refused", "problems": ["TRADING_MODE is not execute: the agent never places orders"]}
    p = approvals.get(conn, pid)
    if not p:
        raise ExecutionError(f"proposal {pid} does not exist")
    problems = check(client, cfg, p)
    if problems:
        approvals.set_status(conn, pid, "stale" if p["status"] == "approved" else p["status"],
                             result={"problems": problems})
        return {"id": pid, "status": "stale", "problems": problems}

    pay, log, done = p["payload"], [], []
    orders = pay["orders"]
    pos_side = lambda o: ("long" if o["side"] == "buy" else "short") if not o["reduceOnly"] else \
        ("long" if o["side"] == "sell" else "short")
    try:
        if p["kind"] == "open":
            for o in orders:
                _ensure_leverage(client, o["exchange"], o["asset"], pay["leverage"], log)
        for o in orders:
            side = pos_side(o)
            before = _position_size(client, o["exchange"], o["asset"], side)
            _order(client, o, log)
            target = before + float(o["size"]) if not o["reduceOnly"] else max(before - float(o["size"]), 0)
            after = _wait_size(client, o["exchange"], o["asset"], side, target, tol=float(o["size"]) * 0.01)
            if abs(after - target) > float(o["size"]) * 0.01:
                raise ExecutionError(f"{o['exchange']} {o['asset']}: position {after:g}, expected {target:g}")
            done.append(o)
    except (MCPError, ExecutionError) as e:
        log.append(f"ERROR: {e}")
        if p["kind"] == "open" and done:
            # Never leave a leg uncovered: close the ones already open.
            for o in done:
                undo = {**o, "side": "sell" if o["side"] == "buy" else "buy", "reduceOnly": True}
                try:
                    _order(client, undo, log)
                except MCPError as e2:
                    log.append(f"SAFETY CLOSE ERROR {o['exchange']} {o['asset']}: {e2} — ACT MANUALLY")
            _cancel_leftovers(client, done, log)
        result = {"id": pid, "status": "failed", "log": log}
        approvals.set_status(conn, pid, "failed", executed_ts=now_iso(), result=result)
        return result

    if p["kind"] == "open":
        _check_protection(client, pay, log)
    else:
        _cancel_leftovers(client, orders, log)
    if p["kind"] == "open":
        long_o = next(o for o in orders if o["side"] == "buy")
        short_o = next(o for o in orders if o["side"] == "sell")
        journal_open(conn, asset=long_o["asset"], long_ex=long_o["exchange"], long_sym=long_o["asset"],
                     short_ex=short_o["exchange"], short_sym=short_o["asset"],
                     notional_usd=pay["notional_per_leg"], leverage=pay["leverage"],
                     entry_spread_h=pay.get("ref_spread_h"), thesis=pay.get("reason", ""))
    result = {"id": pid, "status": "executed", "log": log}
    approvals.set_status(conn, pid, "executed", executed_ts=now_iso(), result=result)
    return result


def render_result(r: dict) -> str:
    head = {"executed": "EXECUTED", "failed": "FAILED", "stale": "NOT EXECUTED (conditions changed)",
            "refused": "NOT EXECUTED (notify mode)"}.get(
        r["status"], r["status"])
    lines = [f"PROPOSAL #{r['id']}: {head}"]
    lines += [f"- {x}" for x in r.get("problems", []) + r.get("log", [])]
    return "\n".join(lines)
