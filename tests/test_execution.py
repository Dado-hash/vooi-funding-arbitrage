"""Proposals, Telegram approvals and execution with a simulated exchange."""

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
    def __init__(self, fail_on: str | None = None, available=None):
        self.fail_on = fail_on
        self.available = available or {"hyperliquid": 500.0, "lighter": 500.0}
        self.positions: dict[str, float] = {}   # exchange -> signed size (MET)
        self.orders: list[dict] = []
        self.triggers: list[dict] = []          # open TP/SL (children of the brackets)
        self.cancelled: list[str] = []
        self.leverage_set: list[tuple] = []
        self.liq = {"hyperliquid": "0.22", "lighter": "0.52"}
        self.slip: dict[tuple[str, str], float] = {}   # (exchange, side) -> impact in bps (default 2)

    def call(self, tool, args=None):
        if tool == "estimate_slippage":
            bps = self.slip.get((args["exchange"], args["side"]), 2.0)
            price = float(MARKETS[(args["exchange"], args["asset"])]["price"])
            sign = 1 if args["side"] == "buy" else -1   # buying lifts the average price, selling lowers it
            return {"avgPrice": price * (1 + sign * bps / 1e4), "slippageBps": bps, "filled": True,
                    "filledPercent": 100}
        if tool == "get_markets":
            return {"items": [MARKETS[(args["exchanges"][0], args["assets"][0])]]}
        if tool == "get_accounts":
            return [{"exchange": ex, "availableBalance": str(v)} for ex, v in self.available.items()]
        if tool == "get_market_settings":
            return {"leverage": 3, "marginMode": "cross"}
        if tool == "set_leverage":
            self.leverage_set.append((args["exchange"], args["leverage"]))
            return {"ok": True}
        if tool == "get_positions":
            return [{"exchange": ex, "baseSymbol": "MET", "size": str(s), "markPrice": "0.3745",
                     "liquidationPrice": self.liq[ex]} for ex, s in self.positions.items() if s]
        if tool == "get_open_orders":
            return [t for t in self.triggers if t["exchange"] in args["exchanges"]]
        if tool == "cancel_order":
            self.cancelled.append(args["orderId"])
            self.triggers = [t for t in self.triggers if t["orderId"] != args["orderId"]]
            return {"ok": True}
        if tool == "create_order":
            if args["exchange"] == self.fail_on and not args["reduceOnly"]:
                raise MCPError("create_order: rejected by venue")
            self.orders.append(args)
            for kind in ("stopLoss", "takeProfit"):
                if kind in args:
                    self.triggers.append({"exchange": args["exchange"], "baseSymbol": "MET", "reduceOnly": True,
                                          "type": kind, "orderId": f"t{len(self.orders)}{kind}",
                                          "triggerPrice": args[kind]["triggerPrice"]})
            delta = float(args["size"]) * (1 if args["side"] == "buy" else -1)
            self.positions[args["exchange"]] = self.positions.get(args["exchange"], 0) + delta
            return {"orderId": str(len(self.orders))}
        raise AssertionError(tool)


@pytest.fixture(autouse=True)
def execute_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "execute")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    approvals.init(c)
    return c


def _approved(conn, fx, tp_sl=None):
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "test", ttl_min=15,
                               tp_sl_fraction=tp_sl)
    approvals.set_status(conn, p["id"], "approved")
    return p


def test_open_executes_both_legs_thin_book_first(conn):
    fx = FakeExchange()
    p = _approved(conn, fx)
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "executed", r
    assert [o["exchange"] for o in fx.orders] == ["lighter", "hyperliquid"]   # lighter: lower volume
    assert fx.positions == {"lighter": -1201.0, "hyperliquid": 1201.0}
    assert set(fx.leverage_set) == {("hyperliquid", 2), ("lighter", 2)}
    assert store.journal_list(conn, open_only=True)[0]["long_ex"] == "hyperliquid"


