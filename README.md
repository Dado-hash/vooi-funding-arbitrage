# vooi-funding-arbitrage

Toolkit per scegliere e monitorare strategie di funding arbitrage delta-neutral (long su una venue e short sulla stessa underlying su un'altra) tramite il server MCP di VOOI.

L'obiettivo non è l'APR più alto del momento. È il miglior **rendimento netto atteso sull'orizzonte di holding**, tenendo conto di quanto dura lo spread e dei costi di entrata e uscita. Uno spread da 1000% APR che si chiude in 20 minuti non copre le fee.

Il toolkit lo usa soprattutto Claude come analista: Claude esegue gli scan, interpreta i risultati e controlla periodicamente le posizioni. Ogni operazione arriva su Telegram come **proposta con i pulsanti Approva/Rifiuta**. Solo dopo l'approvazione Claude la esegue via MCP con `arb execute`. Senza approvazione non parte nessun ordine.

## Setup

```bash
uv sync
cp .env.example .env   # poi inserisci il token
uv run pytest -q       # test offline su risposte MCP reali salvate
```

`.env` contiene l'endpoint MCP (`https://perps-api.vooi.io/mcp`) e il token di accesso. Per sapere quale header usa il token, guarda la configurazione MCP di Claude Code con `claude mcp get vooi-perps`. Poi imposta `VOOI_MCP_AUTH_HEADER` e `VOOI_MCP_AUTH_SCHEME` di conseguenza. Senza token il server risponde `401 Invalid session id`.

I parametri di default (venue, notional, orizzonte, soglie, fee) stanno in `config.toml`.

## Comandi

| Comando | Cosa fa |
|---|---|
| `uv run arb scan` | Funnel completo e tabella delle migliori coppie. Salva in `data/arb.db` e `data/scans/latest.json` |
| `uv run arb scan --dex --notional 500` | Stesso scan su tutte le venue on-chain, con override dei parametri |
| `uv run arb venues --dex --days 30` | Confronta le combinazioni di venue: universo, spread strutturali su 30 giorni, migliori 3 coppie valide al netto dei costi |
| `uv run arb portfolio --capital 1000 --leverage 2 --max-k 5` | Simula il portafoglio: capitale diviso tra le venue, da 1 a K strategie in parallelo, rendimento in $ e distanza dalla liquidazione |
| `uv run arb plan MET:hyperliquid MET:lighter --notional 450 --leverage 2 [--send]` | Piano d'ordine da eseguire a mano: size identica sulle due gambe (arrotondata ai decimali di entrambe), prima la gamba col book più sottile. `--send` lo manda su Telegram |
| `uv run arb propose open MET:hyperliquid MET:lighter --notional 1000 --reason "..."` | Crea una proposta di apertura e la manda su Telegram con Approva/Rifiuta |
| `uv run arb propose close MET:hyperliquid MET:lighter --reason "..."` | Proposta di chiusura (reduce-only, size prese dalle posizioni) |
| `uv run arb tg wait <id>` | Aspetta il tap sulla proposta (long polling). Da lanciare in background: quando termina, Claude esegue |
| `uv run arb tg poll` | Legge i tap arrivati e aggiorna le proposte |
| `uv run arb execute <id> [--dry-run]` | Esegue una proposta APPROVATA (con `--dry-run` fa solo i controlli) e manda l'esito su Telegram |
| `uv run arb proposals [--status pending]` | Elenco proposte |
| `uv run arb tg send "testo"` / `echo testo \| uv run arb tg send` | Invia un messaggio su Telegram |
| `uv run arb tg chat-id` | Mostra le chat che hanno scritto al bot (per ricavare `TELEGRAM_CHAT_ID`) |
| `uv run arb pair CHIP:aster CHIP:hyperliquid` | Analisi completa di una singola coppia (`SYM:exchange` long, poi short) |
| `uv run arb monitor [--scan]` | Accoppia le posizioni aperte, controlla rischio e spread, propone azioni |
| `uv run arb journal open CHIP:aster CHIP:hyperliquid --notional 1000 --thesis "..."` | Registra una strategia aperta |
| `uv run arb journal close 1 --reason "spread negativo" --realized 4.2` | La chiude |
| `uv run arb journal list [--open]` | Elenco |
| `uv run arb call get_markets '{"assets":["ETH"]}'` | Chiamata grezza a qualsiasi tool MCP |

Aggiungi `--json` a `scan`, `venues` e `monitor` per l'output completo. Flag utili per `scan` e `venues`: `--venues a,b`, `--dex`, `--all-venues`, `--days`, `--horizon`, `--notional`, `--min-hist-apr`, `--deep`.

Un primo scan su tutte le venue DEX richiede circa 3 minuti, perché scarica 30 giorni di storico per circa 740 mercati. Gli scan successivi aggiornano solo le ultime ore.

## Come funziona lo scan

1. **Universo**: `get_funding_strategies` restituisce **tutte** le coppie, filtrate solo per volume, senza soglia sull'APR attuale. Se `both_legs = true`, entrambe le gambe devono stare in `venues`. La UI VOOI fa lo stesso. Il filtro `exchanges` dell'MCP invece accetta una coppia anche se solo una gamba è nella lista.
2. **Storico**: per ogni mercato coinvolto scarica il funding orario con `get_funding_rate_history`, in cache nella tabella SQLite `funding_history`. Lo spread di ogni coppia si calcola in locale come `funding_short − funding_long`: il risultato coincide esattamente con `get_funding_spread_chart`. Così la durata si valuta sull'intero universo con circa N chiamate invece di N². Lo scan precedente invece analizzava solo le coppie con l'APR attuale più alto e, su tutte le venue, finiva per selezionare quasi solo picchi.
3. **Classifica**: entrano solo le coppie con APR medio a 7 giorni ≥ `min_hist_apr`, ordinate per `score` con le fee della configurazione.
4. **Costi reali**: per le prime `deep` coppie:
   - `get_markets` (una chiamata paginata per venue) per prezzo, OI, intervallo e prossimo settlement;
   - `estimate_slippage` per lo slippage e il fill;
   - `get_quotes` per le fee reali del singolo mercato (`feesBps`, fee VOOI incluse).

### Metriche (per ogni coppia)

Spread = `short_rate − long_rate`, orario. I rendimenti sono una frazione del notional di **una** gamba, come nella UI VOOI.

| Campo | Significato |
|---|---|
| `APRnow` | Spread attuale annualizzato (il `netApr` di VOOI, che in realtà è lordo) |
| `APR24h/72h/7d` | Media dello spread sulla finestra, annualizzata |
| `pos72` | Quota di ore con spread > 0 nelle ultime 72h: misura la stabilità |
| `strk` | Ore consecutive con spread positivo fino ad ora |
| `hl` | Half-life: ore perché la deviazione attuale dalla media si dimezzi (da `phi`, autocorrelazione lag-1). Basso = picco effimero |
| `exp` | Funding atteso sull'orizzonte con un modello AR(1) (lo spread parte dal valore attuale e torna verso la media: 50% 72h + 50% 7d), più il P&L di basis atteso |
| `cons` | Scenario pessimista: la media più bassa tra 24h, 72h e 7d tenuta per tutto l'orizzonte, più il P&L di basis pessimista |
| `cost` | Costi round-trip: fee taker × 2 gambe × (entrata + uscita) + slippage × 2 |
| `net` | `(exp + cons)/2 − cost`, cioè il rendimento netto atteso |
| `score` | `net` annualizzato sull'orizzonte. **È la metrica di ranking** |
| `BE` | Ore di holding necessarie per coprire i costi |
| `APR30`/`pos_all` (JSON: `mean_all`, `pos_all`) | Media e quota di ore positive su tutto lo storico (`history_days`, default 30): distingue uno spread strutturale da un regime recente |
| `roi_on_margin` | `net` × leva comune ÷ 2: rendimento sul margine totale delle due gambe |
| `basis_entry` | `(prezzo_short − prezzo_long)/mid` all'ingresso. Positivo = favorevole |
| `basis_mean` | Media storica del basis dagli snapshot di prezzo (`price_snapshots`, uno per scan). Si usa da 24 snapshot in su |

### Basis (differenza di prezzo tra le venue)

Il P&L di prezzo di una coppia è `b₀ − b_uscita`, con b = (prezzo short − prezzo long)/mid:
- **atteso**: se c'è storico, `b₀ − basis_mean` (si torna al livello normale della coppia). Senza storico, `b₀ × 0.5` (convergenza parziale verso zero);
- **pessimista**: un basis sfavorevole converge (si perde), uno favorevole persiste (si guadagna zero).

Ogni scan salva i prezzi di tutti i mercati delle venue configurate, così la stima del basis normale migliora nel tempo. Sulle coppie HL/Lighter liquide il basis d'ingresso vale tipicamente 5-15 bps: poco rispetto al funding settimanale, ma paragonabile alle fee.

### Flag

`storico corto`, `spread attuale negativo`, `regime recente` (positivo meno del 50% delle ore su tutto lo storico), `spread medio 24h negativo`, `instabile` (pos72 < 60%), `picco` (attuale > 3× media 7d), `costi non coperti entro l'orizzonte`, `OI basso`, `slippage alto` (> 15 bps), `book insufficiente`, `settlement imminente` (venue con intervallo di 4h o 8h, prossimo pagamento entro 60 minuti), `orari limitati` (mercato non 24x7: rischio gap), `stessa venue, sottostanti diversi` (es. USBOND contro TLT: non è delta-neutral puro), `basis sfavorevole`.

Flag bloccanti (`is_viable` li esclude): `costi non coperti`, `book insufficiente`, `spread medio 24h negativo`, `instabile`, `analisi costi fallita`.

## Confronto tra venue (`arb venues`)

Raggruppa le coppie per combinazione di venue e analizza a fondo le migliori `--per-pair` di ciascuna. Colonne:
- `univ`: coppie nell'universo;
- `hist`: coppie oltre `min_hist_apr`;
- `struct`: coppie con APR medio ≥ 15% e positive ≥ 75% delle ore su tutto lo storico;
- `sAPR3`: APR medio delle 3 coppie strutturali migliori;
- `valid`: coppie con netto positivo e senza flag bloccanti;
- `top3`: somma del netto delle 3 migliori coppie valide sull'orizzonte (si possono tenere in parallelo);
- `costMed`: costo round-trip mediano.

### Analisi del 2026-10-15 (solo venue on-chain, orizzonte 7 giorni)

| Venue | Top3 a $1000/gamba | Top3 a $5000/gamba | Costo mediano | Note |
|---|---|---|---|---|
| Aster + Hyperliquid | +2.35% | +1.45% | 0.58% → 1.02% | Guidato da RSR e BLUR: book Aster sottili (OI $20-28k, slippage 35-50 bps). Senza queste due resta solo MET (+0.29% a $5k) |
| **Hyperliquid + Lighter** | +1.32% | +1.17% | 0.30% → 0.40% | Fee più basse (4.5 + 2.5 bps), book più profondi, 6 coppie strutturali. Scala meglio |
| Lighter + Ondo | +0.70% | - | 0.22% | Molte coppie valide ma piccole (azioni) |
| Aster + Lighter | +0.09% | +0.07% | 0.63% | Quasi nulla |

Scelta: **Hyperliquid + Lighter**. Aster può servire come terza venue per outlier come RSR (941% APR medio su 30 giorni, positivo nel 94% delle ore), ma solo con size piccole. Hyperliquid compare in tutte le combinazioni migliori.

Lezione dei 30 giorni: molti spread ottimi sulla settimana (MET, BLUR) sono regimi recenti, positivi solo nell'11-32% delle ore su 30 giorni. Le opportunità ruotano: conta avere venue con un universo ampio e costi bassi, più che una singola coppia.

## Portafoglio (`arb portfolio`)

Il capitale è diviso in parti uguali tra le venue. Il notional massimo per venue è `margine × leva × utilizzo` (utilizzo di default 90%, per lasciare un buffer a fee e P&L). Con K strategie ogni gamba vale `notional massimo / K`. Le candidate sono le coppie valide che non hanno i flag `spread attuale negativo`, `stessa venue` o `orari limitati` e la cui leva massima comune è ≥ della leva scelta. Rischio:
- `liqIso`: movimento avverso che liquida una gamba in isolated, `1/leva − 1/(2 × leva massima)`;
- `shockX`: movimento avverso di un solo asset che liquida il conto di una venue in cross margin, con le altre gambe ferme.

Il P&L delle due gambe sta su venue diverse: il guadagno su una non copre la perdita sull'altra senza un trasferimento. Per questo il monitor controlla la distanza dalla liquidazione di ogni gamba.

### Simulazione del 2026-10-15 ($1000, $500 per venue, leva 2x, orizzonte 7 giorni)

| K | $/gamba | Netto atteso | Netto pessimista | ROI 7g | APR sul capitale | Shock singolo asset (cross) |
|---|---|---|---|---|---|---|
| 1 | 900 | $8.35 | $6.96 | 0.83% | 43% | 39% |
| 2 | 450 | $5.59 | $4.40 | 0.56% | 29% | 89% |
| 3 | 300 | $4.35 | $3.36 | 0.44% | 23% | 135% |

K=1 è tutto su MET, un regime recente (positivo solo nel 33% delle ore su 30 giorni). Con K=2 si aggiunge SAMSUNG: rende di meno ma rischia molto meno.

## Monitoraggio

`arb monitor`:
- accoppia le posizioni per asset (toglie i prefissi HIP-3 e i suffissi `USDT`/`USDC`);
- segnala le gambe scoperte;
- controlla la distanza dalla liquidazione (`min_liq_distance`) e lo sbilanciamento del notional (`max_imbalance`);
- calcola lo spread atteso nelle prossime `exit_lookahead_h` ore. Se è peggiore del costo di uscita propone **esci**;
- confronta la posizione con il miglior candidato dell'ultimo scan senza flag. Propone una **rotazione** se il guadagno netto, dopo i costi di uscita e di nuova entrata, supera il hold di `rotate_min_uplift`;
- confronta le posizioni con il journal (posizioni sparite, posizioni non registrate).

## Telegram, approvazioni ed esecuzione

Setup:
1. Su Telegram crea un bot con @BotFather e copia il token in `.env` come `TELEGRAM_BOT_TOKEN`.
2. Scrivi un messaggio qualsiasi al bot, poi esegui `uv run arb tg chat-id` e copia l'id in `.env` come `TELEGRAM_CHAT_ID`.
3. Prova con `uv run arb tg send "test"`.
4. Collega Hyperliquid e Lighter nell'app VOOI ("Connect" ed "Enable trading", che include l'approvazione del builder).

