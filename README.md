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
  cli.py         # discover-btc-15m + record-btc-15m
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