def test_second_leg_failure_unwinds_first(conn):
    fx = FakeExchange(fail_on="hyperliquid")
    p = _approved(conn, fx)
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "failed"
    assert fx.positions["lighter"] == 0                       # leg closed: no uncovered exposure
    assert fx.orders[-1] == {"exchange": "lighter", "asset": "MET", "side": "buy", "size": "1201", "reduceOnly": True}


def test_not_approved_or_drifted_is_not_executed(conn):
    fx = FakeExchange()
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "", ttl_min=15)
    assert execute(fx, load_config(), conn, p["id"])["status"] == "stale"      # still pending
    assert not fx.orders

    p = _approved(conn, fx)
    MARKETS[("lighter", "MET")]["price"] = "0.40"   # +6.7%: over max_price_drift
    try:
        r = execute(fx, load_config(), conn, p["id"])
    finally:
        MARKETS[("lighter", "MET")]["price"] = "0.3748"
    assert r["status"] == "stale" and any("moved" in x for x in r["problems"])
    assert not fx.orders


def test_limits_from_config_are_enforced(conn):
    fx = FakeExchange()
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 5000, 2, "", ttl_min=15)
    approvals.set_status(conn, p["id"], "approved")
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "stale" and any("over the limit" in x for x in r["problems"])


def test_close_uses_reduce_only(conn):
    fx = FakeExchange()
    fx.positions = {"hyperliquid": 1201.0, "lighter": -1201.0}
    p = approvals.propose_close(fx, conn, "MET:hyperliquid", "MET:lighter", "negative spread", ttl_min=15)
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
        {"update_id": 2, "message": {"text": f"approve {p['id']}", "chat": {"id": 111}}},
        {"update_id": 3, "callback_query": {"id": "y", "from": {"id": 111}, "data": f"a:{p['id']}"}},
    ]

    def fake_call(method, **params):
        sent.append((method, params))
        return updates if method == "getUpdates" else {"ok": True}

    monkeypatch.setattr(telegram, "_call", fake_call)
    decisions = telegram.poll(conn)
    assert decisions == [{"id": p["id"], "decision": "approved"}]
    assert ("answerCallbackQuery", {"callback_query_id": "x", "text": "Not authorized"}) in sent
    assert conn.execute("SELECT value FROM kv WHERE key='tg_offset'").fetchone()["value"] == "4"


def test_notify_mode_never_executes(conn, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "notify")
    fx = FakeExchange()
    p = _approved(conn, fx)
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "refused" and not fx.orders
    text = approvals.manual_instructions(approvals.get(conn, p["id"]))
    assert "long Hyperliquid / short Lighter" in text and "<code>1201</code> MET" in text
    assert "arbitrage-desk/met-hyperliquid-usdc-lighter-usdc" in text


def test_execute_blocked_when_margin_missing(conn):
    fx = FakeExchange(available={"hyperliquid": 500.0, "lighter": 50.0})
    p = _approved(conn, fx)
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "stale" and any("lighter" in x and "missing" in x for x in r["problems"])
    assert not fx.orders


def test_capital_plan_suggests_transfer_between_venues():
    from arb.capital import plan_capital

    balances = {"hyperliquid": {"available": 900.0}, "lighter": {"available": 100.0}}
    r = plan_capital(balances, {"hyperliquid": 250.0, "lighter": 250.0}, buffer=0.1)
    assert not r["ok"] and r["missing"] == 0
    assert r["moves"] == [{"from": "hyperliquid", "to": "lighter", "amount": 175}]   # 275 − 100, rounded to 5

    r = plan_capital({"hyperliquid": {"available": 200.0}, "lighter": {"available": 100.0}},
                     {"hyperliquid": 250.0, "lighter": 250.0}, buffer=0.1)
    assert r["missing"] > 0 and any("insufficient" in l for l in r["lines"])

    r = plan_capital({"hyperliquid": {"available": 300.0}}, {"hyperliquid": 250.0, "lighter": 250.0}, buffer=0.1)
    assert any("not connected" in l for l in r["lines"])


