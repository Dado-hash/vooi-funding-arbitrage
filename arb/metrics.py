"""Metriche sullo spread di funding orario e previsione del rendimento su un orizzonte.

Convenzioni:
- spread = short_rate - long_rate, frazione per ora (positivo = incassi).
- Rendimenti espressi come frazione del notional di UNA gamba (stessa convenzione della UI VOOI:
  "$100 · 3 days" = notional per gamba).
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
    pos_24h: float      # quota di ore con spread > 0
    pos_72h: float
    pos_7d: float
    streak_h: int       # ore consecutive con spread > 0 fino ad ora
    phi: float          # autocorrelazione lag-1: persistenza delle deviazioni dalla media
    half_life_h: float | None  # ore perché la deviazione attuale si dimezzi
    p25_72h: float
    mean_all: float     # media su tutto lo storico disponibile (es. 30 giorni con --days 30)
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
    """`series`: spread orario dal più vecchio al più recente. `now`: spread live, se più fresco."""
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
    """Funding cumulato atteso su `horizon_h` ore con modello AR(1) mean-reverting.

    Lo spread attuale torna verso la media di lungo periodo con persistenza `phi`:
    E[s_{t+k}] = mu + phi^k (s_t - mu).
    """
    mu = long_run_mean(s)
    now = s.now if start_spread is None else start_spread
    phi = s.phi
    geo = phi * (1 - phi ** horizon_h) / (1 - phi)
    return horizon_h * mu + (now - mu) * geo


def conservative_cum(s: SpreadStats, horizon_h: float) -> float:
    """Scenario pessimista: la media più bassa tra 24h, 72h e 7d tenuta per tutto l'orizzonte."""
    return horizon_h * min(s.mean_24h, s.mean_72h, s.mean_7d)


def breakeven_hours(s: SpreadStats, cost: float, max_h: int = 24 * 30) -> int | None:
    """Prime ore di holding dopo cui il funding atteso copre i costi round-trip."""
    if cost <= 0:
        return 0
    for h in range(1, max_h + 1):
        if expected_cum(s, h) >= cost:
            return h
    return None


@dataclass
class Evaluation:
    horizon_h: float
    exp_return: float       # funding + P&L di basis attesi sull'orizzonte (frazione del notional)
    cons_return: float      # idem, scenario pessimista
    cost_rt: float          # fee + slippage round-trip
    net_exp: float
    net_cons: float
    net_blend: float        # media di atteso e pessimista, al netto dei costi
    score_apr: float        # net_blend annualizzato: metrica di ranking
    breakeven_h: int | None

    def as_dict(self) -> dict:
        return asdict(self)


BASIS_CAPTURE = 0.5  # quota del basis d'ingresso che si assume di incassare in media


def basis_pnl(basis_entry: float | None, basis_mean: float | None = None) -> tuple[float, float]:
    """P&L di prezzo atteso e pessimista: `b0 − b_uscita`, b = (prezzo short − prezzo long)/mid.

    Con storico (`basis_mean`): atteso = b0 − media storica. Senza storico: metà del basis
    d'ingresso (convergenza parziale verso zero). Pessimista: un basis sfavorevole converge,
    uno favorevole persiste.
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
