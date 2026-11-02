## What and why

<!-- What does this change, and which issue does it address? -->

## Risk impact

<!-- Does it touch order handling, risk checks / caps, the order state machine, clientOrderId handling, the
ledger, reconciliation, the cloud protocol, STOP / kill switch, credentials or the installer?
If yes: what could go wrong, and how does the change fail closed? -->

- [ ] No effect on orders, limits, financial records or STOP
- [ ] Touches one of them - explained above, with a regression test that fails before this change

## Tests

- [ ] `python -m pytest -q` passes (with and without `cryptography`)
- [ ] engine changes: `AVID_FUZZ_STEPS=3000 AVID_FUZZ_SEEDS=8 python -m pytest -q -m fuzz` passes
- [ ] new behaviour has tests; no test talks to a real exchange account

## Compatibility

<!-- Changes to state.json / ledger / log formats, ceilings.json, the cloud protocol, CLI options or exit codes?
Migration? CHANGELOG entry? -->

## Documentation

- [ ] docs updated (CLI_REFERENCE, RISK_MODEL, ORDER_LIFECYCLE ... as relevant), CHANGELOG and KNOWN_ISSUES

## Security

- [ ] no credentials, real configuration or runtime data committed (`python3 -I scripts/check-secrets.py .`)
- [ ] no existing safety limit is weakened (or it is, explicitly, with the reason above)