def test_normalize_account_variants():
    from arb.capital import normalize_account

    assert normalize_account({"exchange": "lighter", "availableBalance": "12.5"})["available"] == 12.5
    assert normalize_account({"exchange": "hyperliquid", "accountValue": "100", "marginUsed": "30"})["available"] == 70


@pytest.mark.parametrize("mode,hl_expected", [("unifiedAccount", 58.224269), ("disabled", 0.0)])
def test_fetch_balances_real_format(mode, hl_expected):
    import json
    from pathlib import Path

    from arb.capital import fetch_balances

    accounts = json.loads((Path(__file__).parent / "fixtures" / "accounts.json").read_text())

    class C:
        def call(self, tool, args=None):
            return accounts if tool == "get_accounts" else {"abstractionMode": mode}

    b = fetch_balances(C())
    assert b["lighter"]["available"] == 58.0
    assert b.get("hyperliquid", {"available": 0.0})["available"] == pytest.approx(hl_expected)


def test_tp_sl_levels_close_both_legs_before_liquidation():
    from arb.protection import bracket, check_against_liq, tp_sl_levels

    hl, lt = MARKETS[("hyperliquid", "MET")], MARKETS[("lighter", "MET")]
    lv = tp_sl_levels(hl, lt, leverage=2, fraction=0.7)
    # estimated isolated liquidation: long HL (max 3x) −33%, short Lighter (max 5x) +40%
    assert lv["liq_long_move"] == pytest.approx(-1 / 3) and lv["liq_short_move"] == pytest.approx(0.4)
    assert lv["down"] == "0.28724" and lv["up"] == "0.47955"   # 5 significant digits, towards the price
    assert bracket("buy", lv) == {"stopLoss": {"triggerPrice": "0.28724"}, "takeProfit": {"triggerPrice": "0.47955"}}
    assert bracket("sell", lv)["stopLoss"]["triggerPrice"] == "0.47955"   # same prices, swapped
    # the case from yesterday: short stop at 0.5306 with liquidation at 0.5170
    assert check_against_liq({"down": "0.22753", "up": "0.53062"}, 0.2144, 0.5170) == [
        "short stop 0.53062 beyond liquidation 0.517"]


def test_open_with_tp_sl_sends_brackets_and_checks_them(conn):
    fx = FakeExchange()
    p = _approved(conn, fx, tp_sl=0.7)
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "executed", r
    long_o = next(o for o in fx.orders if o["side"] == "buy")
    assert long_o["stopLoss"] == {"triggerPrice": "0.28724"} and long_o["takeProfit"] == {"triggerPrice": "0.47955"}
    assert len(fx.triggers) == 4
    assert any(x.startswith("TP/SL ok") for x in r["log"]), r["log"]

    fx.liq["lighter"] = "0.45"    # real liquidation closer than the stop: must be flagged
    fx.positions, fx.triggers = {}, []
    r = execute(fx, load_config(), conn, _approved(conn, fx, tp_sl=0.7)["id"])
    assert any("short stop 0.47955 beyond liquidation" in x for x in r["log"]), r["log"]


def test_close_and_unwind_cancel_leftover_tp_sl(conn):
    fx = FakeExchange()
    execute(fx, load_config(), conn, _approved(conn, fx, tp_sl=0.7)["id"])
    p = approvals.propose_close(fx, conn, "MET:hyperliquid", "MET:lighter", "test", ttl_min=15)
    approvals.set_status(conn, p["id"], "approved")
    assert execute(fx, load_config(), conn, p["id"])["status"] == "executed"
    assert not fx.triggers and len(fx.cancelled) == 4
    assert all("stopLoss" not in o for o in fx.orders if o["reduceOnly"])   # closures do not carry TP/SL

    fx = FakeExchange(fail_on="hyperliquid")       # second leg fails: the first leg's TP/SL are cancelled
    r = execute(fx, load_config(), conn, _approved(conn, fx, tp_sl=0.7)["id"])
    assert r["status"] == "failed" and not fx.triggers and fx.positions["lighter"] == 0


