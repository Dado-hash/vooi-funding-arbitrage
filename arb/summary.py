"""Daily summary as a Telegram HTML message, built from the monitor report, balances and last scan.

The agent only adds an optional one-line note: numbers and layout come from code, so the message
looks the same whichever agent runs the cycle.
"""

from datetime import datetime
from html import escape as esc

from .approvals import _venue
from .metrics import HOURS_PER_YEAR
from .portfolio import PORTFOLIO_EXCLUDE
from .scan import is_viable

STATUS_ICON = {"OK": "🟢", "INFO": "🔵", "WARNING": "🟡", "ACTION": "🔴"}


def _usd(x: float | None, sign: bool = False) -> str:
    if x is None:
        return "-"
    s = f"{abs(x):,.2f}"
    return (("+" if x >= 0 else "−") + "$" + s) if sign else f"${s}"


def _apr(x: float | None) -> str:
    return "-" if x is None else f"{x * HOURS_PER_YEAR:+.0%}"


def _pair_label(pair_id: str) -> tuple[str, str, str]:
    """`MET:hyperliquid>MET:lighter` → ("MET", "Hyperliquid", "Lighter")."""
    long, short = pair_id.split(">")
    long_sym, long_ex = long.rsplit(":", 1)
    return long_sym.split(":")[-1], _venue(long_ex), _venue(short.rsplit(":", 1)[1])


def _headline(report: dict) -> str:
    levels = [p["status"] for p in report["pairs"]] + [a[1:a.index("]")] for a in report["alerts"] if a.startswith("[")]
    if "ACTION" in levels:
        return "🔴 <b>Action needed</b>: see below."
    if "WARNING" in levels:
        return "🟡 <b>Keep an eye on it</b>: see below."
    if report["pairs"]:
        return "✅ <b>No action.</b> Keep holding."
    return "✅ <b>No action.</b> No open positions."


def _position_lines(p: dict, horizon_h: float) -> list[str]:
    sym, long_ex, short_ex = _pair_label(p["id"])
    n = [x for x in p["notional"].values() if x]
    notional = sum(n) / len(n) if n else None
    liq = [p["liq_distance"].get(k) for k in ("long", "short")]
    sp = p.get("spread") or {}
    lines = [
        f"{STATUS_ICON.get(p['status'], '⚪')} <b>{esc(sym)}</b> · long {long_ex} / short {short_ex}",
        f"Size <code>{_usd(notional)}</code>/leg"
        + (f" · {p['leverage']:g}x" if p.get("leverage") else "")
        + (f" · liq <code>{liq[0]:.0%}</code> / <code>{liq[1]:.0%}</code> away" if all(x is not None for x in liq) else ""),
        f"Funding <code>{_usd(p['funding_collected'], sign=True)}</code> · uPnL <code>{_usd(p['upnl'], sign=True)}</code>",
    ]
    if sp:
        lines.append(f"Spread APR: now <b>{_apr(sp.get('now'))}</b> · 24h {_apr(sp.get('mean_24h'))} · "
                     f"72h {_apr(sp.get('mean_72h'))} · 7d {_apr(sp.get('mean_7d'))}")
    exp = (p.get("expected_next") or {}).get(f"{horizon_h:g}h")
    if exp is not None:
        dollars = f" (~{_usd(exp * notional, sign=True)})" if notional else ""
        lines.append(f"Expected next {horizon_h / 24:g}d: <b>{exp:+.1%}</b> of notional{dollars}")
    lines += [f"• {esc(a)}" for a in p.get("actions", [])]
    return lines


def _opportunity_line(i: int, c: dict, held: set[str]) -> str:
    sym, long_ex, short_ex = _pair_label(c["id"])
    roc = c.get("apr_on_capital")
    be = c.get("eval", {}).get("breakeven_h")
    parts = [f"{i}. <b>{esc(sym)}</b> {long_ex} › {short_ex}",
             f"<b>{roc:.0%}</b> on capital" if roc is not None else None,
             f"BE {be}h" if be else None]
    flags = c.get("flags", [])
    if c["id"] in held:
        tag = "held"
    elif ":" in c["long"]["symbol"] or ":" in c["short"]["symbol"]:
        tag = "HIP-3"
    elif is_viable(c) and not any(x in f for f in flags for x in PORTFOLIO_EXCLUDE):
        tag = "✅ eligible"
    else:
        tag = flags[0].split(":")[0].split(" (")[0] if flags else "not eligible"
    return " · ".join(x for x in parts if x) + f" · <i>{esc(tag)}</i>"


def build_daily(report: dict, balances: dict[str, dict], scan: dict | None, horizon_h: float,
                note: str = "", now: datetime | None = None) -> str:
    now = now or datetime.now()
    lines = [f"☀️ <b>Daily summary</b> · {now:%a %d %b}", "", _headline(report)]
    if note:
        lines.append(f"<i>{esc(note)}</i>")

    lines += ["", "<b>📌 Positions</b>"]
    if report["pairs"]:
        for p in report["pairs"]:
            lines += _position_lines(p, horizon_h) + [""]
        lines.pop()
    else:
        lines.append("None open.")
    lines += [f"• {esc(a)}" for a in report["alerts"] if not a.startswith("[INFO]")]

    lines += ["", "<b>💼 Balances</b>"]
    for ex, b in sorted(balances.items()):
        lines.append(f"{_venue(ex)} <code>{_usd(b['equity'])}</code> · free <code>{_usd(b['available'])}</code>")
    if len(balances) > 1:
        lines.append(f"Total <code>{_usd(sum(b['equity'] for b in balances.values()))}</code>")
    if not balances:
        lines.append("No balances: exchanges not connected?")

    if scan and scan.get("top"):
        held = {p["id"] for p in report["pairs"]}
        lines += ["", "<b>🔭 Top opportunities</b>"]
        lines += [_opportunity_line(i, c, held) for i, c in enumerate(scan["top"][:3], 1)]
    return "\n".join(lines)