### Flusso di una proposta

1. Claude crea la proposta con `arb propose`. I parametri esatti degli ordini restano in SQLite e la proposta scade dopo `proposal_ttl_min` (15 minuti).
2. Su Telegram arriva il riepilogo con ✅ Approva e ❌ Rifiuta.
3. Claude lancia `arb tg wait <id>` in background. Il tuo tap chiude il processo e lo risveglia.
4. Se hai approvato, Claude esegue `arb execute <id>`.
5. L'esito arriva su Telegram.

### Sicurezza

- **Mittente**: contano solo i tap del tuo `TELEGRAM_CHAT_ID`. Gli altri ricevono "Non autorizzato".
- **Solo pulsanti**: i messaggi di testo vengono ignorati e non sono mai istruzioni.
- **Validità**: un'approvazione vale per una sola proposta, solo prima della scadenza, e la proposta si esegue una volta sola.
- **Limiti indipendenti**: `arb execute` ricontrolla `[execution]` in `config.toml` (venue ammesse, notional massimo per gamba, leva massima) qualunque cosa contenga la proposta.
- **Condizioni di mercato**: prima di eseguire ricontrolla il prezzo (scostamento massimo 1%) e, per le aperture, che lo spread di funding sia ancora positivo e che il basis non sia peggiorato di oltre 20 bps. Se qualcosa non torna, la proposta passa a `stale` e Claude ne manda una nuova.

