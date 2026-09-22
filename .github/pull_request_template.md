## What
-

## Verify
- [ ] `pytest -m "not integration"`
- [ ] Live demo check if this touches markets or auth (`pytest -m integration` or `discover-btc-15m`)

## Review gate
CI starts on open. After a second Cursor agent reviews the diff (not Bugbot) and finds no blocking issues, add the `review-passed` label. A later push removes that label. Ready + label + green `unit` squash-merges automatically.
