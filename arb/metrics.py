"""Metrics on the hourly funding spread and return forecast over a holding horizon.

Conventions:
- spread = short_rate - long_rate, fraction per hour (positive = you collect).
- Returns are a fraction of the notional of ONE leg (same convention as the VOOI UI:
  "$100 · 3 days" = notional per leg).
"""

import math
import statistics
from dataclasses import asdict, dataclass

HOURS_PER_YEAR = 8760


@dataclass
class SpreadStats:
    n_hours: int
    now: float
    mean_24h: float
    mean_72h: float
    mean_7d: float
    std_7d: float
    pos_24h: float      # share of hours with spread > 0
    pos_72h: float
    pos_7d: float
    streak_h: int       # consecutive hours with spread > 0 up to now
    phi: float          # lag-1 autocorrelation: persistence of deviations from the mean
    half_life_h: float | None  # hours for the current deviation to halve
    p25_72h: float
    mean_all: float     # mean over the whole available history (e.g. 30 days with --days 30)
    pos_all: float

    def as_dict(self) -> dict:
        return asdict(self)


def _tail(xs: list[float], h: int) -> list[float]:
    return xs[-h:] if len(xs) > h else xs


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    i = (len(s) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def _lag1_autocorr(xs: list[float]) -> float:
    m = statistics.fmean(xs)
    d = [x - m for x in xs]
    var = sum(v * v for v in d)
    if var == 0:
        return 0.99
    cov = sum(d[i] * d[i - 1] for i in range(1, len(d)))
    return min(max(cov / var, 0.0), 0.99)


def spread_stats(series: list[float], now: float | None = None) -> SpreadStats | None:
    """`series`: hourly spread, oldest first. `now`: live spread, if fresher."""
    if len(series) < 6:
        return None
    w24, w72, w7d = _tail(series, 24), _tail(series, 72), _tail(series, 168)
    streak = 0
    for x in reversed(series):
        if x <= 0:
            break
        streak += 1
    phi = _lag1_autocorr(w7d)
    half_life = math.log(0.5) / math.log(phi) if 0 < phi < 1 else None
    pos = lambda w: sum(x > 0 for x in w) / len(w)
    return SpreadStats(
        n_hours=len(series),
        now=series[-1] if now is None else now,
        mean_24h=statistics.fmean(w24),
        mean_72h=statistics.fmean(w72),
        mean_7d=statistics.fmean(w7d),
        std_7d=statistics.pstdev(w7d),
        pos_24h=pos(w24),
        pos_72h=pos(w72),
        pos_7d=pos(w7d),
        streak_h=streak,
        phi=phi,
        half_life_h=half_life,
        p25_72h=_quantile(w72, 0.25),
        mean_all=statistics.fmean(series),
        pos_all=pos(series),
    )


def long_run_mean(s: SpreadStats) -> float:
    return 0.5 * s.mean_72h + 0.5 * s.mean_7d


def expected_cum(s: SpreadStats, horizon_h: float, start_spread: float | None = None) -> float:
    """Expected cumulative funding over `horizon_h` hours with a mean-reverting AR(1) model.

    The current spread reverts to the long-run mean with persistence `phi`:
    E[s_{t+k}] = mu + phi^k (s_t - mu).
    """
    mu = long_run_mean(s)
    now = s.now if start_spread is None else start_spread
    phi = s.phi
    geo = phi * (1 - phi ** horizon_h) / (1 - phi)
    return horizon_h * mu + (now - mu) * geo


def conservative_cum(s: SpreadStats, horizon_h: float) -> float:
    """Pessimistic scenario: the lowest of the 24h, 72h and 7d means, held for the whole horizon."""
    return horizon_h * min(s.mean_24h, s.mean_72h, s.mean_7d)


def breakeven_hours(s: SpreadStats, cost: float, max_h: int = 24 * 30) -> int | None:
    """First holding hour after which expected funding covers the round-trip costs."""
    if cost <= 0:
        return 0
    for h in range(1, max_h + 1):
        if expected_cum(s, h) >= cost:
            return h
    return None


@dataclass
class Evaluation:
    horizon_h: float
    exp_return: float       # expected funding + basis P&L over the horizon (fraction of notional)
    cons_return: float      # same, pessimistic scenario
    cost_rt: float          # round-trip fees + slippage
    net_exp: float
    net_cons: float
    net_blend: float        # mean of expected and pessimistic, net of costs
    score_apr: float        # annualized net_blend: the ranking metric
    breakeven_h: int | None

    def as_dict(self) -> dict:
        return asdict(self)


BASIS_CAPTURE = 0.5  # share of the entry basis assumed to be captured on average


def basis_pnl(basis_entry: float | None, basis_mean: float | None = None) -> tuple[float, float]:
    """Expected and pessimistic price P&L: `b0 − b_exit`, b = (short price − long price)/mid.

    With history (`basis_mean`): expected = b0 − historical mean. Without: half the entry basis
    (partial convergence to zero). Pessimistic: an unfavorable basis converges, a favorable one
    persists.
    """
    if basis_entry is None:
        return 0.0, 0.0
    exp = basis_entry - basis_mean if basis_mean is not None else basis_entry * BASIS_CAPTURE
    return exp, min(exp, 0.0, basis_entry)


def evaluate(s: SpreadStats, horizon_h: float, cost_rt: float,
             basis_entry: float | None = None, basis_mean: float | None = None) -> Evaluation:
    b_exp, b_cons = basis_pnl(basis_entry, basis_mean)
    exp = expected_cum(s, horizon_h) + b_exp
    cons = conservative_cum(s, horizon_h) + b_cons
    blend = 0.5 * (exp + cons) - cost_rt
    return Evaluation(
        horizon_h=horizon_h,
        exp_return=exp,
        cons_return=cons,
        cost_rt=cost_rt,
        net_exp=exp - cost_rt,
        net_cons=cons - cost_rt,
        net_blend=blend,
        score_apr=blend / horizon_h * HOURS_PER_YEAR,
        breakeven_h=breakeven_hours(s, cost_rt),
    )
