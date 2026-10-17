"""CLI `arb`: scan, venues, pair, monitor, journal, call. See README."""

import argparse
import json
import sys
from collections import Counter

from .client import MCPError, VooiClient
from .config import DATA_DIR, load_config, trading_mode
from .scan import (ScanParams, fetch_strategies, pair_id, render_table, run_scan,
                   top_per_venue_pair)
from . import store


def _csv(x: str | None) -> list[str] | None:
    return [v.strip() for v in x.split(",") if v.strip()] if x else None


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _save(conn, result: dict, strategies: list[dict], name: str = "latest") -> None:
    store.save_snapshots(conn, result["ts"], strategies)
    store.save_scan(conn, result["params"], result)
    out = DATA_DIR / "scans"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(result, indent=2, default=str))


def _params(args, cfg, **extra) -> ScanParams:
    venues = [] if args.all_venues else (_csv(args.venues) or (cfg["venues"]["dex"] if args.dex else None))
    return ScanParams.from_config(
        cfg, venues=venues, notional_usd=args.notional, horizon_h=args.horizon,
        min_hist_apr=args.min_hist_apr, deep=args.deep, assets=_csv(args.assets),
        history_days=args.days, **extra,
    )


def cmd_scan(args, cfg, client) -> None:
    p = _params(args, cfg)
    conn = store.connect()
    result, strategies = run_scan(client, cfg, p, conn)
    _save(conn, result, strategies)
    _dump(result) if args.json else print(render_table(result))


def cmd_venues(args, cfg, client) -> None:
    """Which venue combination pays the most: top-k pairs analysed in depth for each combination."""
    from .scan import venue_pair
    from .venues import render, summarize

    p = _params(args, cfg)
    conn = store.connect()
    result, strategies = run_scan(client, cfg, p, conn, select_deep=top_per_venue_pair(args.per_pair))
    _save(conn, result, strategies, name="venues")
    universe = Counter(
        "+".join(sorted((s["longMarketData"]["exchange"], s["shortMarketData"]["exchange"]))) for s in strategies
    )
    rows = summarize(result, universe)
    _dump(rows) if args.json else print(render(rows, result))


def cmd_portfolio(args, cfg, client) -> None:
    from .capital import balance_capital, fetch_balances
    from .portfolio import render, simulate

    p = _params(args, cfg)
    capital, source = args.capital, "--capital"
    if capital is None:
        # Default: the equity actually held on the configured venues (or on all of them if none is set).
        capital, source = balance_capital(fetch_balances(client), p.venues)
        if not capital:
            sys.exit("no balances found on the venues: pass --capital")
    sim = simulate(client, cfg, store.connect(), p, capital, args.leverage, args.max_k, args.utilization)
    sim["params"]["capital_source"] = source
    _dump(sim) if args.json else print(render(sim))


def cmd_plan(args, cfg, client) -> None:
    from .plan import order_plan, render_plan

    text = render_plan(order_plan(client, args.long, args.short, args.notional, args.leverage), args.title or "")
    print(text)
    if args.send:
        from .telegram import send
        send(text)


def cmd_tg(args, cfg, client) -> None:
    from .telegram import TelegramError, chat_ids, poll, send, wait_decision

    try:
        if args.action == "chat-id":
            _dump(chat_ids())
        elif args.action == "poll":
            _dump(poll(store.connect()))
        elif args.action == "wait":
            print(wait_decision(store.connect(), args.text, args.timeout))
        else:
            text = sys.stdin.read() if args.text in (None, "-") else args.text
            print(f"sent {send(text)} messages")
    except TelegramError as e:
        sys.exit(f"Telegram error: {e}")


