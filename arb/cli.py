"""CLI `arb`: scan, venues, pair, monitor, journal, call. Vedi README."""

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
    """Quale combinazione di venue rende di più: top-k coppie analizzate a fondo per ogni combinazione."""
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
    from .portfolio import render, simulate

    p = _params(args, cfg)
    sim = simulate(client, cfg, store.connect(), p, args.capital, args.leverage, args.max_k, args.utilization)
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
            print(f"inviati {send(text)} messaggi")
    except TelegramError as e:
        sys.exit(f"errore Telegram: {e}")


def cmd_propose(args, cfg, client) -> None:
    from . import approvals
    from .telegram import send_proposal

    conn = store.connect()
    ttl = args.ttl or cfg.get("execution", {}).get("proposal_ttl_min", 15)
    mode = trading_mode()
    capital = None
    if args.kind == "open":
        from .capital import check_capital

        notional = args.notional or cfg.get("strategy", {}).get("notional_per_strategy") or cfg["scan"]["notional_usd"]
        exchanges = [args.long.rsplit(":", 1)[1], args.short.rsplit(":", 1)[1]]
        capital = check_capital(client, cfg, exchanges, notional, args.leverage)
        if capital["missing"] > 0 or (not capital["ok"] and mode == "execute"):
            # Niente proposta: prima vanno sistemati i fondi. In execute l'approvazione arriva solo a capitale pronto.
            text = "\n".join([f"OPPORTUNITÀ {args.long} > {args.short} (${notional:,.0f}/gamba, leva {args.leverage:g}x)",
                              "Non propongo il trade finché il capitale non è pronto.", ""] + capital["lines"]
                             + ([f"", f"Perché il trade: {args.reason}"] if args.reason else []))
            print(text)
            if not args.no_send:
                from .telegram import send
                send(text)
            print("\n[capitale] proposta NON creata: fondi da sistemare. Ricontrolla al prossimo ciclo.")
            return
        p = approvals.propose_open(client, conn, args.long, args.short, notional, args.leverage, args.reason, ttl)
    else:
        p = approvals.propose_close(client, conn, args.long, args.short, args.reason, ttl)
    if mode == "notify":
        text = approvals.manual_instructions(p)
        if capital:
            if capital["moves"]:
                text = "PRIMA DI APRIRE:\n" + "\n".join(capital["lines"]) + "\n\n" + text
            else:
                text += "\n\n" + "\n".join(capital["lines"])
        approvals.set_status(conn, p["id"], "notified")
        print(text)
        if not args.no_send:
            from .telegram import send
            send(text)
        print(f"\n[notify] istruzioni #{p['id']} {'inviate' if not args.no_send else 'create'}: l'utente opera su VOOI. "
              f"Al prossimo ciclo verifica con `arb monitor` e, se aperta, `arb journal open`.")
        return
    if capital:
        p["summary"] += "\n\n" + "\n".join(capital["lines"])
    print(p["summary"])
    if not args.no_send:
        approvals.set_status(conn, p["id"], "pending", tg_message_id=send_proposal(p))
        print(f"\n[execute] proposta #{p['id']} inviata, scade {p['expires_ts']}. "
              f"Ora: `uv run arb tg wait {p['id']}` in background, poi `uv run arb execute {p['id']}` se approvata.")
    else:
        print(f"\nproposta #{p['id']} creata (non inviata)")


def cmd_capital(args, cfg, client) -> None:
    """Saldi per venue e spostamenti necessari per aprire una strategia (o solo i saldi)."""
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
            print("nessun saldo: exchange non collegati?")
        for ex, b in balances.items():
            print(f"{ex}: equity ${b['equity']:,.2f} · disponibile ${b['available']:,.2f} · in uso ${b['used']:,.2f}")


def cmd_proposals(args, cfg, client) -> None:
    from . import approvals

    conn = store.connect()
    approvals.expire_old(conn)
    for p in approvals.list_proposals(conn, args.status):
        print(f"#{p['id']} {p['status']:<9} {p['kind']:<5} {p['payload']['pair']}  creata {p['created_ts']}  "
              f"scade {p['expires_ts']}")