def test_notify_instructions_include_tp_sl(conn, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "notify")
    p = _approved(conn, FakeExchange(), tp_sl=0.7)
    text = approvals.manual_instructions(approvals.get(conn, p["id"]))
    assert "Long Hyperliquid: SL <code>0.28724</code> · TP <code>0.47955</code>" in text
    assert "Short Lighter: SL <code>0.47955</code> · TP <code>0.28724</code>" in text
    assert "<b>3. Check</b>" in text
    assert "SL 0.28724" in telegram.plain(text)


def test_opening_blocked_when_lighter_impact_is_high(conn):
    fx = FakeExchange()
    fx.slip = {("lighter", "buy"): 40, ("lighter", "sell"): 40}
    p = _approved(conn, fx)
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "stale"
    assert any("price impact" in x and "lighter" in x for x in r["problems"]), r["problems"]
    assert not fx.orders


def test_closing_is_not_blocked_by_price_impact(conn):
    fx = FakeExchange()
    fx.positions = {"hyperliquid": 1201.0, "lighter": -1201.0}
    fx.slip = {("lighter", "buy"): 40, ("lighter", "sell"): 40}
    p = approvals.propose_close(fx, conn, "MET:hyperliquid", "MET:lighter", "test", ttl_min=15)
    approvals.set_status(conn, p["id"], "approved")
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "executed", r
    assert fx.positions == {"hyperliquid": 0, "lighter": 0}


def test_proposal_shows_price_impact_and_basis_at_size(conn):
    fx = FakeExchange()
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "", ttl_min=15,
                               max_impact_bps=25)
    assert "Price impact" in p["summary"]
    assert "Lighter 2 bps" in p["summary"] and "Hyperliquid 2 bps" in p["summary"]
    assert "basis at size" in p["summary"] and "⚠️" not in p["summary"]
    assert p["payload"]["ref_basis_at_size"] is not None

    fx.slip = {("lighter", "sell"): 40}
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "", ttl_min=15,
                               max_impact_bps=25)
    assert "Lighter 40 bps" in p["summary"] and "Above the limit of 25 bps on Lighter" in p["summary"]


def test_notify_instructions_show_price_impact(conn, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "notify")
    fx = FakeExchange()
    p = approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "", ttl_min=15,
                               max_impact_bps=25)
    assert "Price impact" in approvals.manual_instructions(p)


def test_pre_trade_check_skips_impact_for_legacy_proposals(conn):
    """Proposals stored before the impact check have no `ref_basis_at_size`: no basis check, no crash."""
    fx = FakeExchange()
    p = _approved(conn, fx)
    p["payload"].pop("ref_basis_at_size", None)
    assert execute(fx, load_config(), conn, p["id"])["status"] == "executed"


def test_no_proposal_when_entry_basis_is_negative(conn, monkeypatch):
    """Long quoted above short: buying high and selling low is never proposed."""
    fx = FakeExchange()
    monkeypatch.setitem(MARKETS[("lighter", "MET")], "price", "0.3740")    # short below long
    with pytest.raises(approvals.BasisError, match="entry basis"):
        approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "", ttl_min=15, min_basis=0.0)


def test_no_proposal_when_basis_at_size_is_negative(conn):
    """Positive basis at mid (+8 bps) but the price impact turns it negative at size."""
    fx = FakeExchange()
    fx.slip = {("lighter", "sell"): 10, ("hyperliquid", "buy"): 10}
    with pytest.raises(approvals.BasisError, match="basis at size"):
        approvals.propose_open(fx, conn, "MET:hyperliquid", "MET:lighter", 450, 2, "", ttl_min=15, min_basis=0.0)


def test_opening_blocked_when_basis_turns_negative(conn, monkeypatch):
    fx = FakeExchange()
    p = _approved(conn, fx)
    monkeypatch.setitem(MARKETS[("lighter", "MET")], "price", "0.3744")    # -2.7 bps: within worsening limit
    r = execute(fx, load_config(), conn, p["id"])
    assert r["status"] == "stale"
    assert any("entry basis" in x for x in r["problems"]), r["problems"]
    assert not fx.orders