def cmd_propose(args, cfg, client) -> None:
    from . import approvals
    from .telegram import send_proposal

    conn = store.connect()
    ttl = args.ttl or cfg.get("execution", {}).get("proposal_ttl_min", 15)
    mode = trading_mode()
    capital = None
    if args.kind == "open":
        from .capital import check_capital

        notional = args.notional or ScanParams.from_config(cfg).notional_usd
        exchanges = [args.long.rsplit(":", 1)[1], args.short.rsplit(":", 1)[1]]
        capital = check_capital(client, cfg, exchanges, notional, args.leverage)
        if capital["missing"] > 0 or (not capital["ok"] and mode == "execute"):
            # No proposal: funds must be fixed first. In execute, approval only arrives once capital is ready.
            text = "\n".join([f"OPPORTUNITY {args.long} > {args.short} (${notional:,.0f}/leg, leverage {args.leverage:g}x)",
                              "I will not propose the trade until the capital is ready.", ""] + capital["lines"]
                             + ([f"", f"Why the trade: {args.reason}"] if args.reason else []))
            print(text)
            if not args.no_send:
                from .telegram import send
                send(text)
            print("\n[capital] proposal NOT created: funds need fixing. Check again next cycle.")
            return
        ex = cfg.get("execution", {})
        frac = ex.get("tp_sl_liq_fraction", 0.7) if ex.get("tp_sl", True) else None
        from .scan import min_entry_basis

        try:
            p = approvals.propose_open(client, conn, args.long, args.short, notional, args.leverage, args.reason,
                                       ttl, tp_sl_fraction=frac, max_impact_bps=ex.get("max_impact_bps"),
                                       min_basis=min_entry_basis(cfg))
        except approvals.BasisError as e:
            # The long would be bought above the short's price: never open at an unfavorable basis.
            print(f"[basis] proposal NOT created: {e}. Check again next cycle.")
            return
    else:
        p = approvals.propose_close(client, conn, args.long, args.short, args.reason, ttl)
    from html import escape

    from .telegram import plain

    cap_html = "\n".join(escape(l) for l in capital["lines"]) if capital else ""
    if mode == "notify":
        text = approvals.manual_instructions(p)
        if capital:
            if capital["moves"]:
                text = "⚠️ <b>BEFORE OPENING</b>\n" + cap_html + "\n\n" + text
            else:
                text += "\n\n💼 " + cap_html
        approvals.set_status(conn, p["id"], "notified")
        print(plain(text))
        if not args.no_send:
            from .telegram import send
            send(text, as_html=True)
        print(f"\n[notify] instructions #{p['id']} {'sent' if not args.no_send else 'created'}: the user operates on VOOI. "
              f"At the next cycle check with `arb monitor` and, if opened, `arb journal open`.")
        return
    if capital:
        p["summary"] += "\n\n💼 " + cap_html
    print(plain(p["summary"]))
    if not args.no_send:
        approvals.set_status(conn, p["id"], "pending", tg_message_id=send_proposal(p))
        print(f"\n[execute] proposal #{p['id']} sent, expires {p['expires_ts']}. "
              f"Now: `uv run arb tg wait {p['id']}` in background, then `uv run arb execute {p['id']}` if approved.")
    else:
        print(f"\nproposal #{p['id']} created (not sent)")


def cmd_capital(args, cfg, client) -> None:
    """Balances per venue and transfers needed to open a strategy (or just the balances)."""
    from .capital import check_capital, fetch_balances

    if args.notional:
        venues = _csv(args.venues) or cfg["scan"]["venues"]
        r = check_capital(client, cfg, venues, args.notional, args.leverage)
        _dump({k: v for k, v in r.items() if k != "lines"}) if args.json else print("\n".join(r["lines"]))
    else:
        balances = fetch_balances(client)
        if args.json:
            _dump(balances)
        elif not balances:
            print("no balances: exchanges not connected?")
        for ex, b in balances.items():
            print(f"{ex}: equity ${b['equity']:,.2f} · available ${b['available']:,.2f} · in use ${b['used']:,.2f}")


def cmd_proposals(args, cfg, client) -> None:
    from . import approvals

    conn = store.connect()
    approvals.expire_old(conn)
    for p in approvals.list_proposals(conn, args.status):
        print(f"#{p['id']} {p['status']:<9} {p['kind']:<5} {p['payload']['pair']}  created {p['created_ts']}  "
              f"expires {p['expires_ts']}")


def cmd_execute(args, cfg, client) -> None:
    from . import approvals
    from .executor import check, execute, render_result

    conn = store.connect()
    if args.dry_run:
        p = approvals.get(conn, args.id)
        problems = check(client, cfg, p) if p else ["proposal does not exist"]
        print("\n".join(problems) or "checks ok: executable")
        return
    result = execute(client, cfg, conn, args.id)
    text = render_result(result)
    print(text)
    try:
        from .telegram import send
        send(text)
    except Exception as e:  # the notification must not hide the outcome of the execution
        print(f"(Telegram notification failed: {e})")


def cmd_pair(args, cfg, client) -> None:
    """Full analysis of a single pair, even outside the scan filters."""
    # rsplit: HIP-3 symbols contain ":" (e.g. xyz:GOLD:hyperliquid)
    long_sym, long_ex = args.long.rsplit(":", 1)
    short_sym, short_ex = args.short.rsplit(":", 1)
    target = f"{long_sym}:{long_ex}>{short_sym}:{short_ex}"
    p = ScanParams.from_config(
        cfg, venues=[long_ex, short_ex], both_legs=True, assets=[long_sym, short_sym],
        min_hist_apr=-1e9, min_volume_usd=0, notional_usd=args.notional, horizon_h=args.horizon, deep=1,
    )
    strategies = [s for s in fetch_strategies(client, p) if pair_id(s) == target]
    if not strategies:
        sys.exit(f"pair {target} not found in get_funding_strategies")
    result, _ = run_scan(client, cfg, p, store.connect(), strategies=strategies[:1])
    _dump(result["top"][0] if result["top"] else result)