def cmd_execute(args, cfg, client) -> None:
    from . import approvals
    from .executor import check, execute, render_result

    conn = store.connect()
    if args.dry_run:
        p = approvals.get(conn, args.id)
        problems = check(client, cfg, p) if p else ["proposta inesistente"]
        print("\n".join(problems) or "controlli ok: eseguibile")
        return
    result = execute(client, cfg, conn, args.id)
    text = render_result(result)
    print(text)
    try:
        from .telegram import send
        send(text)
    except Exception as e:  # la notifica non deve mascherare l'esito dell'esecuzione
        print(f"(notifica Telegram fallita: {e})")


def cmd_pair(args, cfg, client) -> None:
    """Analisi completa di una singola coppia, anche fuori dai filtri dello scan."""
    # rsplit: i simboli HIP-3 contengono ":" (es. xyz:GOLD:hyperliquid)
    long_sym, long_ex = args.long.rsplit(":", 1)
    short_sym, short_ex = args.short.rsplit(":", 1)
    target = f"{long_sym}:{long_ex}>{short_sym}:{short_ex}"
    p = ScanParams.from_config(
        cfg, venues=[long_ex, short_ex], both_legs=True, assets=[long_sym, short_sym],
        min_hist_apr=-1e9, min_volume_usd=0, notional_usd=args.notional, horizon_h=args.horizon, deep=1,
    )
    strategies = [s for s in fetch_strategies(client, p) if pair_id(s) == target]
    if not strategies:
        sys.exit(f"coppia {target} non trovata in get_funding_strategies")
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
        print(f"journal #{jid} aperto")
    elif args.action == "close":
        store.journal_close(conn, args.id, args.reason, args.realized)
        print(f"journal #{args.id} chiuso")
    else:
        _dump(store.journal_list(conn, open_only=args.open))


def cmd_daily(args, cfg, client) -> None:
    """Riepilogo giornaliero: `due` se oggi (ora locale) non è ancora stato inviato e sono passate le 9."""
    from datetime import datetime

    conn = store.connect()
    today = datetime.now().date().isoformat()
    row = conn.execute("SELECT value FROM kv WHERE key = 'daily_sent'").fetchone()
    if args.mark:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('daily_sent', ?)", (today,))
        conn.commit()
        print("marcato")
    else:
        due = (row is None or row["value"] != today) and datetime.now().hour >= 9
        print("due" if due else "done")


def cmd_call(args, cfg, client) -> None:
    _dump(client.call(args.tool, json.loads(args.args) if args.args else {}))


def _scan_args(sp) -> None:
    sp.add_argument("--venues", help="es. hyperliquid,aster (default da config.toml)")
    sp.add_argument("--dex", action="store_true", help="tutte le venue decentralizzate ([venues].dex)")
    sp.add_argument("--all-venues", action="store_true")
    sp.add_argument("--assets", help="limita a questi asset, es. BTC,ETH")
    sp.add_argument("--notional", type=float, help="USD per gamba")
    sp.add_argument("--horizon", type=float, help="ore di holding")
    sp.add_argument("--min-hist-apr", type=float, help="APR medio 7d minimo, frazione (0.05 = 5%%)")
    sp.add_argument("--deep", type=int)
    sp.add_argument("--days", type=int, help="giorni di storico (default 7; 30 per valutare la persistenza)")
    sp.add_argument("--json", action="store_true")


