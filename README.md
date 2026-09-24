# Kalshi-Bot

Kalshi **Bitcoin 15-minute** (`KXBTC15M`) client: demo plumbing, **production WebSocket** for live data, local paper fills. No production orders.

Docs: [docs.kalshi.com](https://docs.kalshi.com/) (environments, demo env, WebSockets, orderbook updates, public trades, CF Benchmarks).

## VPS path

`/home/chupa/Kalshi-Project/Kalshi-Bot`

Develop with Cursor (Remote SSH) or Codex against this directory.

## Environments

| Surface | Host |
| --- | --- |
| Demo REST / WS | `external-api.demo.kalshi.co` / `external-api-ws.demo.kalshi.co` |
| Production REST / WS | `external-api.kalshi.com` / `external-api-ws.kalshi.com` |

- `KALSHI_ENV=demo` — demo client cannot point at production.
- `KALSHI_DATA_ENV=production` — live book, trades, BRTI come from the production WebSocket. REST is only used to find the open window. There is no REST-orderbook fallback.
- `KALSHI_TRADE_ENV=paper` (default). `live` is refused. Demo POST is plumbing only.
- Production POST/PUT/PATCH/DELETE are blocked in code.

Kalshi’s [demo env](https://docs.kalshi.com/getting_started/demo_env) is not a live book. Do not use demo quotes for fair value or EV.

Docs verified against: [Kalshi llms.txt](https://docs.kalshi.com/llms.txt).

> Note: The official `kalshi_python_sync` SDK (3.2.0) currently fails to deserialize live market payloads after Kalshi’s fixed-point dollar fields. This repo uses a thin signed `httpx` client matching the official RSA-PSS scheme so public discover works today.

## Layout

```
src/kalshi_bot/
  config.py      # demo vs production hosts, TRADE/DATA env
  auth.py        # RSA-PSS request signing
  client.py      # demo client + production GET-only client
  ws.py          # signed production WebSocket
  orderbook.py   # snapshot + delta, no gap fill
  record.py      # append-only JSONL
  recorder.py    # 24/7 loop + lock
  paper.py       # local paper ledger
  fees.py        # quadratic taker fee
  discover.py    # KXBTC15M open-market discovery
  cli.py         # discover, record, dashboard, replay, paper-btc-15m
  dashboard/     # local live overview of the JSONL capture and paper session
  replay.py      # last-minute paper hint vs settlement on a JSONL capture
  strategy.py    # signal-edge params and adaptation
  account.py     # fake cash, equity, settlement credit
  session.py     # paper-btc-15m tail of the recorder JSONL
```

## Setup

```bash
cd /home/chupa/Kalshi-Project/Kalshi-Bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### Demo API keys (optional for public discover)

Public market data needs no auth. For `GET /portfolio/balance`:

1. Open [https://demo.kalshi.co/](https://demo.kalshi.co/) and sign up / log in (demo credentials are separate from production).
2. Account settings → **API Keys** → Create New API Key.
3. Save the **Key ID** and the downloaded private key (`.key`) under `keys/` (gitignored) or another path outside the repo.
4. Edit `.env`:

```env
KALSHI_ENV=demo
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/demo-key.key
```

Production recorder keys (required for `record-btc-15m`):

```env
KALSHI_DATA_ENV=production
KALSHI_TRADE_ENV=paper
KALSHI_PROD_API_KEY_ID=your-prod-key-id
KALSHI_PROD_PRIVATE_KEY_PATH=/absolute/path/to/prod-kalshi.key
```

Never commit `.env`, `*.key`, or `*.pem`.

## Run

Discover current open `KXBTC15M` markets on **demo** (ticker / title / close_time / quotes / floor_strike):

```bash
discover-btc-15m
discover-btc-15m --json
```

Record the **production** WebSocket (fails hard without a live key). Writes `data/prod-kxbtc15m.jsonl`. Optional local paper order:

```bash
record-btc-15m --seconds 30
record-btc-15m --paper-style maker --paper-outcome yes --paper-price 0.40 --paper-count 5
```

Local interactive dashboard over that capture (does not place orders):

```bash
dashboard-btc-15m
```

On this host: [http://127.0.0.1:8787](http://127.0.0.1:8787).

On any machine in the Tailscale tailnet (Mac, phone, …):
[https://chupa.tail9f5972.ts.net:8443](https://chupa.tail9f5972.ts.net:8443)
(tailnet only; the existing `https://chupa.tail9f5972.ts.net/` app on :443 is unchanged).

The page follows `data/prod-kxbtc15m.jsonl` live: book, tape, BRTI, countdown, BRTI−strike gap, and a local model P(YES) vs the book after taker fee. Pause, depth, and tape filters stay in the browser. Paper hints are not orders. When a paper session is running, the same page shows equity, cash, realized and unrealized PnL, open marks, in-window exit reasons, the blotter, strategy params, adaptation events, the online learner (sample count and a few weights), and the session countdown. Times on the page are Europe/Amsterdam.

## 24-hour paper session

`paper-btc-15m` is a second process. It does not open the production WebSocket and it does not send orders. Leave `record-btc-15m` and `dashboard-btc-15m` running; the paper process only tails `data/prod-kxbtc15m.jsonl`.

`KXBTC15M` is fee type `quadratic` with multiplier 1 (verified via `GET /series/KXBTC15M`): taker fee `ceil_6dp(0.07 × C × P × (1−P))`, maker fee 0. A yes/no settlement pays $1 per winning contract and charges no settlement fee. The session prefers the official market `result` and falls back to the BRTI compare only after three minutes. An open position can also be sold inside the window: the paper process hits the visible bid on the side it holds (sell YES or sell NO; it does not buy the other outcome to flatten) and pays the same taker fee. Anything still open at expiry settles as before.

The `0.50` target is an aspirational KPI for the progress meter and for risk cuts when equity is behind a linear pace. It is not a forecast and not a promise. A losing close tightens edge, size, stop, and take-profit immediately. A healthy tape can loosen only after four closes, and never by raising size to chase the KPI. An open mark that is down by at least half a percent of bankroll (floor $1) tightens once per position. Separately, an online logistic model updates from each closed trade's entry features (edge, regime, time left, book imbalance, spread, depth, maker vs taker, price vs model). It does not call an external model API. Until four closed trades it only records samples; after that it can demand more edge, cut size, shift maker preference, or exit early. Weights resume from `state.json`. A new session starts cold. Hold time and the exit reason are logged, not used as inputs, because they are unknown when the order is placed.

On the VPS, from the repo root, with `KALSHI_TRADE_ENV=paper`:

```bash
paper-btc-15m --bankroll 1000 --hours 24 --target-return 0.50
```

State lands in `data/paper-sessions/` (gitignored). A lock file stops a second paper process from double-trading. Restarting before `ends_at` resumes the same fake account, including learner weights. The dashboard at [http://127.0.0.1:8787](http://127.0.0.1:8787) picks the session up on its own. The strategy starts from the signal-edge bars (8¢ mid-window, 3¢ in the close minute, 8¢ stop, 6¢ take-profit) and tightens when closes, the open mark, or the learner say so. It does not increase size to chase a deficit.

To pick up this behavior on the VPS, restart only `paper-btc-15m` (leave `record-btc-15m` writing the JSONL). Resume keeps the current session. For a clean bankroll with the same flags, stop the paper process, move `data/paper-sessions/` aside, then start again with `KALSHI_TRADE_ENV=paper`.

Replay the last-minute paper hint against official settlements (local report, no orders):

```bash
replay-btc-15m
replay-btc-15m --json
```

Private delivery board (not public issues): [Kalshi KXBTC15M — Roadmap](https://github.com/users/daffieeee-arch/projects/5). After each shipped step the authoring agent updates that board.

If credentials are present, the CLI also prints demo portfolio balance. If not, it prints setup steps and still completes public discover.

## Smoke test

```bash
pytest -m "not integration"
pytest -m integration
```

Unit tests cover signing and the demo lock. The integration mark hits the live DEMO public API (series + markets).

## Development flow

Work stays off `main`. One change set per branch, then a PR. CI and review run **in parallel**; squash-merge waits for both.

1. Branch from latest `main` (`feature/…` or `cursor/…`).
2. Open a PR, draft is fine. Put `wip` on it if it must stay draft after CI.
3. CI starts immediately: `unit` (merge gate) and `demo-api` (live Kalshi demo; advisory).
4. When `unit` is green and there is no `wip` label, CI marks the PR **ready**. Drafts with `wip` never auto-ready or auto-merge.
5. Independent Bugbot review (read-only). GitHub Actions cannot start Cursor Bugbot; run `/review-bugbot` on the ready PR. Nits do not block.
6. Findings: the authoring agent fixes and pushes. A new push **drops** the `review-passed` label, so review must run again. Stop after two review rounds unless a finding is still merge-blocking.
7. No blocking findings: add the `review-passed` label.
8. Ready + `review-passed` + mergeable + green `unit` → squash-merge and delete the head branch.
9. Update the private GitHub Project: move Now/Next/Later/Done, rewrite the item with the result, add a card only when the plan changed. Do not open public issues for the hypothesis.

Do not skip the label. With zero required human approvals, a green `unit` check alone would otherwise merge without a review. `/autopilot` still only fixes comments and CI; it does not merge.

Keep secrets, production unlocks, and “should we trade this” questions for a human. Do not push demo keys, `.env`, or `keys/*.key`. `main` still requires a PR plus `unit`. Admins can bypass the ruleset in an emergency.

## Auth model

Authenticated requests send:

- `KALSHI-ACCESS-KEY` — API key ID
- `KALSHI-ACCESS-TIMESTAMP` — unix ms
- `KALSHI-ACCESS-SIGNATURE` — base64 RSA-PSS(SHA-256) over `timestamp + METHOD + path` (path without query)

See [Authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests) and [API Keys](https://docs.kalshi.com/getting_started/api_keys).

## Status

Production data + local paper. No production orders. No Hyperliquid coupling.
