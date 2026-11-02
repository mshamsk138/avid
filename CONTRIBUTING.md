# Contributing

Thank you for helping. AVID handles real money, so the bar is high and fixed.

## Rules

1. **Every change to order handling, caps, the order state machine, clientOrderId handling, the
   ledger, reconciliation, the cloud protocol or STOP comes with tests**: one that reproduces the old
   behaviour (it must fail before your change) and one that checks the safe behaviour. Security proofs
   go to `security/test_findings.py`. Existing safety limits are never weakened silently
   ([GOVERNANCE.md](GOVERNANCE.md)).
2. The whole suite passes with and without `cryptography`: `python -m pytest -q`. Do not set
   `AVID_ALLOW_PURE_ED25519` for the test run. Engine changes also pass a fuzz run of at least
   `AVID_FUZZ_STEPS=3000 AVID_FUZZ_SEEDS=8 python -m pytest -q -m fuzz`.
3. No new path may place a second order while a previous one is ambiguous (invariants in
   [docs/TESTING.md](docs/TESTING.md#invariants-checked-by-the-fuzzer)). Fail closed: when unsure, place
   nothing and keep the right to cancel the bot's own orders.
4. Standard library only in `avid_core` and `avid_agent` (`cryptography` stays optional).
5. Never commit credentials, real configuration or runtime data: run
   `python3 -I scripts/check-secrets.py .` before pushing.
6. Never test against a real account. Use `tests/fakes.py`, `avid-engine simulate`, or the official
   testnet.
7. Update the documentation that describes what you changed, the CHANGELOG, and KNOWN_ISSUES.md for
   remaining limitations.

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e . -r requirements-dev.txt
python -m pytest -q
python3 -I scripts/check-syntax.py . && python3 -I scripts/check-links.py .
```

## Pull requests

Use the pull request template: risk impact, tests, compatibility, documentation, security. Small,
traceable changes are easier to review than rewrites. Style: small functions, explicit names, comments
that say *why*; messages to the customer in plain words that say what to do next.

By contributing you agree that your contribution is licensed under the MIT license of this project and
that you follow the [code of conduct](CODE_OF_CONDUCT.md).