def cmd_monitor(args, cfg, client) -> None:
    from .monitor import render_report, run_monitor

    conn = store.connect()
    if args.scan:
        p = ScanParams.from_config(cfg)
        result, strategies = run_scan(client, cfg, p, conn)
        _save(conn, result, strategies)
    report = run_monitor(client, cfg, conn)
    store.save_monitor_run(conn, report)
    _dump(report) if args.json else print(render_report(report))


def cmd_journal(args, cfg, client) -> None:
    conn = store.connect()
    if args.action == "open":
        long_sym, long_ex = args.long.rsplit(":", 1)
        short_sym, short_ex = args.short.rsplit(":", 1)
        jid = store.journal_open(
            conn, asset=long_sym, long_ex=long_ex, long_sym=long_sym, short_ex=short_ex,
            short_sym=short_sym, notional_usd=args.notional, leverage=args.leverage,
            entry_spread_h=args.entry_spread, horizon_h=args.horizon, thesis=args.thesis,
        )
        print(f"journal #{jid} opened")
    elif args.action == "close":
        store.journal_close(conn, args.id, args.reason, args.realized)
        print(f"journal #{args.id} closed")
    else:
        _dump(store.journal_list(conn, open_only=args.open))


def cmd_daily(args, cfg, client) -> None:
    """Daily summary: `due` if today (local time) has not been sent yet and it is past 9:00.
    `--send` builds the summary, sends it and marks it sent; `--show` only prints it."""
    from datetime import datetime

    conn = store.connect()
    today = datetime.now().date().isoformat()
    row = conn.execute("SELECT value FROM kv WHERE key = 'daily_sent'").fetchone()
    if args.send or args.show:
        from .capital import fetch_balances
        from .monitor import run_monitor
        from .summary import build_daily
        from .telegram import plain, send

        client = VooiClient()
        text = build_daily(run_monitor(client, cfg, conn), fetch_balances(client), store.last_scan(conn),
                           cfg.get("scan", {}).get("horizon_h", 168), note=args.note)
        print(plain(text))
        if args.send:
            send(text, as_html=True)
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('daily_sent', ?)", (today,))
            conn.commit()
            print("\nsent and marked")
    elif args.mark:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('daily_sent', ?)", (today,))
        conn.commit()
        print("marked")
    else:
        due = (row is None or row["value"] != today) and datetime.now().hour >= 9
        print("due" if due else "done")


def cmd_call(args, cfg, client) -> None:
    _dump(client.call(args.tool, json.loads(args.args) if args.args else {}))


def _scan_args(sp) -> None:
    sp.add_argument("--venues", help="e.g. hyperliquid,aster (default from config.toml)")
    sp.add_argument("--dex", action="store_true", help="all decentralized venues ([venues].dex)")
    sp.add_argument("--all-venues", action="store_true")
    sp.add_argument("--assets", help="limit to these assets, e.g. BTC,ETH")
    sp.add_argument("--notional", type=float, help="USD per leg")
    sp.add_argument("--horizon", type=float, help="holding hours")
    sp.add_argument("--min-hist-apr", type=float, help="minimum 7d mean APR, fraction (0.05 = 5%%)")
    sp.add_argument("--deep", type=int)
    sp.add_argument("--days", type=int, help="days of history (default 7; 30 to assess persistence)")
    sp.add_argument("--json", action="store_true")


