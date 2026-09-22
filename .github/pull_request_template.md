## What
-

## Verify
- [ ] `pytest -m "not integration"`
- [ ] Live demo check if this touches markets or auth (`pytest -m integration` or `discover-btc-15m`)

## Review gate
CI starts on open. Green `unit` marks a draft ready unless it has `wip`. Then run `/review-bugbot` (reviewer only). After a clean review, add `review-passed`. A later push removes that label. Ready + label + green `unit` squash-merges automatically.
