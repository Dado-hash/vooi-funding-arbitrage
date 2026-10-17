"""Offline tests on real MCP responses saved in tests/fixtures."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import sqlite3

import pytest

import copy

from arb import store
from arb.config import load_config, taker_fee
from arb.metrics import evaluate, expected_cum, spread_stats
from arb.monitor import group_pairs, normalize_position
from arb.scan import ScanParams, render_table, run_scan

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def chip_series():
    return [float(x) for x in load("chip_spread_chart.json")["spreads"]]


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    return conn


class FakeClient:
    """History per market: Hyperliquid = the CHIP chart spread, Aster = 0, so the pair spread = the chart."""

    def call(self, tool, args=None):
        if tool == "get_funding_rate_history":
            raw = load("chip_spread_chart.json")
            start = datetime.fromisoformat(raw["start"].replace("Z", "+00:00"))
            rate = (lambda s: s) if args["exchange"] == "hyperliquid" else (lambda s: "0")
            return [{"date": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00.000Z"),
                     "fundingRate": rate(s)} for i, s in enumerate(raw["spreads"])]
        if tool == "get_funding_spread_chart":
            raw = load("chip_spread_chart.json")
            start = datetime.fromisoformat(raw["start"].replace("Z", "+00:00"))
            return [{"date": (start + timedelta(hours=i)).isoformat(), "spread": s}
                    for i, s in enumerate(raw["spreads"])]
        if tool == "get_markets":
            items = [m for m in load("chip_markets.json")["items"] if m["exchange"] in args["exchanges"]]
            return {"items": items, "hasMore": False}
        if tool == "estimate_slippage":
            return load("chip_slippage.json")
        if tool == "get_quotes":
            ex = args["exchanges"][0]
            quote = {"aster": "USDT", "hyperliquid": "USDC"}[ex]
            fees = {"aster": "6", "hyperliquid": "4.5"}[ex]
            return [{"exchange": ex, "quote": {"baseSymbol": args["asset"], "quoteSymbol": quote,
                                               "feesBps": fees, "slippageBps": "1"}}]
        raise AssertionError(tool)

    def paginate(self, tool, args, **kw):
        if tool == "get_markets":
            return load("chip_markets.json")["items"]
        assert tool == "get_funding_strategies"
        return load("strategies.json")["items"]


def test_stats_capture_instability():
    s = spread_stats(chip_series())
    assert s.n_hours == 66
    assert s.now == pytest.approx(0.00003525)
    assert s.streak_h == 3                 # last 3 hours positive
    assert s.mean_24h < 0                  # despite the current APR of ~31%
    assert s.pos_72h < 0.5
    assert 0 <= s.phi <= 0.99


def test_forecast_reverts_to_mean():
    s = spread_stats(chip_series())
    naive = s.now * 72
    assert expected_cum(s, 72) < naive     # the current peak does not last the whole horizon
    ev = evaluate(s, 72, cost_rt=0.0016)
    assert ev.net_blend < 0 and ev.score_apr < 0


def test_scan_pipeline_ranks_and_flags():
    cfg = load_config()
    p = ScanParams.from_config(cfg, venues=["hyperliquid", "aster"], min_volume_usd=0,
                               min_hist_apr=-1, horizon_h=72)
    result, strategies = run_scan(FakeClient(), cfg, p, memory_db())
    ids = [c["id"] for c in result["top"]]
    assert "HYPE:robinhood>HYPE:aster" not in ids          # robinhood outside the venues
    assert "CHIP:aster>CHIP:hyperliquid" in ids
    chip = next(c for c in result["top"] if c["id"].startswith("CHIP"))
    assert chip["costs"]["slippage_rt"] > 0
    assert chip["costs"]["basis_entry"] > 0                 # short HL quoted above long Aster
    assert any("negative 24h mean" in f for f in chip["flags"])
    assert chip["stats"]["n_hours"] == 66
    assert chip["costs"]["fees_rt"] == pytest.approx(2 * (0.0006 + 0.00045))   # fees from get_quotes
    assert "CHIP:aster>CHIP:hyperliquid" in render_table(result)


def _chip_pair(result):
    return next(c for c in result["top"] if c["id"] == "CHIP:aster>CHIP:hyperliquid")


def test_scan_slippage_covers_entry_and_exit_sides():
    cfg = load_config()
    p = ScanParams.from_config(cfg, venues=["hyperliquid", "aster"], min_volume_usd=0, min_hist_apr=-1,
                               horizon_h=72)
    result, _ = run_scan(FakeClient(), cfg, p, memory_db())
    chip = _chip_pair(result)
    # the fixture answers 4.85 bps for every side: 2 legs x (entry + exit)
    assert chip["costs"]["slippage_rt"] == pytest.approx(4 * 4.854641 / 1e4)
    assert chip["long"]["slippage_bps"] == pytest.approx(4.854641)
    assert chip["long"]["exit_slippage_bps"] == pytest.approx(4.854641)
    assert chip["long"]["entry_avg_price"] == 0.045933
    assert chip["costs"]["basis_at_size"] == pytest.approx(0.0)   # same avgPrice on both legs
    assert not any("high price impact" in f for f in chip["flags"])


def test_high_price_impact_flag_makes_pair_not_viable():
    from arb.scan import is_viable

    cfg = copy.deepcopy(load_config())
    cfg["execution"]["max_impact_bps"] = 1          # the fixture's 4.85 bps is now over the limit
    p = ScanParams.from_config(cfg, venues=["hyperliquid", "aster"], min_volume_usd=0, min_hist_apr=-1,
                               horizon_h=72)
    result, _ = run_scan(FakeClient(), cfg, p, memory_db())
    chip = _chip_pair(result)
    assert any(f.startswith("high price impact on aster") and "entry" in f for f in chip["flags"]), chip["flags"]
    assert not is_viable(chip)


def test_unfavorable_basis_flag_makes_pair_not_viable():
    from arb.scan import is_viable

    cfg = copy.deepcopy(load_config())
    cfg["execution"]["min_entry_basis"] = 0.0001   # the fixture's basis at size is exactly 0
    p = ScanParams.from_config(cfg, venues=["hyperliquid", "aster"], min_volume_usd=0, min_hist_apr=-1,
                               horizon_h=72)
    result, _ = run_scan(FakeClient(), cfg, p, memory_db())
    chip = _chip_pair(result)
    assert any(f.startswith("unfavorable basis at size") for f in chip["flags"]), chip["flags"]
    assert not is_viable(chip)


def test_exit_cost_includes_price_impact():
    from arb.monitor import exit_cost, group_pairs, normalize_position

    pairs, _ = group_pairs([normalize_position(p) for p in load("positions.json")])
    cfg = load_config()
    cost, usd = exit_cost(FakeClient(), cfg, pairs[0])
    fees = taker_fee(cfg, "hyperliquid") + taker_fee(cfg, "lighter")
    assert cost == pytest.approx(fees + 2 * 4.854641 / 1e4)          # exit sells long, buys short
    mean_notional = (pairs[0]["long"]["notional"] + pairs[0]["short"]["notional"]) / 2
    assert usd == pytest.approx(cost * mean_notional)


def test_history_spread_matches_chart():
    from arb.history import FundingHistory, pair_spread

    h = FundingHistory(FakeClient(), memory_db())
    legs = {("aster", "CHIP", "USDT"), ("hyperliquid", "CHIP", "USDC")}
    assert h.refresh(legs, 8)["fetched"] == 2
    spread = pair_spread(h.series(("aster", "CHIP", "USDT"), 7), h.series(("hyperliquid", "CHIP", "USDC"), 7))
    assert spread == pytest.approx(chip_series())


def test_pairing_positions():
    raw = [
        {"exchange": "aster", "symbol": "CHIPUSDT", "side": "LONG", "size": "1000", "markPrice": "0.0459",
         "liquidationPrice": "0.03"},
        {"exchange": "hyperliquid", "baseSymbol": "CHIP", "size": "-1000", "markPrice": "0.046"},
        {"exchange": "lighter", "baseSymbol": "ETH", "side": "buy", "size": "0.1", "markPrice": "4000"},
    ]
    pairs, unmatched = group_pairs([normalize_position(p) for p in raw])
    assert len(pairs) == 1 and pairs[0]["asset"] == "CHIP"
    assert pairs[0]["short"]["exchange"] == "hyperliquid"
    assert [u["symbol"] for u in unmatched] == ["ETH"]


def test_real_positions_mark_and_liq_distance():
    """Real VOOI format: no mark price, it is derived from entry price and uPnL."""
    from arb.monitor import _liq_distance

    pairs, unmatched = group_pairs([normalize_position(p) for p in load("positions.json")])
    assert len(pairs) == 1 and not unmatched
    long, short = pairs[0]["long"], pairs[0]["short"]
    assert (long["exchange"], short["exchange"]) == ("hyperliquid", "lighter")
    assert long["mark_price"] == pytest.approx(0.37927 - 2.6796 / 290)
    assert short["mark_price"] == pytest.approx(0.37905 - 2.4998 / 290)
    assert long["notional"] == pytest.approx(290 * long["mark_price"])
    assert short["size"] == 290
    assert _liq_distance(long) == pytest.approx(0.42, abs=0.01)
    assert _liq_distance(short) == pytest.approx(0.40, abs=0.01)


def test_plan_same_size_and_thin_book_first():
    from arb.plan import build_plan

    hl = {"exchange": "hyperliquid", "baseSymbol": "MET", "quoteSymbol": "USDC", "price": "0.3745",
          "baseDecimals": 0, "maxLeverage": 3, "volume24h": 1_300_000}
    lighter = {"exchange": "lighter", "baseSymbol": "MET", "quoteSymbol": "USDC", "price": "0.3748",
               "baseDecimals": 1, "maxLeverage": 5, "volume24h": 200_000}
    plan = build_plan(hl, lighter, notional=450, leverage=2)
    assert plan["size"] == 1201                      # floor(450 / 0.37465), common decimals = 0
    assert plan["order"][0]["exchange"] == "lighter"  # lower volume: first
    assert plan["order"][0]["side"] == "SELL"
    assert not plan["problems"]
    assert build_plan(hl, lighter, 450, leverage=4)["problems"]


def test_telegram_chunks():
    from arb.telegram import chunks

    text = "\n".join(f"line {i}" for i in range(2000))
    parts = chunks(text, size=500)
    assert "".join(parts) == text and all(len(p) <= 500 for p in parts)


def test_apr_on_capital_includes_leverage():
    from arb.scan import apr_on_capital

    # 0.5% net in 7 days per leg: at 2x the capital is 1x the notional per leg → ~26% per year
    assert apr_on_capital(0.005, 168, 2) == pytest.approx(0.005 * 8760 / 168)
    assert apr_on_capital(0.005, 168, 4) == pytest.approx(2 * apr_on_capital(0.005, 168, 2))


def test_daily_summary_layout():
    from datetime import datetime

    from arb.summary import build_daily

    report = {
        "pairs": [{
            "id": "MET:hyperliquid>MET:lighter", "status": "OK", "actions": [], "leverage": 2.0,
            "notional": {"long": 107.49, "short": 107.59}, "liq_distance": {"long": 0.42, "short": 0.39},
            "upnl": -0.17, "funding_collected": 0.04,
            "spread": {"now": 1.0e-4, "mean_24h": 3.0e-5, "mean_72h": 8.9e-5, "mean_7d": 6.4e-5},
            "expected_next": {"24h": 0.0017, "168h": 0.0127},
            "exit_cost_usd": 0.3,
        }],
        "alerts": ["[INFO] MET:hyperliquid>MET:lighter not in the journal"],
    }
    balances = {"hyperliquid": {"equity": 55.13, "available": 1.66}, "lighter": {"equity": 60.91, "available": 5.95}}
    held = {"id": "MET:hyperliquid>MET:lighter", "long": {"symbol": "MET"}, "short": {"symbol": "MET"},
            "apr_on_capital": 0.25, "eval": {"breakeven_h": 55, "net_blend": 0.01}, "flags": []}
    hip3 = {"id": "xyz:SMSN:hyperliquid>SMSN:lighter", "long": {"symbol": "xyz:SMSN"}, "short": {"symbol": "SMSN"},
            "apr_on_capital": 0.17, "eval": {"breakeven_h": 40, "net_blend": 0.01}, "flags": ["below target: 17%"]}
    low = {"id": "XPL:lighter>XPL:hyperliquid", "long": {"symbol": "XPL"}, "short": {"symbol": "XPL"},
           "apr_on_capital": 0.08, "eval": {"breakeven_h": 90, "net_blend": 0.001},
           "flags": ["below target: 8% on capital (min 20%)"]}
    text = build_daily(report, balances, {"top": [held, hip3, low]}, 168, note="Spread cooling.",
                       now=datetime(2026, 10, 16, 9, 5))
    assert text.startswith("☀️ <b>Daily summary</b> · Fri 16 Oct")
    assert "✅ <b>No action.</b> Keep holding." in text and "<i>Spread cooling.</i>" in text
    assert "🟢 <b>MET</b> · long Hyperliquid / short Lighter" in text
    assert "liq <code>42%</code> / <code>39%</code> away" in text
    assert "Funding <code>+$0.04</code> · uPnL <code>−$0.17</code>" in text
    assert "Expected next 7d: <b>+1.3%</b> of notional (~+$1.37)" in text
    assert "Exit now: ~$0.30 (fees + price impact)" in text
    assert "Total <code>$116.04</code>" in text
    assert "not in the journal" not in text                      # INFO alerts stay out of the summary
    assert "<i>held</i>" in text and "<i>HIP-3</i>" in text and "<i>below target</i>" in text

    report["pairs"][0]["status"] = "ACTION"
    assert "🔴 <b>Action needed</b>" in build_daily(report, balances, None, 168)


def test_scan_notional_follows_strategy_size():
    cfg = load_config()
    assert "notional_usd" not in cfg["scan"]
    assert ScanParams.from_config(cfg).notional_usd == cfg["strategy"]["notional_per_strategy"]
    cfg["strategy"]["notional_per_strategy"] = 500
    assert ScanParams.from_config(cfg).notional_usd == 500
    assert ScanParams.from_config(cfg, notional_usd=1000).notional_usd == 1000   # --notional wins
