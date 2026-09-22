# Kalshi-Bot

DEMO-only Kalshi Trade API scaffold for **Bitcoin 15-minute up/down** markets (`KXBTC15M`).
No trading strategy — first slice is client + discover + RSA-PSS auth.

## VPS path

`/home/chupa/Kalshi-Project/Kalshi-Bot`

Develop with Cursor (Remote SSH) or Codex against this directory.

## Hard lock: DEMO only

| Surface | URL |
| --- | --- |
| REST | `https://external-api.demo.kalshi.co/trade-api/v2` |
| WebSocket | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |

`KALSHI_ENV` must be `demo`. Production hosts are rejected in config.

Docs verified against: [Kalshi llms.txt](https://docs.kalshi.com/llms.txt) (API environments, demo env, market data, authenticated requests, SDKs).

> Note: The official `kalshi_python_sync` SDK (3.2.0) currently fails to deserialize live market payloads after Kalshi’s fixed-point dollar fields. This repo uses a thin signed `httpx` client matching the official RSA-PSS scheme so public discover works today.

## Layout

```
src/kalshi_bot/
  config.py      # demo endpoints + env loading
  auth.py        # RSA-PSS request signing
  client.py      # DEMO HTTP client
  discover.py    # KXBTC15M open-market discovery
  cli.py         # discover-btc-15m entrypoint
tests/
  test_smoke_demo.py
.env.example
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

Never commit `.env`, `*.key`, or `*.pem`.

## Run

Discover current open `KXBTC15M` markets (ticker / title / close_time / quotes / floor_strike):

```bash
discover-btc-15m
# or
python -m kalshi_bot.cli
# JSON:
discover-btc-15m --json
```

If credentials are present, the CLI also prints demo portfolio balance. If not, it prints setup steps and still completes public discover.

## Smoke test

```bash
pytest -m "not integration"
pytest -m integration
```

Unit tests cover signing and the demo lock. The integration mark hits the live DEMO public API (series + markets).

## Development flow

Work stays off `main`. One change set per branch, then a PR.

1. Branch from latest `main` (`feature/…` or `cursor/…`).
2. Open a **draft** PR when the change is reviewable.
3. CI runs two jobs: `unit` (required quality bar) and `demo-api` (live Kalshi demo; can flake if the demo host is down).
4. Review the diff. Fix real comments, dismiss noise, push to the same branch. `/autopilot` in Cursor does that loop; it does not merge.
5. **Squash and merge** when `unit` is green. Merge commits and rebases are disabled.
6. GitHub deletes the head branch after merge. A weekly workflow also deletes leftover remote branches that are already merged into `main` and have no open PR.

Do not push demo keys, `.env`, or `keys/*.key`. The repo is public: secret scanning and push protection are on, and `main` requires a PR plus a green `unit` check. Admins can bypass in an emergency. Review is still a step; it is not a required approval.

## Auth model

Authenticated requests send:

- `KALSHI-ACCESS-KEY` — API key ID
- `KALSHI-ACCESS-TIMESTAMP` — unix ms
- `KALSHI-ACCESS-SIGNATURE` — base64 RSA-PSS(SHA-256) over `timestamp + METHOD + path` (path without query)

See [Authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests) and [API Keys](https://docs.kalshi.com/getting_started/api_keys).

## Status

Greenfield scaffold. No orders, no strategy, no Hyperliquid coupling.