### Esecuzione

- **Leva**: viene impostata su entrambe le gambe.
- **Gambe**: una alla volta, ordini market, prima il book più sottile. Dopo ogni ordine `get_positions` verifica la size.
- **Seconda gamba fallita**: la prima viene chiusa subito (reduce-only), per non restare mai senza copertura.
- **Chiusure**: sempre reduce-only.
- **Journal**: le aperture riuscite vengono registrate automaticamente.

**Da fare al primo utilizzo reale**: il formato di `get_positions` non è ancora stato verificato con posizioni vere. Il primo trade va fatto con una size minima (per esempio $20 per gamba), per validare la verifica delle posizioni prima di usare il notional pieno.

### Loop

`.claude/settings.local.json` consente `uv run arb *` senza conferma in Claude Code, così il loop non si blocca. Gli ordini restano legati al tap su Telegram e ai limiti di `arb execute`.

In una sessione Claude Code aperta nella cartella del progetto (funziona finché la sessione resta aperta e il Mac acceso):

```
/loop 1h esegui il ciclo di monitoraggio del playbook nel README
```

Messaggi, per evitare spam:
- **Subito**: proposta con pulsanti per ogni `[AZIONE]` del monitor (uscita, ribilanciamento, gamba senza l'altra) e per ogni nuova opportunità o rotazione sopra le soglie del playbook.
- **Subito**: un messaggio senza pulsanti se la situazione richiede un intervento manuale (per esempio una chiusura di sicurezza fallita).
- **Una volta al giorno** (primo ciclo dopo le 9:00): riepilogo con posizioni, funding incassato, spread attuali e prime 3 opportunità.
- Altrimenti nessun messaggio.

## Playbook dell'analista (Claude)

1. Esegui `arb scan` (o `--json` per i dettagli). Scarta le coppie con flag bloccanti: `costi non coperti`, `book insufficiente`, `spread medio 24h negativo`.
2. Fra le rimanenti preferisci, in quest'ordine: `score` alto, `pos72` ≥ 80%, `BE` ben sotto l'orizzonte, half-life lunga, OI e volume adeguati al notional.
3. Diffida dell'`APRnow` molto sopra l'`APR7d`. Di solito è un picco: il modello lo sconta già, ma va detto all'utente.
4. Per le 2-3 migliori esegui `arb pair` e, se serve, guarda `get_funding_rate_history` delle singole gambe: chi guida lo spread? È un funding negativo strutturale o un evento?
5. Dimensiona con `arb portfolio` (capitale attuale: $1000, cioè $500 di margine per venue con leva 2x, fino a $1000 di notional per venue). I mercati HIP-3 (`xyz:`, `mkts:`, `io:`) su Hyperliquid accettano solo isolated margin.
6. Proponi all'utente coppia, direzione, notional, leva, rendimento atteso e pessimista, break-even e rischi. Allega il piano di `arb plan`.
7. Ogni operazione passa da `arb propose`, poi `arb tg wait <id>` in background, poi `arb execute <id>` solo se approvata. **Mai** chiamare direttamente i tool di scrittura dell'MCP (`create_order`, `set_leverage`, trasferimenti, ...). Mai trasferimenti né prelievi.
8. Ciclo di monitoraggio (loop orario):
   - `arb tg poll` (tap arrivati mentre il loop dormiva) ed esecuzione delle proposte approvate;
   - `arb monitor --scan`;
   - confronta il report con le posizioni e con il journal;
   - decidi se tenere, uscire, ribilanciare o ruotare;
   - crea le proposte e i messaggi secondo le regole della sezione "Telegram, approvazioni ed esecuzione".

   Un'uscita o una rotazione si propone solo se il netto atteso migliora dopo i costi di uscita e di nuova entrata.

## Limiti noti

- Le fee in `config.toml` vengono da `get_quotes` senza exchange collegati. Con un account collegato il tier potrebbe essere diverso: lo stadio 3 usa comunque `get_quotes`.
- Il formato di `get_positions` non è ancora stato verificato con posizioni reali: al momento non c'è nessun exchange collegato. `normalize_position` prova più nomi di campo. Va validato alla prima posizione aperta.
- Lo slippage di uscita è stimato uguale a quello di entrata. Il basis non entra nello score.
- Lo storico dello spread sulle venue con funding a 4h/8h è una funzione a gradini: `phi` risulta più alto.
- I costi assumono ordini taker, per scelta: niente ordini limit, per non rischiare che venga eseguita una sola gamba.
- Non sono inclusi i costi di bridge e trasferimento del collaterale tra venue.
- Con `--all-venues` il primo scan scarica lo storico di qualche migliaio di mercati (diversi minuti).
- Su Aster alcuni asset hanno due mercati (`XAG/USDT` e `XAG/USD1`). Lo scan risolve l'ambiguità passando la coppia completa (`XAGUSDT`). Esistono anche coppie con entrambe le gambe su Aster (USDT contro USD1).

## Changelog

- **2026-10-15**: permesso di progetto per `uv run arb *` (loop senza conferme in Claude Code).
- **2026-10-15**: approvazione via Telegram ed esecuzione via MCP. Nuovi comandi: `arb propose` (proposta con pulsanti), `arb tg wait`/`poll`, `arb execute` (limiti `[execution]`, controllo dello scostamento, esecuzione a gambe con chiusura di sicurezza), `arb proposals`. Notional di default $1000 per gamba ($500 di margine × 2x).
- **2026-10-15**: niente esecuzione automatica: Claude analizza e manda su Telegram cosa fare, l'utente esegue su VOOI. Nuovi comandi `arb plan` (piano d'ordine con size arrotondate e ordine delle gambe) e `arb tg` (invio Telegram).
- **2026-10-15**: basis tra venue nel modello (atteso e pessimista) e snapshot dei prezzi a ogni scan per stimarne la media storica. Nuovo comando `arb portfolio` (capitale, leva, K strategie, rischio di liquidazione).
- **2026-10-15**: config impostata su Hyperliquid + Lighter, $100 per gamba. Nuovi flag: `spread attuale negativo`, `orari limitati`, `stessa venue, sottostanti diversi`.
- **2026-10-15**: scan riscritto. Storico per mercato in cache, spread di coppia calcolato in locale: niente più bias verso i picchi. Fee reali per mercato da `get_quotes` (Aster 6 bps, non 3.5). Storico di default a 30 giorni con `mean_all`/`pos_all` e flag `regime recente`. Nuovo comando `arb venues`, flag `--dex` e `--days`. Rimossi `min_apr` e `max_candidates`, sostituiti da `min_hist_apr`. Analisi delle venue on-chain: scelta Hyperliquid + Lighter.
- **2026-10-15**: primo scan reale riuscito (client HTTP con token verificato, circa 25s per scan). Risolti gli asset ambigui su Aster (prima 7 coppie su XAU/XAG/CL venivano scartate). Orizzonte di default portato a 168h: a 72h quasi nessuna coppia HL/Aster copre i costi taker.
- **2026-10-15**: prima versione. Client MCP HTTP, funnel di scan con metriche di durata e costi, monitor delle posizioni, journal SQLite, test offline su risposte reali (CHIP aster/hyperliquid).
