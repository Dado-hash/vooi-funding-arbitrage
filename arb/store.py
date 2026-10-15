"""Persistenza locale (SQLite in data/arb.db): scan, snapshot dello spread, journal posizioni."""

import json
import sqlite3
from datetime import datetime, timezone

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, params TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spread_snapshots (
    ts TEXT NOT NULL, long_ex TEXT, long_sym TEXT, short_ex TEXT, short_sym TEXT,
    spread_h REAL, volume_24h REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_pair ON spread_snapshots(long_ex, long_sym, short_ex, short_sym, ts);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    opened_ts TEXT NOT NULL, closed_ts TEXT,
    asset TEXT, long_ex TEXT, long_sym TEXT, short_ex TEXT, short_sym TEXT,
    notional_usd REAL, leverage REAL, entry_spread_h REAL, horizon_h REAL,
    thesis TEXT, close_reason TEXT, realized_usd REAL
);
CREATE TABLE IF NOT EXISTS price_snapshots (
    ts TEXT NOT NULL, exchange TEXT NOT NULL, asset TEXT NOT NULL, quote TEXT NOT NULL, price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price ON price_snapshots(exchange, asset, quote, ts);
CREATE TABLE IF NOT EXISTS monitor_runs (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, report TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "arb.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    from .approvals import init  # import locale: approvals importa store

    init(conn)
    return conn


def save_scan(conn, params: dict, result: dict) -> int:
    cur = conn.execute(
        "INSERT INTO scans (ts, params, result) VALUES (?, ?, ?)",
        (result["ts"], json.dumps(params), json.dumps(result)),
    )
    conn.commit()
    return cur.lastrowid


def save_snapshots(conn, ts: str, strategies: list[dict]) -> None:
    rows = [
        (
            ts,
            s["longMarketData"]["exchange"], s["longMarketData"]["baseSymbol"],
            s["shortMarketData"]["exchange"], s["shortMarketData"]["baseSymbol"],
            float(s["grossSpreadHourly"]), s.get("volume24h"),
        )
        for s in strategies
    ]
    conn.executemany("INSERT INTO spread_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()


def save_prices(conn, ts: str, markets: dict) -> None:
    rows = [(ts, *key, float(m["price"])) for key, m in markets.items() if m.get("price")]
    conn.executemany("INSERT INTO price_snapshots VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()


def basis_mean(conn, long_leg: tuple, short_leg: tuple, min_points: int = 24) -> tuple[float | None, int]:
    """Media storica del basis (short − long)/mid dagli snapshot di prezzo presi nello stesso scan."""
    rows = conn.execute(
        """SELECT l.price AS lp, s.price AS sp FROM price_snapshots l
           JOIN price_snapshots s ON s.ts = l.ts AND s.exchange = ? AND s.asset = ? AND s.quote = ?
           WHERE l.exchange = ? AND l.asset = ? AND l.quote = ?""",
        (*short_leg, *long_leg),
    ).fetchall()
    if len(rows) < min_points:
        return None, len(rows)
    values = [(r["sp"] - r["lp"]) / ((r["sp"] + r["lp"]) / 2) for r in rows]
    return sum(values) / len(values), len(values)


def last_scan(conn) -> dict | None:
    row = conn.execute("SELECT result FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row["result"]) if row else None


def journal_open(conn, **fields) -> int:
    fields.setdefault("opened_ts", now_iso())
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO journal ({cols}) VALUES ({marks})", tuple(fields.values()))
    conn.commit()
    return cur.lastrowid


def journal_close(conn, entry_id: int, reason: str, realized_usd: float | None) -> None:
    conn.execute(
        "UPDATE journal SET closed_ts = ?, close_reason = ?, realized_usd = ? WHERE id = ?",
        (now_iso(), reason, realized_usd, entry_id),
    )
    conn.commit()


def journal_list(conn, open_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM journal" + (" WHERE closed_ts IS NULL" if open_only else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(sql)]


def save_monitor_run(conn, report: dict) -> None:
    conn.execute("INSERT INTO monitor_runs (ts, report) VALUES (?, ?)", (report["ts"], json.dumps(report)))
    conn.commit()
