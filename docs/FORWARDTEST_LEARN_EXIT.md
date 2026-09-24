# Protocol — paper forwardtest learned-exit orders

**Status:** vastgezet vóór de run. Niet tussentijds aanpassen om mooiere cijfers te krijgen.
**Dit is geen winsttest.** Succes = schonere labels en een meetbare exit-mix, zonder `learned`-fee-loop.
**Paper only.** Geen Kalshi-orders. Geen Hyperliquid. `record-btc-15m` blijft aan.

## Wat de twee armen doen

Zelfde code, dezelfde JSONL (`data/prod-kxbtc15m.jsonl`), eigen cash, eigen posities, eigen learner (koud bij start). Entries hoeven niet gelijk te zijn; dit is een volledige strategie-vergelijking. De eerdere same-entry counterfactual was alleen diagnose.

| Arm | Directory | Learned-exit orders |
|-----|-----------|---------------------|
| **REF** | `data/paper-sessions/ab-ref/` | aan (`learner.wants_exit` mag sluiten) |
| **NO_LEARN_EXIT** | `data/paper-sessions/ab-nolearn/` | uit |

De vlag is `--learn-exit-orders` / `--no-learn-exit-orders`, of `KALSHI_PAPER_LEARN_EXIT_ORDERS=1|0`. Zonder vlag blijft het oude gedrag (orders aan). Een lopende sessie hervat niet met de andere stand; daarvoor is een nieuwe map nodig.

Bestaande sessies onder `data/paper-sessions/` (sessie B en andere research) niet archiveren of wissen. De AB-mappen zijn submappen en schrijven niet over `data/paper-sessions/current.json`.

## Startstaat (beide armen hetzelfde)

- Bankroll **1000**, target-return **0.50** (alleen KPI, geen voorspelling).
- `initial_params(1000)`: 5 contracten, max open risk 50, mid-edge 0.08, last-minute edge 0.03, maker bias 0.35, cooldown 20s, stop 0.08, take-profit 0.06, flip-margin 0.05.
- Learner koud: gewichten 0, `n = 0`. Tot 4 closes geen `adjust_params` en geen `wants_exit`.
- Urenklok 24. Daarna door tot **minstens 30 closes** (`--min-closes 30` via `--ab`).
- Drawdown-stop **$25 vanaf bankroll** (equity), per arm, alleen op deze AB-start.

## Wat uit staat, en wat aan blijft

**Uit op NO_LEARN_EXIT:** alleen of `learner.wants_exit` een exit-order mag plaatsen.

**Aan op beide armen:**

- training op closes die wél gebeuren (`OnlineLearner.update`; label = teken van in-window of settlement PnL, niet “de markt had ongelijk”)
- `adjust_params` (extra edge, kleinere size, maker-bias)
- rolling tighten/loosen en mark-adapt
- size, cooldown, max open risk
- stop (wacht 2s), signal_flip, take_profit, edge_gone, settlement

Geen minimum-hold. Die zou de entry↔exit-tegenstelling alleen verbergen.

## Stop en succes

Stoppen (per arm) als aan **beide** voldaan is: wall-clock ≥ 24u **en** ≥ 30 closes. Of eerder als drawdown vanaf bankroll > $25. Of bij een code-/capturefout; de recorder blijft dan draaien.

Niet stoppen omdat de PnL er beter uitziet. Minder trades of kleinere size is geen betere voorspelling.

Succes: op NO_LEARN_EXIT geen `learned` exit-orders; exit-mix (stop / signal_flip / take_profit / edge_gone / settlement) en sub-1s-aandeel zijn af te lezen; labels voor de learner komen niet meer uit same-ts learned fee-burn. Geen claim dat de strategie edge heeft.

## Sub-1s en bekende logica (niet in deze run gefixt)

Elke close schrijft `exit_delta` op de intent (en in de view): model_p, bid/ask/mid, edge, seconds_left, BRTI/strike (`gap`), feature-delta’s, welke predicate van false naar true ging, en of die predicate op de entry-quote al waar was.

`seconds_left` is de wall-clock die de paper-sessie nu al gebruikt, niet een aparte exchange-klok.

**Bekende inconsistentie (H1, aparte wijziging):** een maker-entry mag als `model − bid ≥ edge-bar` terwijl `model < 0.5 − flip_margin` (default 0.45). Dan is `signal_flip` op dezelfde quote al waar, zonder nieuwe informatie. Beide armen delen dit. `exit_delta.same_quote_contradiction` en `flip_already_true_at_entry` maken het zichtbaar. Niet “fixen” met een min-hold, en niet in deze run de entry-regel wijzigen.

## Brier

Opgeslagen per gevulde entry in `predictions` (ook in `state.json`). Samenvatting in `metrics.json` → `brier`.

- **Model:** `signal.model_yes` op het entry-moment. Dat is het lokale signaalmodel, niet `OnlineLearner.predict`.
- **Label:** `settlement_yes` (1 als het contract YES settled). Dat is de label van de eerdere Brier ≈ 0.27. Niet het teken van de in-window PnL.
- `learner_p_win` staat erbij als P(in-window winst) en telt niet mee in deze Brier.
- **Benchmarks op dezelfde rijen:** constante 50%, en de markt (YES-mid op entry, anders YES-ask).
- Alleen `settlement_source=official` telt voor het hoofdgetal. Zonder officiële result blijft `n = 0` (reconstructed is fallback en niet de sessie-B vergelijking). Vroege exits blijven in de watch tot close + 3 minuten; daarna is een post-hoc join op de ticker nog nodig.
- 11/22 of een kleine `n` is geen edge-bewijs.

## Metrics

`data/paper-sessions/ab-*/<session_id>/metrics.json` (en `view.metrics`):

net PnL, gross (ruwe prijs, net + fees), fees, closes, contracts, unique 15m-markten, max open, time-in-market, avg hold, sub-1s closes, exit-reason histogram, drawdown vanaf bankroll, peak drawdown, Brier.

## Start (ouder doet dit ná merge)

Recorder en capture niet stoppen. Vanuit de repo-root, `KALSHI_TRADE_ENV=paper`:

```bash
paper-btc-15m --ab --bankroll 1000 --hours 24 --target-return 0.50
```

Zelfde run als twee processen:

```bash
paper-btc-15m --session-dir data/paper-sessions/ab-ref --learn-exit-orders --bankroll 1000 --hours 24 --target-return 0.50 --min-closes 30 --drawdown-stop 25
paper-btc-15m --session-dir data/paper-sessions/ab-nolearn --no-learn-exit-orders --bankroll 1000 --hours 24 --target-return 0.50 --min-closes 30 --drawdown-stop 25
```

Dashboard blijft het bestaande proces: `dashboard-btc-15m` op `127.0.0.1:8787`. Tailscale: `https://chupa.tail9f5972.ts.net:8443`. Geen nieuwe poort. De pagina toont beide armen als `ab-ref` en `ab-nolearn` een `current.json` hebben, plus een eventuele oudere sessie in `data/paper-sessions/`. Per arm: variant, of learn-exit orders aan staan, welke andere invloeden aan blijven, en per close de `exit_reason` (en sub-1s / same-quote als dat zo is).

Hervatten binnen dezelfde map houdt de opgeslagen vlag, bankroll en stopregels. Niet halverwege de knoppen verzetten.
