"""TP/SL di protezione per una coppia delta-neutral.

Le due gambe stanno su venue diverse: se il prezzo si muove molto, una gamba si avvicina alla liquidazione
mentre l'altra guadagna, e il guadagno non copre la perdita senza un trasferimento. I TP/SL chiudono
ENTRAMBE le gambe allo stesso prezzo, prima della liquidazione:

- prezzo giù fino a `down`: stop-loss del long + take-profit dello short;
- prezzo su fino a `up`: stop-loss dello short + take-profit del long.

I livelli stanno a `fraction` (es. 70%) della distanza di liquidazione stimata in isolated margin,
più vicina di quella reale in cross: la stima è prudente.
"""

import math

from .portfolio import isolated_liq_move


def fmt_price(x: float, max_decimals: int, toward_up: bool) -> str:
    """Prezzo con al massimo 5 cifre significative e `max_decimals` decimali (regole Hyperliquid, accettate
    anche da Lighter). Arrotonda verso il prezzo corrente, cioè dal lato prudente."""
    sig_decimals = 4 - math.floor(math.log10(x))
    d = max(0, min(max_decimals, sig_decimals))
    f = 10 ** d
    v = (math.ceil(x * f) if toward_up else math.floor(x * f)) / f
    return f"{v:.{d}f}"


def tp_sl_levels(long_m: dict, short_m: dict, leverage: float, fraction: float) -> dict:
    lp, sp = float(long_m["price"]), float(short_m["price"])
    mid = (lp + sp) / 2
    liq_long = isolated_liq_move(leverage, float(long_m["maxLeverage"]))    # il long si liquida se il prezzo scende
    liq_short = isolated_liq_move(leverage, float(short_m["maxLeverage"]))  # lo short se sale
    max_dec = min(6 - int(m.get("baseDecimals") or 0) for m in (long_m, short_m))
    down = fmt_price(mid * (1 - fraction * liq_long), max_dec, toward_up=True)
    up = fmt_price(mid * (1 + fraction * liq_short), max_dec, toward_up=False)
    return {
        "down": down, "up": up,
        "down_move": float(down) / mid - 1, "up_move": float(up) / mid - 1,
        "liq_long_move": -liq_long, "liq_short_move": liq_short,
        "fraction": fraction,
    }


def bracket(side: str, levels: dict) -> dict:
    """Campi `takeProfit`/`stopLoss` di `create_order` per la gamba che apre con `side`."""
    if side == "buy":   # long: perde se scende
        return {"stopLoss": {"triggerPrice": levels["down"]}, "takeProfit": {"triggerPrice": levels["up"]}}
    return {"stopLoss": {"triggerPrice": levels["up"]}, "takeProfit": {"triggerPrice": levels["down"]}}


def check_against_liq(levels: dict, long_liq: float | None, short_liq: float | None) -> list[str]:
    """Con le posizioni aperte: lo stop di ogni gamba deve scattare prima della sua liquidazione reale."""
    problems = []
    if long_liq and float(levels["down"]) <= long_liq:
        problems.append(f"stop del long {levels['down']} oltre la liquidazione {long_liq:g}")
    if short_liq and float(levels["up"]) >= short_liq:
        problems.append(f"stop dello short {levels['up']} oltre la liquidazione {short_liq:g}")
    return problems
