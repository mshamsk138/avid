# Testing

No automated test sends an order to a real exchange account. The engine is driven against
`tests/fakes.py:FakeExchange` (a simulated Nobitex that keeps the truth of every order and fill and
injects faults), the offline `avid_core.sim.SimExchange`, and a stub systemd. The only tests that talk
to Nobitex are the opt-in contract tests, and they refuse anything but the official testnet.

## Running

```bash
python -m pip install -r requirements-dev.txt     # pytest, cryptography, setuptools (pinned)
python -m pytest -q                                # everything except the testnet contract tests
python -m pytest -q -m "not slow"                  # without the packaging tests (they build and install)
python -m pytest -q -m security                    # one category
```

Run it twice: with `cryptography` installed and without it (the pure-Python signer path). Do **not**
set `AVID_ALLOW_PURE_ED25519` for the test run.

## Categories

Every test file carries category markers (`conftest.py:CATEGORIES`); select with `-m`.

| marker | files | what it covers |
|---|---|---|
| `unit` | `test_rebalance`, `test_validation`, `test_ed25519`, `test_client`, `test_cloud`, `test_outbox` | pure functions, the Nobitex client against a fake transport, signer, protocol parsing, outbox |
| `state_machine`, `integration` | `test_order_state`, `test_reconciliation`, `test_control_config_cli`, `test_agent` | full cycles against the fake exchange: lost replies, unknown records, cancels, recovery, control file, agent ↔ fake cloud |
| `invariants`, `fuzz` | `test_fuzz_invariants`, `test_agent_fuzz` | thousands of random steps with faults; invariants checked against the exchange's truth |
| `property` | `test_properties` | seeded property-based tests (Decimal grid, Decimal ↔ float, planner bounds, validation, strict JSON, state machine) |
| `failure_injection`, `crash_recovery` | `test_performance_and_ops` | disk full, shutdown budget, kill after send, torn log, installer without sudo |
| `cli`, `simulation` | `test_sim_cli` | `avid-engine` commands, the offline simulator, the ExchangeAdapter surface |
| `installer`, `agent` | `test_service` | `install` / `uninstall` / `stop` / `restart` against a stub systemctl, `systemd-analyze verify` of the unit, a real SIGTERM to a real process |
| `packaging`, `slow` | `test_packaging` | sdist + wheel build, clean virtual environment install, both commands without `PYTHONPATH`, reinstall, uninstall, editable install, `[live]` extra |
| `security`, `regression` | `security/test_findings.py` | one proof per audit finding F01-F30 and PRA findings; they fail on v1.2 and pass now |
| `testnet` | `tests/contract/test_nobitex_testnet.py` | opt-in, official testnet only |

## Invariants checked by the fuzzer

| | invariant (checked after every step against the fake exchange's truth) |
|---|---|
| I1 / B | never two bot orders open at once; no placement while a bot order is open on the exchange |
| I2 | orders created per UTC day ≤ `max_orders_per_day` |
| I3 | filled + still-open notional of the orders created per UTC day ≤ `max_daily_usdt` |
| I4 | no order the bot did not create is ever cancelled |
| I5 | every bot execution is reported exactly once (one event id per trade id) or explicitly flagged `AGGREGATE_ONLY` / `INCOMPLETE_RECONCILIATION` after the history window |
| I6 | with STOP or dry mode no new order is created |
| I7 | every bot order carries a clientOrderId |
| A | the bot's committed notional is never below the truth (nothing released without confirmation) |

The fuzzer drives price moves, spread blow-outs, partial and full fills, foreign orders, lost
placement replies, network errors on every call, listing lag, unrecognised status words, delayed
cancels, STOP and dry/live switches, a clock that goes back, restarts and corrupted or deleted state
files.

Scale: `AVID_FUZZ_STEPS` (default 2500) × `AVID_FUZZ_SEEDS` (default 4); the agent fuzz uses
`AVID_AGENT_FUZZ_STEPS` (default 400). The weekly CI job runs 10 000 × 8. Property tests use `AVID_PROPERTY_CASES`
(default 2000) per seed and print the seed and case of any failure (`AVID_PROPERTY_SEED` replays it).

## Exchange contract tests (testnet)

```bash
AVID_CONTRACT_TESTNET=1 NOBITEX_NETWORK=testnet NOBITEX_SECRETS_DIR=~/.nobitex-testnet \
  python -m pytest -q tests/contract
```

They check clientOrderId echo and lookup, list lag, status words and cancel by clientOrderId on the
**official testnet** and cancel everything they placed. Without the three variables they are skipped
with the reason. Record each run in [audits/TEST_EVIDENCE.md](audits/TEST_EVIDENCE.md). Status for
0.1.0a2: **not run** (no testnet key available).

## Coverage

Coverage is measured with the standard library (`trace`), per area, by
`python3 scripts/coverage-by-area.py` (no `coverage` package needed); numbers for this release are in
[audits/TEST_EVIDENCE.md](audits/TEST_EVIDENCE.md). Line coverage says which lines ran, not that their
behaviour was checked; the invariants and the security proofs are the stronger evidence.

## Writing a test

* A change to the order state machine, the risk checks, clientOrderId handling, the ledger,
  reconciliation, the cloud protocol or STOP needs a regression test that fails before the change.
* Reproduce exchange behaviour with `FakeExchange.faults` (lost reply, network error, list lag,
  unknown status, delayed cancel) instead of mocks of internal functions.
* Never use real credentials; never point a test at a real account.
