# Test evidence — AVID 0.1.0a2

Everything below was run in the release preparation environment (Linux container, 2 CPUs, no package
index, no systemd user session, no Nobitex key) on 2026-11-02 UTC. Raw outputs are in
[evidence-0.1.0a2/](evidence-0.1.0a2/); nothing here is copied from CI, because CI has not run yet.

## 1. Full suite

353 tests are collected (`python -m pytest`), plus the fuzzers' scale variables.

| run | interpreter | signer backend | result | raw output |
|---|---|---|---|---|
| with `cryptography` 50.0.1 | CPython 3.13.18 | cryptography | **348 passed, 5 skipped** | [full_suite_python3.13_cryptography.txt](evidence-0.1.0a2/full_suite_python3.13_cryptography.txt) |
| `cryptography` blocked | CPython 3.13.18 | pure | **344 passed, 9 skipped** | [full_suite_python3.13_pure.txt](evidence-0.1.0a2/full_suite_python3.13_pure.txt) |
| distro `cryptography` 41.0.7 | CPython 3.12.3 | cryptography | **343 passed, 10 skipped** | [full_suite_python3.12.txt](evidence-0.1.0a2/full_suite_python3.12.txt) |
| distro `cryptography` built for 3.12 (panics on 3.11) | CPython 3.11.18 | pure (broken library detected) | **344 passed, 9 skipped** | [full_suite_python3.11.txt](evidence-0.1.0a2/full_suite_python3.11.txt) |
| — | CPython 3.10 | — | **NOT VERIFIED** (no interpreter here; in the CI matrix) | — |

Every skip, with its reason:

| skipped | count | reason |
|---|---|---|
| `tests/contract/test_nobitex_testnet.py` | 5 (every run) | opt-in testnet contract tests: need `AVID_CONTRACT_TESTNET=1`, `NOBITEX_NETWORK=testnet`, `NOBITEX_SECRETS_DIR` - no testnet key available |
| `tests/test_ed25519.py` library comparisons | 3 (pure, 3.11) | `cryptography` not installed / installed but unusable (PanicException) |
| `tests/test_packaging.py::test_live_extra_resolves_when_cryptography_is_present` | 1 (pure, 3.11) | the `[live]` extra needs a package index when no usable `cryptography` is installed |
| `tests/test_packaging.py` build tests | 5 (3.12) | the 3.12 interpreter here has setuptools 68; building needs ≥ 77 (PEP 639) |

On Python 3.12 and 3.11, pytest 9.1.1 was loaded from a pure-Python copy on `PYTHONPATH` because those
interpreters have no pytest and no package index was reachable.

## 2. By category

| category (marker) | tests | what it covers |
|---|---|---|
| `unit` | 138 | strategy maths, validation, signer, Nobitex client, cloud protocol parsing, outbox, secret files |
| `integration` / `state_machine` | 76 / 23 | full cycles against the fault-injecting fake exchange; agent against a fake cloud |
| `fuzz` / `invariants` | 7 / 4 | random steps with faults; invariants I1-I7, A, B, F checked against the exchange's truth |
| `property` | 21 | seeded property tests (3 seeds × 2 000 cases by default) |
| `failure_injection` / `crash_recovery` | 10 / 10 | disk full, shutdown budget, kill after send, torn log line, leftover temp files |
| `cli` / `simulation` | 47 / 25 | both CLIs, offline simulator, ExchangeAdapter conformance |
| `installer` / `agent` | 16 / 51 | install / uninstall / stop / restart against a stub systemctl, `systemd-analyze verify`, real SIGTERM |
| `packaging` | 6 | sdist + wheel build, clean venv install, editable install, sdist install, `[live]` extra, versions |
| `security` / `regression` | 58 / 44 | one proof per audit finding (F01-F30, PRA) |
| `docs` | 5 | state diagram vs code, command lists, version, links, no superseded documents |
| `testnet` | 5 | opt-in, not run |

Markers overlap (a file can carry two), so the column does not add up to 353.

## 3. Long runs

| run | result | raw output |
|---|---|---|
| engine fuzz, 10 000 random steps × 8 seeds (`AVID_FUZZ_STEPS=10000 AVID_FUZZ_SEEDS=8`) | **8 passed** in 160 s | [fuzz_engine_10000x8.txt](evidence-0.1.0a2/fuzz_engine_10000x8.txt) |
| agent fuzz (hostile cloud), 2 000 steps × 3 seeds | **3 passed** in 57 s | [fuzz_agent_2000.txt](evidence-0.1.0a2/fuzz_agent_2000.txt) |
| property tests, 20 000 cases per seed × 3 seeds | **21 passed** | [property_20000.txt](evidence-0.1.0a2/property_20000.txt) |

## 4. Security proofs and regressions

