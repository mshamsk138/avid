# Changelog

All notable changes. Versions follow [PEP 440](https://peps.python.org/pep-0440/); tags are `v<version>`.

## [0.1.0a2] — open-source release preparation (unreleased)

Pre-release audit: [docs/audits/PRE_RELEASE_AUDIT.md](docs/audits/PRE_RELEASE_AUDIT.md).

Breaking:
* The pure-Python Ed25519 signer is accepted for live orders **only on the official testnet** with
  `AVID_ALLOW_PURE_ED25519=1` (0.1.0a1 accepted the override on mainnet too), and the gate checks the
  signer that will actually sign (PRA-04).
* `avid-agent status` prints a human-readable summary; the JSON is `avid-agent status --json`.
* `TimeoutStopSec` of the service is 300 s (was 180 s); give containers `stop_grace_period: 300s`.
* `scripts/avid-agent` is a thin wrapper around the Python CLI; install/start/stop/restart/logs moved
  into `avid-agent` itself (same commands, now also after `pip install`).
* Documents renamed: `docs/PROTOCOL.md` -> `docs/AGENT_PROTOCOL.md`, `docs/THREAT_MODEL.md` ->
  `docs/SECURITY_ARCHITECTURE.md`, `docs/STRATEGY_MATH_FA.md` -> `docs/STRATEGY_MATH.md` (English);
  audit documents moved to `docs/audits/`.

Added:
* `avid-engine` commands `simulate` (offline, real engine on stored synthetic prices, bundled examples
  `btc-usdt` and `btc-paxg-usdt`), `status` and `help`; human-readable output and `--json` (PRA-01, PRA-02).
* `avid-agent` commands `install`, `uninstall`, `start`, `stop`, `restart`, `logs`, `help`; the stop
  verdict in `~/.avid-agent/last_stop.json`; exit codes 3 (cancellation not confirmed) and 4 (no report).
* Pre-trade risk checks reported one by one (`risk_checks`); severity (`level`) on every log event
  (PRA-08); typed contracts and the `ExchangeAdapter` protocol (`avid_core/models.py`, PRA-11).
* Tests: property-based (seeded), offline simulator and CLI, service management with a stub systemd
  and a real SIGTERM, packaging (clean venv install of sdist and wheel, editable install); category
  markers.
* CI: Python 3.10-3.13 × cryptography/pure matrix, syntax and link checks, package build and wheel
  smoke test, secret scan, pip-audit, dependency review, CodeQL, weekly long fuzz with artifacts,
  release workflow (draft release, SBOM, checksums, build provenance; manual PyPI Trusted Publishing),
  Dependabot. Every action pinned to a commit SHA.
* Scripts: `build-dist.py`, `make-sbom.py` (CycloneDX 1.5), `check-links.py`, `check-syntax.py`,
  `coverage-by-area.py`, `make-sim-prices.py`.
* Documentation set, Persian README, community files, issue and pull request templates, logo.

Fixed:
* A broken `cryptography` install (pyo3 `PanicException`, a `BaseException`) crashed every cycle,
  including STOP; it is now detected and live orders fail closed (PRA-03).
* A full disk made the agent step fail before the cycle, so STOP could not cancel; the engine now sends
  write-free emergency cancels and the agent survives storage errors (PRA-09).
* The controlled stop could outlast `TimeoutStopSec` and `stop` printed success regardless: the stop is
  time-bounded (exchange send deadline, shutdown budget) and `stop` reports the agent's own verdict
  (PRA-05).
* Secret files were checked and then opened (race): one `O_NOFOLLOW` descriptor is checked and read;
  `NOBITEX_ALLOW_LOOSE_TOKEN_PERMS` relaxes only the read bits (PRA-06, PRA-15).
* `state.json` vanishing between the existence check and the read raised instead of recovering (PRA-07).
* `avid-engine` and `avid-agent install/start/stop` did not work after `pip install` (PRA-01).

Changed:
* `avid_core/cycle.py` split into `records.py`, `reconcile.py`, `store.py` (public names re-exported).
* Package metadata: PEP 639 license fields, `setuptools>=77`, classifiers, `py.typed`, package data;
  User-Agent strings derived from the package version.

## [0.1.0a1] — first open-source alpha (supersedes internal v1.2)

Breaking:
* Agent ↔ cloud **protocol v2** (docs/AGENT_PROTOCOL.md): reports are deleted only on a valid ACK by
  `item_key`; a v1.2 cloud answer is refused (no deletion, no new orders) until the cloud is updated.
* The cloud kill switch is now `data/bot/STOP.cloud`; `STOP` belongs to the customer (v1.2 files are
  migrated automatically).
* `NOBITEX_NETWORK` accepts only empty / `mainnet` / `testnet`.
* `ceilings.json` refuses unknown keys and non-finite or out-of-range values; new optional keys
  `max_spread` (default 0.01) and `min_cash_usdt` (default 0).
* Live orders in API-key mode require the `cryptography` signer (override: `AVID_ALLOW_PURE_ED25519=1`; narrowed to the testnet in 0.1.0a2).
* The installer no longer uses sudo; the command goes to `~/.local/bin`.

Fixes (report AVID-01 … AVID-08 and audit findings F01–F30, see docs/audits/AUDIT_RESPONSE_0.1.0a1_FA.md):
* explicit order state machine; UNKNOWN never releases reservations or allows new orders (AVID-01)
* central validation of every risk number, strict JSON everywhere (AVID-02)
* NAV over all managed assets, removed targets set to 0, incomplete valuation stops trading (AVID-03)
* log cursor by file identity stored with the outbox, three generations, gap detection (AVID-04)
* outbox classes, storage pressure, exact eviction, no silent drop (AVID-05)
* versioned ACK, partial ACK, idempotent resend, audience binding (AVID-06)
* clientOrderId lookup by status, record matching, per-account verification, contract tests (AVID-07)
* per-order reconciliation status and amounts, time-bounded paging, ledger recovery (AVID-08)
* fsync'd atomic writes, submission ledger, fail-closed control file, clock guard with the exchange's
  Date header, market-spec overrides, honest performance basis (report §10)
* state/clock/ceilings/control/directive failures fail closed while STOP keeps working; resolve checks
  the exchange; stale keep-orders flags ignored; other AVID instances on the account detected

Known issues: see KNOWN_ISSUES.md.
