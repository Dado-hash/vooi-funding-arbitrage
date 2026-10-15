"""Proposte, approvazioni Telegram ed esecuzione con un exchange simulato."""

import sqlite3

import pytest

from arb import approvals, store, telegram
from arb.client import MCPError
from arb.config import load_config
from arb.executor import execute

MARKETS = {
    ("hyperliquid", "MET"): {"exchange": "hyperliquid", "baseSymbol": "MET", "quoteSymbol": "USDC", "price": "0.3745",
                             "fundingRate": "-0.00002", "baseDecimals": 0, "maxLeverage": 3, "volume24h": 1_300_000},
    ("lighter", "MET"): {"exchange": "lighter", "baseSymbol": "MET", "quoteSymbol": "USDC", "price": "0.3748",
                         "fundingRate": "0.00002", "baseDecimals": 1, "maxLeverage": 5, "volume24h": 200_000},
}


class FakeExchange:
    def __init__(self, fail_on: str | None = None):
        self.fail_on = fail_on
        self.positions: dict[str, float] = {}   # exchange -> size con segno (MET)
        self.orders: list[dict] = []
        self.leverage_set: list[tuple] = []

    def call(self, tool, args=None):
        if tool == "get_markets":
            return {"items": [MARKETS[(args["exchanges"][0], args["assets"][0])]]}
        if tool == "get_market_settings":
            return {"leverage": 3, "marginMode": "cross"}
        if tool == "set_leverage":
            self.leverage_set.append((args["exchange"], args["leverage"]))
            return {"ok": True}
        if tool == "get_positions":
            return [{"exchange": ex, "baseSymbol": "MET", "size": str(s), "markPrice": "0.3745"}
                    for ex, s in self.positions.items() if s]
        if tool == "create_order":
            if args["exchange"] == self.fail_on and not args["reduceOnly"]:
                raise MCPError("create_order: rejected by venue")
            self.orders.append(args)
            delta = float(args["size"]) * (1 if args["side"] == "buy" else -1)
            self.positions[args["exchange"]] = self.positions.get(args["exchange"], 0) + delta
            return {"orderId": str(len(self.orders))}
        raise AssertionError(tool)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    approvals.init(c)
    return c


def _approved(conn, fx):
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "test", ttl_min=15)
    approvals.set_status(conn, p["id"], "approved")
    return p


def test_open_executes_both_legs_thin_book_first(conn):
    fx = FakeExchange()
    p = _approved(conn, fx)
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "executed", r
    assert [o["exchange"] for o in fx.orders] == ["lighter", "hyperliquid"]   # lighter: volume più basso
    assert fx.positions == {"lighter": -1201.0, "hyperliquid": 1201.0}
    assert set(fx.leverage_set) == {("hyperliquid", 2), ("lighter", 2)}
    assert store.journal_list(conn, open_only=True)[0]["long_ex"] == "hyperliquid"


def test_second_leg_failure_unwinds_first(conn):
    fx = FakeExchange(fail_on="hyperliquid")
    p = _approved(conn, fx)
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "failed"
    assert fx.positions["lighter"] == 0                       # gamba chiusa: niente esposizione scoperta
    assert fx.orders[-1] == {"exchange": "lighter", "asset": "MET", "side": "buy", "size": "1201", "reduceOnly": True}


def test_not_approved_or_drifted_is_not_executed(conn):
    fx = FakeExchange()
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "", ttl_min=15)
    assert execute(fx, load_config(), conn, p["id"])["status"] == "stale"      # ancora pending
    assert not fx.orders

    p = _approved(conn, fx)
    MARKETS[("lighter", "MET")]["price"] = "0.40"   # +6.7%: oltre max_price_drift
    try:
        r = execute(fx, load_config(), conn, p["id"])
    finally:
        MARKETS[("lighter", "MET")]["price"] = "0.3748"
    assert r["status"] == "stale" and any("mosso" in x for x in r["problems"])
    assert not fx.orders


def test_limits_from_config_are_enforced(conn):
    fx = FakeExchange()
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 5000, 2, "", ttl_min=15)
    approvals.set_status(conn, p["id"], "approved")
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "stale" and any("oltre il limite" in x for x in r["problems"])


def test_close_uses_reduce_only(conn):
    fx = FakeExchange()
    fx.positions = {"hyperliquid": 1201.0, "lighter": -1201.0}
    p = approvals.propose_close(fx, conn, "MET:hyperliquid", "MET:lighter", "spread negativo", ttl_min=15)
    approvals.set_status(conn, p["id"], "approved")
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "executed", r
    assert all(o["reduceOnly"] for o in fx.orders)
    assert fx.positions == {"hyperliquid": 0, "lighter": 0}


def test_telegram_poll_only_accepts_allowed_user(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    p = approvals.propose_open(FakeExchange(), conn, "MET:hyperliquid", "MET:lighter", 450, 2, "", ttl_min=15)
    sent = []
    updates = [
        {"update_id": 1, "callback_query": {"id": "x", "from": {"id": 999}, "data": f"a:{p['id']}"}},
        {"update_id": 2, "message": {"text": f"approva {p['id']}", "chat": {"id": 111}}},
        {"update_id": 3, "callback_query": {"id": "y", "from": {"id": 111}, "data": f"a:{p['id']}"}},
    ]

    def fake_call(method, **params):
        sent.append((method, params))
        return updates if method == "getUpdates" else {"ok": True}

    monkeypatch.setattr(telegram, "_call", fake_call)
    decisions = telegram.poll(conn)
    assert decisions == [{"id": p["id"], "decision": "approved"}]
    assert ("answerCallbackQuery", {"callback_query_id": "x", "text": "Non autorizzato"}) in sent
    assert conn.execute("SELECT value FROM kv WHERE key='tg_offset'").fetchone()["value"] == "4"
