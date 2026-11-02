# Release readiness — AVID 0.1.0a2

## Decision

**READY WITH DOCUMENTED LIMITATIONS** — for publication as an **alpha** on GitHub (after the owner's
decisions in [PUBLISHING_CHECKLIST.md](../PUBLISHING_CHECKLIST.md) §1).

**Not ready for live trading and not ready for production**: both gates below fail on unmet
prerequisites, not on known defects.

Evidence: [PRE_RELEASE_AUDIT.md](PRE_RELEASE_AUDIT.md), [TEST_EVIDENCE.md](TEST_EVIDENCE.md),
[INSTALL_VERIFICATION.md](INSTALL_VERIFICATION.md).

## Gate 1 — publication of the alpha: PASS (conditional)

| criterion | status | evidence |
|---|---|---|
| full suite passes on the interpreters available (3.11, 3.12, 3.13; with and without `cryptography`) | PASS | TEST_EVIDENCE §1 |
| long fuzz (10 000 × 8) and agent fuzz pass | PASS | TEST_EVIDENCE §3 |
| security proofs pass; they fail on v1.2; PRA regressions fail on 0.1.0a1 | PASS | TEST_EVIDENCE §4 |
| no High or Medium finding open | PASS | PRE_RELEASE_AUDIT §4 |
| wheel and sdist build; clean venv install; both commands work without `PYTHONPATH` | PASS | INSTALL_VERIFICATION |
| no secret in the tree (scanner: 0 findings) | PASS | static_checks.txt |
| license present and unchanged (MIT) | PASS | `LICENSE` |
| documentation complete, links and anchors valid, diagrams render and match the code | PASS | `tests/test_docs.py`, mermaid_render.txt |
| no false claim (badges static and factual; status "alpha, not production-ready" everywhere) | PASS | README, `tests/test_docs.py` |
| copyright holder, contacts, maintainers, repository URL | **PARTIAL**: owner, initial maintainer, repository known; private contact still unavailable | PUBLISHING_CHECKLIST §1 |
| CI, CodeQL, dependency audit run green on GitHub | **NOT VERIFIED** (never run) | PRA-14 |
| Python 3.10 | **NOT VERIFIED** (CI matrix only) | PRA-12 |

The gate passes for the engineering criteria; it remains conditional until a functional private contact
method is configured and the first CI run on GitHub is green. An unqualified contact handle is not an email address.

## Gate 2 — live trading with real money: FAIL

| criterion | status |
|---|---|
| Nobitex contract tests run on the official testnet (clientOrderId echo and lookup, list lag, status words, cancel by clientOrderId) | **NOT VERIFIED** — not run, no testnet key |
| AVID Cloud implements protocol v2 (until then the agent places no new order) | **NOT VERIFIED** — cloud code not available |
| supervised multi-day testnet run of the agent as a service, with restarts and a controlled stop with open orders | **NOT VERIFIED** — not done |
| service under a real systemd user manager | **NOT VERIFIED** — stub + `systemd-analyze verify` + real SIGTERM only |
| live orders refused without a constant-time signer | PASS |
| STOP, dry mode, revocation, disk full still cancel the bot's orders | PASS (tests, fuzz) |

The standalone engine can technically send live orders (`avid-engine run --live`); doing so before the
items above are verified is at the operator's own risk and is not recommended.

## Gate 3 — production: FAIL

Requires gate 2, plus: an independent review of the order state machine and reconciliation, real
power-loss testing, hash-locked dependencies, a monitored deployment, and a support and incident process.
None of these exists for 0.1.0a2.

## What changed since 0.1.0a1, in one line each

CLI and service commands work after `pip install`; offline simulator; broken `cryptography` and full disk
no longer disable STOP; pure signer refused on mainnet; time-bounded stop with an honest verdict; secret
files read through one descriptor; severity on every event; modules split; exchange adapter interface;
CI / release / supply-chain workflows; full documentation. Details: [CHANGELOG.md](../../CHANGELOG.md).