def main() -> None:
    ap = argparse.ArgumentParser(prog="arb", description="Analisi funding arbitrage VOOI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    _scan_args(sub.add_parser("scan", help="trova e classifica le strategie"))
    ve = sub.add_parser("venues", help="confronta le combinazioni di venue")
    _scan_args(ve)
    ve.add_argument("--per-pair", type=int, default=5, help="coppie analizzate a fondo per combinazione")

    po = sub.add_parser("portfolio", help="simula 1..K strategie in parallelo su un capitale dato")
    _scan_args(po)
    po.add_argument("--capital", type=float, default=1000, help="capitale totale, diviso in parti uguali tra le venue")
    po.add_argument("--leverage", type=float, default=2)
    po.add_argument("--max-k", type=int, default=5, help="massimo numero di strategie in parallelo")
    po.add_argument("--utilization", type=float, default=0.9, help="quota del margine impegnata (buffer per fee e P&L)")

    pl = sub.add_parser("plan", help="piano d'ordine da eseguire a mano su VOOI")
    pl.add_argument("long", help="SYM:exchange della gamba long, es. MET:hyperliquid")
    pl.add_argument("short", help="SYM:exchange della gamba short, es. MET:lighter")
    pl.add_argument("--notional", type=float, required=True, help="USD per gamba")
    pl.add_argument("--leverage", type=float, default=2)
    pl.add_argument("--title")
    pl.add_argument("--send", action="store_true", help="invia anche su Telegram")

    tg = sub.add_parser("tg", help="Telegram: messaggi e approvazioni")
    tg.add_argument("action", choices=["send", "chat-id", "poll", "wait"])
    tg.add_argument("text", nargs="?", help="send: testo ('-' o assente = stdin) · wait: id proposta")
    tg.add_argument("--timeout", type=int, default=900, help="wait: secondi massimi di attesa")

    pp = sub.add_parser("propose", help="crea una proposta e la manda su Telegram con Approva/Rifiuta")
    pp.add_argument("kind", choices=["open", "close"])
    pp.add_argument("long", help="SYM:exchange della gamba long")
    pp.add_argument("short", help="SYM:exchange della gamba short")
    pp.add_argument("--notional", type=float, help="USD per gamba (open; default da config)")
    pp.add_argument("--leverage", type=float, default=2)
    pp.add_argument("--reason", default="", help="motivazione mostrata nel messaggio")
    pp.add_argument("--ttl", type=float, help="minuti di validità")
    pp.add_argument("--no-send", action="store_true")

    sub.add_parser("mode", help="mostra TRADING_MODE (notify | execute)")
    cp = sub.add_parser("capital", help="saldi per venue e spostamenti necessari")
    cp.add_argument("--notional", type=float, help="USD per gamba della strategia da aprire")
    cp.add_argument("--leverage", type=float, default=2)
    cp.add_argument("--venues", help="default: venue in config.toml")
    cp.add_argument("--json", action="store_true")
    da = sub.add_parser("daily", help="riepilogo giornaliero: dovuto? (--mark dopo l'invio)")
    da.add_argument("--mark", action="store_true")

    pls = sub.add_parser("proposals", help="elenco proposte")
    pls.add_argument("--status", choices=["pending", "notified", "approved", "rejected", "expired", "stale", "executed", "failed"])

    ex = sub.add_parser("execute", help="esegue una proposta APPROVATA")
    ex.add_argument("id")
    ex.add_argument("--dry-run", action="store_true", help="solo controlli, nessun ordine")

    pr = sub.add_parser("pair", help="analisi completa di una coppia")
    pr.add_argument("long", help="SYM:exchange della gamba long, es. CHIP:aster")
    pr.add_argument("short", help="SYM:exchange della gamba short, es. CHIP:hyperliquid")
    pr.add_argument("--notional", type=float)
    pr.add_argument("--horizon", type=float)

    mo = sub.add_parser("monitor", help="controlla posizioni aperte e propone azioni")
    mo.add_argument("--scan", action="store_true", help="aggiorna prima lo scan")
    mo.add_argument("--json", action="store_true")

    jo = sub.add_parser("journal", help="registro delle strategie aperte/chiuse")
    jsub = jo.add_subparsers(dest="action", required=True)
    jo_open = jsub.add_parser("open")
    jo_open.add_argument("long")
    jo_open.add_argument("short")
    jo_open.add_argument("--notional", type=float, required=True)
    jo_open.add_argument("--leverage", type=float)
    jo_open.add_argument("--entry-spread", type=float, help="spread orario all'ingresso")
    jo_open.add_argument("--horizon", type=float)
    jo_open.add_argument("--thesis", default="")
    jo_close = jsub.add_parser("close")
    jo_close.add_argument("id", type=int)
    jo_close.add_argument("--reason", required=True)
    jo_close.add_argument("--realized", type=float)
    jo_list = jsub.add_parser("list")
    jo_list.add_argument("--open", action="store_true")

    ca = sub.add_parser("call", help="chiama un tool MCP grezzo")
    ca.add_argument("tool")
    ca.add_argument("args", nargs="?", help="argomenti JSON")

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
        sys.exit(f"errore MCP: {e}")


if __name__ == "__main__":
    main()
