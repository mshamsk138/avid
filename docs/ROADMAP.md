# Roadmap

AVID is an **alpha**. The order below is by risk to the customer's money, not by appeal. Nothing here
is a promise or a date.

## Before anyone should use real money

1. **Exchange contract tests on the official Nobitex testnet** (`tests/contract`): clientOrderId echo
   and lookup, list lag, status words, cancel by clientOrderId. The engine's lost-reply handling rests
   on these assumptions.
2. **AVID Cloud protocol v2** implemented and tested on the cloud side
   ([AGENT_PROTOCOL.md](AGENT_PROTOCOL.md), [audits/CLOUD_REQUIRED_CHANGES_FA.md](audits/CLOUD_REQUIRED_CHANGES_FA.md)).
   Until then the agent keeps every report and places no new order.
3. A supervised testnet run of several days with the agent as a service, including restarts, a
   controlled stop with open orders, and a deliberately broken network.
4. An independent review of the order state machine and the reconciliation by someone who did not
   write them.

## Engineering

* Run the CI matrix on GitHub (Python 3.10-3.13 × cryptography / pure) and keep it green; enable
  CodeQL, Dependabot and secret scanning on the repository.
* Hash-locked test dependencies (`pip install --require-hashes`).
* Timing measurement of the pure-Python signer (it stays refused for live mainnet orders either way).
* A real power-loss test on hardware (today: reasoned about and simulated).
* `markets.json` refresh from the exchange's own market list, with validation and an alert on change.
* Partial fills and queue position in the offline simulator (today: complete fills only).

## Not planned

* Other exchanges, until an adapter with its own contract tests exists ([EXCHANGE_ADAPTER.md](EXCHANGE_ADAPTER.md)).
* Leverage, margin, market orders, withdrawals.
* Price prediction or "profit optimisation": AVID keeps weights; it does not forecast.