def main() -> None:
    ap = argparse.ArgumentParser(prog="arb", description="VOOI funding arbitrage analysis")
    sub = ap.add_subparsers(dest="cmd", required=True)

    _scan_args(sub.add_parser("scan", help="find and rank the strategies"))
    ve = sub.add_parser("venues", help="compare venue combinations")
    _scan_args(ve)
    ve.add_argument("--per-pair", type=int, default=5, help="pairs analysed in depth per combination")

    po = sub.add_parser("portfolio", help="simulate 1..K strategies in parallel on a given capital")
    _scan_args(po)
    po.add_argument("--capital", type=float, default=None,
                    help="total capital, split equally among the venues (default: equity from get_accounts)")
    po.add_argument("--leverage", type=float, default=2)
    po.add_argument("--max-k", type=int, default=5, help="maximum number of strategies in parallel")
    po.add_argument("--utilization", type=float, default=0.9, help="share of margin committed (buffer for fees and P&L)")

    pl = sub.add_parser("plan", help="order plan to execute by hand on VOOI")
    pl.add_argument("long", help="SYM:exchange of the long leg, e.g. MET:hyperliquid")
    pl.add_argument("short", help="SYM:exchange of the short leg, e.g. MET:lighter")
    pl.add_argument("--notional", type=float, required=True, help="USD per leg")
    pl.add_argument("--leverage", type=float, default=2)
    pl.add_argument("--title")
    pl.add_argument("--send", action="store_true", help="also send on Telegram")

    tg = sub.add_parser("tg", help="Telegram: messages and approvals")
    tg.add_argument("action", choices=["send", "chat-id", "poll", "wait"])
    tg.add_argument("text", nargs="?", help="send: text ('-' or omitted = stdin) · wait: proposal id")
    tg.add_argument("--timeout", type=int, default=900, help="wait: maximum seconds to wait")

    pp = sub.add_parser("propose", help="create a proposal and send it on Telegram with Approve/Reject")
    pp.add_argument("kind", choices=["open", "close"])
    pp.add_argument("long", help="SYM:exchange of the long leg")
    pp.add_argument("short", help="SYM:exchange of the short leg")
    pp.add_argument("--notional", type=float, help="USD per leg (open; default from config)")
    pp.add_argument("--leverage", type=float, default=2)
    pp.add_argument("--reason", default="", help="rationale shown in the message")
    pp.add_argument("--ttl", type=float, help="validity in minutes")
    pp.add_argument("--no-send", action="store_true")

    sub.add_parser("mode", help="show TRADING_MODE (notify | execute)")
    cp = sub.add_parser("capital", help="balances per venue and transfers needed")
    cp.add_argument("--notional", type=float, help="USD per leg of the strategy to open")
    cp.add_argument("--leverage", type=float, default=2)
    cp.add_argument("--venues", help="default: venues in config.toml")
    cp.add_argument("--json", action="store_true")
    da = sub.add_parser("daily", help="daily summary: due? (--mark after sending)")
    da.add_argument("--mark", action="store_true")
    da.add_argument("--send", action="store_true", help="build the summary, send it to Telegram and mark it sent")
    da.add_argument("--show", action="store_true", help="build the summary and print it, without sending")
    da.add_argument("--note", default="", help="optional one-line comment from the agent, shown under the headline")

    pls = sub.add_parser("proposals", help="list proposals")
    pls.add_argument("--status", choices=["pending", "notified", "approved", "rejected", "expired", "stale", "executed", "failed"])

    ex = sub.add_parser("execute", help="execute an APPROVED proposal")
    ex.add_argument("id")
    ex.add_argument("--dry-run", action="store_true", help="checks only, no orders")

    pr = sub.add_parser("pair", help="full analysis of a pair")
    pr.add_argument("long", help="SYM:exchange of the long leg, e.g. CHIP:aster")
    pr.add_argument("short", help="SYM:exchange of the short leg, e.g. CHIP:hyperliquid")
    pr.add_argument("--notional", type=float)
    pr.add_argument("--horizon", type=float)

    mo = sub.add_parser("monitor", help="check open positions and propose actions")
    mo.add_argument("--scan", action="store_true", help="refresh the scan first")
    mo.add_argument("--json", action="store_true")

    jo = sub.add_parser("journal", help="log of opened/closed strategies")
    jsub = jo.add_subparsers(dest="action", required=True)
    jo_open = jsub.add_parser("open")
    jo_open.add_argument("long")
    jo_open.add_argument("short")
    jo_open.add_argument("--notional", type=float, required=True)
    jo_open.add_argument("--leverage", type=float)
    jo_open.add_argument("--entry-spread", type=float, help="hourly spread at entry")
    jo_open.add_argument("--horizon", type=float)
    jo_open.add_argument("--thesis", default="")
    jo_close = jsub.add_parser("close")
    jo_close.add_argument("id", type=int)
    jo_close.add_argument("--reason", required=True)
    jo_close.add_argument("--realized", type=float)
    jo_list = jsub.add_parser("list")
    jo_list.add_argument("--open", action="store_true")

    ca = sub.add_parser("call", help="call a raw MCP tool")
    ca.add_argument("tool")
    ca.add_argument("args", nargs="?", help="JSON arguments")

    args = ap.parse_args()
    cfg = load_config()
    try:
        if args.cmd == "mode":
            print(trading_mode())
            return
        client = None if args.cmd in ("journal", "tg", "proposals", "daily") else VooiClient()
        {"scan": cmd_scan, "venues": cmd_venues, "portfolio": cmd_portfolio, "plan": cmd_plan, "tg": cmd_tg,
         "propose": cmd_propose, "daily": cmd_daily, "capital": cmd_capital, "proposals": cmd_proposals, "execute": cmd_execute,
         "pair": cmd_pair, "monitor": cmd_monitor,
         "journal": cmd_journal, "call": cmd_call}[args.cmd](args, cfg, client)
    except MCPError as e:
        sys.exit(f"MCP error: {e}")
    except Exception as e:
        from .telegram import TelegramError

        if isinstance(e, TelegramError):
            sys.exit(f"Telegram error: {e}")
        raise


if __name__ == "__main__":
    main()
