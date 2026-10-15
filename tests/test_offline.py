"""Test offline su risposte MCP reali salvate in tests/fixtures."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import sqlite3

import pytest

from arb import store
from arb.config import load_config
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
    """Storico per mercato: Hyperliquid = spread del grafico CHIP, Aster = 0, quindi spread coppia = grafico."""

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
    assert s.streak_h == 3                 # ultime 3 ore positive
    assert s.mean_24h < 0                  # nonostante APR attuale ~31%
    assert s.pos_72h < 0.5
    assert 0 <= s.phi <= 0.99


def test_forecast_reverts_to_mean():
    s = spread_stats(chip_series())
    naive = s.now * 72
    assert expected_cum(s, 72) < naive     # il picco attuale non dura tutto l'orizzonte
    ev = evaluate(s, 72, cost_rt=0.0016)
    assert ev.net_blend < 0 and ev.score_apr < 0


def test_scan_pipeline_ranks_and_flags():
    cfg = load_config()
    p = ScanParams.from_config(cfg, venues=["hyperliquid", "aster"], min_volume_usd=0,
                               min_hist_apr=-1, horizon_h=72)
    result, strategies = run_scan(FakeClient(), cfg, p, memory_db())
    ids = [c["id"] for c in result["top"]]
    assert "HYPE:robinhood>HYPE:aster" not in ids          # robinhood fuori dalle venue
    assert "CHIP:aster>CHIP:hyperliquid" in ids
    chip = next(c for c in result["top"] if c["id"].startswith("CHIP"))
    assert chip["costs"]["slippage_rt"] > 0
    assert chip["costs"]["basis_entry"] > 0                 # short HL quota sopra long Aster
    assert any("24h negativo" in f for f in chip["flags"])
    assert chip["stats"]["n_hours"] == 66
    assert chip["costs"]["fees_rt"] == pytest.approx(2 * (0.0006 + 0.00045))   # fee da get_quotes
    assert "CHIP:aster>CHIP:hyperliquid" in render_table(result)


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


def test_plan_same_size_and_thin_book_first():
    from arb.plan import build_plan

    hl = {"exchange": "hyperliquid", "baseSymbol": "MET", "quoteSymbol": "USDC", "price": "0.3745",
          "baseDecimals": 0, "maxLeverage": 3, "volume24h": 1_300_000}
    lighter = {"exchange": "lighter", "baseSymbol": "MET", "quoteSymbol": "USDC", "price": "0.3748",
               "baseDecimals": 1, "maxLeverage": 5, "volume24h": 200_000}
    plan = build_plan(hl, lighter, notional=450, leverage=2)
    assert plan["size"] == 1201                      # floor(450 / 0.37465), decimali comuni = 0
    assert plan["order"][0]["exchange"] == "lighter"  # volume più basso: prima
    assert plan["order"][0]["side"] == "SELL"
    assert not plan["problems"]
    assert build_plan(hl, lighter, 450, leverage=4)["problems"]


def test_telegram_chunks():
    from arb.telegram import chunks

    text = "\n".join(f"riga {i}" for i in range(2000))
    parts = chunks(text, size=500)
    assert "".join(parts) == text and all(len(p) <= 500 for p in parts)
