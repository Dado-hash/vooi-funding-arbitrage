"""Storico del funding per singolo mercato, con cache SQLite.

Lo spread di una coppia è `funding_short − funding_long` ora per ora (verificato contro
`get_funding_spread_chart`). Scaricando lo storico per mercato (N chiamate) si ottiene lo spread
di tutte le coppie (~N²) senza una chiamata per coppia, e senza dover pre-filtrare per APR attuale.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .client import MCPError, VooiClient

WORKERS = 8
FRESH_H = 1.5  # se l'ultimo punto in cache è più recente di così, niente fetch

SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_history (
    exchange TEXT NOT NULL, asset TEXT NOT NULL, quote TEXT NOT NULL,
    ts TEXT NOT NULL, rate REAL NOT NULL,
    PRIMARY KEY (exchange, asset, quote, ts)
);
"""

Leg = tuple[str, str, str]  # (exchange, baseSymbol, quoteSymbol)


def _fetch(client: VooiClient, leg: Leg, from_date: str, to_date: str) -> list[tuple[str, float]]:
    exchange, asset, quote = leg
    args = {"exchange": exchange, "fromDate": from_date, "toDate": to_date}
    try:
        points = client.call("get_funding_rate_history", {**args, "asset": asset})
    except MCPError as e:
        if "Ambiguous" not in str(e):
            raise
        points = client.call("get_funding_rate_history", {**args, "asset": f"{asset}{quote}"})
    return [(p["date"], float(p["fundingRate"])) for p in points or []]


class FundingHistory:
    def __init__(self, client: VooiClient, conn):
        self.client = client
        self.conn = conn
        conn.executescript(SCHEMA)

    def _bounds(self, leg: Leg) -> tuple[datetime | None, datetime | None]:
        row = self.conn.execute(
            "SELECT MIN(ts) AS first, MAX(ts) AS last FROM funding_history WHERE exchange=? AND asset=? AND quote=?",
            leg,
        ).fetchone()
        parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
        return parse(row["first"]), parse(row["last"])

    def refresh(self, legs: set[Leg], days: int) -> dict:
        """Aggiorna la cache per `legs`. Restituisce {"fetched", "cached", "errors"}."""
        now = datetime.now(timezone.utc)
        today = now.date()
        window_start = today - timedelta(days=days)
        jobs = []
        for leg in legs:
            first, last = self._bounds(leg)
            backfill = first is None or first.date() > window_start + timedelta(days=1)
            if not backfill and (now - last).total_seconds() < FRESH_H * 3600:
                continue
            # Cache più corta della finestra richiesta: riscarica tutto. Altrimenti solo dall'ultimo giorno.
            start = window_start if backfill else last.date()
            jobs.append((leg, start.isoformat()))

        def run(job):
            leg, start = job
            try:
                return leg, _fetch(self.client, leg, start, today.isoformat())
            except MCPError as e:
                return leg, e

        errors = {}
        with ThreadPoolExecutor(WORKERS) as ex:
            for leg, res in ex.map(run, jobs):
                if isinstance(res, Exception):
                    errors[":".join(leg)] = str(res)
                    continue
                self.conn.executemany(
                    "INSERT OR REPLACE INTO funding_history VALUES (?, ?, ?, ?, ?)",
                    [(*leg, ts, rate) for ts, rate in res],
                )
        self.conn.commit()
        return {"fetched": len(jobs) - len(errors), "cached": len(legs) - len(jobs), "errors": errors}

    def series(self, leg: Leg, days: int) -> dict[str, float]:
        rows = self.conn.execute(
            "SELECT ts, rate FROM funding_history WHERE exchange=? AND asset=? AND quote=? ORDER BY ts", leg
        ).fetchall()
        if not rows:
            return {}
        last = datetime.fromisoformat(rows[-1]["ts"].replace("Z", "+00:00"))
        cutoff = (last - timedelta(days=days)).isoformat().replace("+00:00", ".000Z")
        return {r["ts"]: r["rate"] for r in rows if r["ts"] > cutoff}


def pair_spread(long: dict[str, float], short: dict[str, float]) -> list[float]:
    """Spread orario short − long sulle ore presenti in entrambe le serie, dal più vecchio."""
    return [short[ts] - long[ts] for ts in sorted(long.keys() & short.keys())]