| run | result | raw output |
|---|---|---|
| `security/test_findings.py` on 0.1.0a2 | **44 passed** | [findings_on_0.1.0a2.txt](evidence-0.1.0a2/findings_on_0.1.0a2.txt) |
| the same proofs against v1.2 (`AVID_SRC=<v1.2>`) | **35 failed, 9 passed** (the defects are real on v1.2) | [findings_on_v1.2.txt](evidence-0.1.0a2/findings_on_v1.2.txt) |
| 0.1.0a2 tests that import on 0.1.0a1, run against 0.1.0a1 | **9 failed, 104 passed** (the PRA regressions) | [pra_regressions_on_0.1.0a1.txt](evidence-0.1.0a2/pra_regressions_on_0.1.0a1.txt) |
| PRA-01 reproduction on 0.1.0a1 | `avid-agent install` / `stop` / `simulate`: invalid choice, exit 2 | [pra01_repro_on_0.1.0a1.txt](evidence-0.1.0a2/pra01_repro_on_0.1.0a1.txt) |

The 9 failures on 0.1.0a1 are: F22 and the testnet-only signer gate (PRA-04), the broken-library and
pure-fallback signer tests (PRA-03), disk-full STOP (PRA-09), the shutdown budget and two send-deadline
tests (PRA-05), and the `signer` / `market_specs` adapter surface (PRA-11). Some of these fail on
0.1.0a1 because the feature is missing (AttributeError / TypeError), not only because of a wrong result;
the behavioural reproductions are PRA-03 (90 failures on Python 3.11 at the baseline) and PRA-09 (STOP
left order 1000 open under disk-full).

## 5. Coverage per area (measured)

Line coverage with the standard library `trace` module (`scripts/coverage-by-area.py`), over the 347
tests of `-m "not slow"` (342 passed, 5 skipped) run in-process on Python 3.13 with `cryptography`. Tests that run AVID in a
subprocess (packaging, the real SIGTERM test, installer wrapper) are not traced, so the numbers are a
lower bound. Executable lines are those of the compiled code objects (docstrings included).

| area | modules | lines | covered | % |
|---|---|---|---|---|
| Risk | risk, control, validation | 264 | 245 | 92.8 |
| Order State | cycle, orderstate, records | 819 | 735 | 89.7 |
| Reconciliation | reconcile | 140 | 131 | 93.6 |
| Ledger and files | store, fsio, events | 351 | 300 | 85.5 |
| Strategy | rebalance, performance | 373 | 296 | 79.4 |
| Exchange Client | client, ed25519 | 469 | 380 | 81.0 |
| Agent | engine, outbox, config, service, `__main__` | 1 619 | 1 302 | 80.4 |
| Cloud Protocol | cloud, directive | 205 | 182 | 88.8 |
| CLI and simulator | cli, sim, models | 794 | 704 | 88.7 |
| **total** | | **5 034** | **4 275** | **84.9** |

Raw: [coverage_by_area.txt](evidence-0.1.0a2/coverage_by_area.txt), per file:
[coverage_by_area.json](evidence-0.1.0a2/coverage_by_area.json). No branch coverage was measured (no
`coverage` package available).

## 6. Static checks

| check | result | raw |
|---|---|---|
| `scripts/check-syntax.py` (Python 3.10 grammar, every `.py`) | 0 errors | [static_checks.txt](evidence-0.1.0a2/static_checks.txt) |
| `python -m compileall` | ok | same |
| `scripts/check-links.py` (relative links and anchors: 227 links in 31 Markdown files) | 0 broken | same |
| `scripts/check-secrets.py .` | 0 findings | same |
| `bash -n scripts/avid-agent` | ok | same |
| YAML of every workflow, issue form and Dependabot file | parses | same |
| Mermaid: 7 diagrams rendered by mermaid-cli 11.16.0 | 7 OK | [mermaid_render.txt](evidence-0.1.0a2/mermaid_render.txt) |
| pinned action SHAs re-resolved with `git ls-remote` | 9 / 9 match | [actions_pins.txt](evidence-0.1.0a2/actions_pins.txt) |
| `systemd-analyze verify` of the generated unit (path with spaces) | ok | in the install transcripts |
| `ruff`, `mypy`, `pip-audit`, CodeQL | **NOT RUN** here (not installable offline); `pip-audit` and CodeQL run in CI | — |

## 7. Not verified

* Nobitex contract tests on the official testnet (no key).
* Python 3.10.
* The GitHub workflows themselves (CI, CodeQL, fuzz, release, dependency review): they have never run.
* The service under a real systemd user manager (stub `systemctl` + `systemd-analyze verify` + a real
  SIGTERM to a real process only).
* AVID Cloud protocol v2 on the cloud side.
* Anything with real money: no test placed an order on any real account.

## 8. How to reproduce

```bash
python -m pytest -q -rs
PYTHONPATH=<dir with a cryptography stub that raises ImportError> python -m pytest -q -rs   # pure signer leg
AVID_FUZZ_STEPS=10000 AVID_FUZZ_SEEDS=8 python -m pytest -v tests/test_fuzz_invariants.py
AVID_AGENT_FUZZ_STEPS=2000 python -m pytest -v tests/test_agent_fuzz.py
AVID_PROPERTY_CASES=20000 python -m pytest -v tests/test_properties.py
AVID_SRC=<other source tree> python -m pytest -q security/test_findings.py
python3 scripts/coverage-by-area.py --json coverage.json
```
