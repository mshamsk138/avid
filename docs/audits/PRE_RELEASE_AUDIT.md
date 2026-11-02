# Pre-release audit — AVID 0.1.0a2

Audit of the 0.1.0a1 code base before its open-source release as 0.1.0a2. Every finding below was
either **reproduced** (a command or test shows the defect on 0.1.0a1) or is marked **analytical** (shown
by reading the code and calculating, not by running it). Hypothetical risks that were not shown to be
reachable are listed separately at the end. Raw outputs: [evidence-0.1.0a2/](evidence-0.1.0a2/).

## 1. Scope and method

| | |
|---|---|
| input | AVID 0.1.0a1: `avid_core`, `agent/avid_agent`, `scripts/`, tests, docs, `.github/workflows/ci.yml` |
| not in scope | AVID Cloud (separate code base, not available), the Nobitex service itself |
| baseline run | full suite on Python 3.13 / 3.12 / 3.11, engine fuzz 10 000 × 8, agent fuzz 2000 × 3, probes for disk-full and a broken `cryptography` ([baseline](evidence-0.1.0a2/baseline/)) |
| tools available | CPython 3.11.18, 3.12.3, 3.13.18; pytest 9.1.1; setuptools 84; `systemd-analyze` 255 (no running user manager); mermaid-cli |
| tools not available | package index (PyPI blocked by the environment), `ruff`, `mypy`, `pip-audit`, CodeQL, a Python 3.10 interpreter, a systemd user session, a Nobitex testnet key |
| rules applied | fail closed but keep the right to cancel; no test against a real account; regression test for every change to orders, risk, clientOrderId, ledger, reconciliation, cloud protocol, STOP; no unrun result claimed |

## 2. Inventory (0.1.0a2)

| area | files | lines |
|---|---|---|
| engine | `avid_core/cycle.py` 875, `store.py` 300, `records.py` 127, `reconcile.py` 170, `orderstate.py` 88, `risk.py` 60, `rebalance.py` 322, `control.py` 160, `validation.py` 151, `events.py` 32, `fsio.py` 128 | 2 413 |
| exchange | `client.py` 449, `ed25519.py` 184, `models.py` 127 | 760 |
| CLI / simulator | `cli.py` 405, `sim.py` 461, `examples/*.json`, `performance.py` 181 | 1 047 |
| agent | `engine.py` 715, `outbox.py` 367, `__main__.py` 369, `service.py` 385, `config.py` 207, `cloud.py` 188, `directive.py` 86 | 2 317 |
| scripts | `avid-agent` (wrapper), `check-secrets.py`, `check-links.py`, `check-syntax.py`, `build-dist.py`, `make-sbom.py`, `coverage-by-area.py`, `make-sim-prices.py`, `verify-install.py` | 652 |
| tests | 21 test files + `fakes.py`, 353 tests | see [TEST_EVIDENCE.md](TEST_EVIDENCE.md) |

Entry points: `avid-engine` -> `avid_core.cli:main`; `avid-agent` -> `avid_agent.__main__:main`;
`python -m avid_core.cycle` (kept, delegates to the CLI). External interfaces: Nobitex REST
(`apiv2.nobitex.ir`, `testnetapiv2.nobitex.ir`), AVID Cloud `/agent/v1/pair` and `/agent/v1/sync`,
systemd `--user`, journald. Files written: see [ARCHITECTURE.md](../ARCHITECTURE.md#files-of-one-engine-folder-rootdatabot-mode-0700-files-0600).
Environment variables: `NOBITEX_SECRETS_DIR`, `NOBITEX_NETWORK`, `NOBITEX_ALLOW_LOOSE_TOKEN_PERMS`,
`AVID_AGENT_HOME`, `AVID_ALLOW_PURE_ED25519`, test-only `AVID_*` scale variables.

## 3. Flow traces

Each critical flow was followed through the code and tied to the test that exercises it.

| # | flow | path through the code | safety property | exercised by |
|---|---|---|---|---|
| T1 | placement | `_cycle` step 9-10 -> `risk.pre_trade_checks` -> `ost.transition(SUBMISSION_PENDING)` + `ledger_append(submit)` + `store.save` -> `client.add_limit_order` -> `OPEN` | intent durable before the send; caps counted before | `test_order_state.py`, fuzz I1-I3 |
| T2 | lost reply | `add_limit_order` raises `NETWORK` -> `pending` kept -> next cycle `_lookup_pending` (open list, status by cid, list all) -> `_matches_pending` -> adopt | never re-sent, matched only by clientOrderId and equal fields | `test_crash_right_after_sending_is_recovered_by_client_order_id`, F01, fuzz I1 |
| T3 | definite refusal | `_definitely_rejected` -> `_rejected` -> `REJECTED_CONFIRMED`, caps released | only a definite "no" releases | `test_order_state.py`, F07 |
| T4 | cancel and final record | `_cancel_and_settle` -> `CANCEL_REQUESTED` -> left the list -> `_finalize` -> `order_numbers` -> `*_CONFIRMED`, refund of the unfilled part once | a request is not a cancellation; settle once | `test_order_state.py`, fuzz A |
| T5 | unreadable record | `_finalize` with unknown status or inconsistent amounts -> `UNKNOWN`, alert after 6 errors | nothing released, no new order | F08 F08b, `test_order_state.py` |
| T6 | STOP | `store.stop_requested` -> cancel own (incl. UNKNOWN, pending by cid) -> `stopped` | no new order, own orders cancelled, foreign never | fuzz I4 I6, F02 |
| T7 | state loss | `Store.load` corrupt/missing -> `_recover_from_ledger` -> day lock | counters never reset, unsettled orders tracked | F09 F17 F18, `test_fsio_and_events.py` |
| T8 | cloud sync | `Agent._sync` -> `CloudClient.sync` -> `parse_sync_response` (v2, ACK by key) -> `outbox` delete accepted only -> `directive.validate` -> `_apply` (min/max with ceilings) | an HTTP 200 is not an ACK; the cloud only tightens | F06 F10 F12 F20 F21 F28-F30, agent fuzz |
| T9 | controlled stop | SIGTERM -> `serve.on_signal` (stop flag, exchange deadline 60 s) -> `run_loop` -> `on_stop` -> `safe_shutdown` (budget 150 s, final sync ≤ 20 s) -> `last_stop.json` -> `service.stop` verdict | bounded; never reports success it did not confirm | `test_service.py` (real SIGTERM), `test_shutdown_respects_its_time_budget` |
| T10 | install / service | `service.install` (root check, import check, manager check, write + backup, daemon-reload or restore) -> `start` -> `stop` / `restart` (keep flag) -> `uninstall` (keeps data) | no root; idempotent; data never deleted | `test_service.py`, `test_installer_runs_without_sudo_into_the_users_path` |
| T11 | offline simulation | `cli.cmd_simulate` -> `sim.run` -> `cycle.cycle(SimExchange)` in a private folder | never touches the network, credentials or a real engine folder | `test_sim_cli.py` |

## 4. Findings

Severity: **High** = can lead to unwanted exposure or an unstoppable order; **Medium** = weakens a
safety property under plausible conditions; **Low** = robustness / hygiene; **Info** = structure.

| ID | severity | title | kind | status |
|---|---|---|---|---|
| PRA-01 | High | commands and installer inconsistent after `pip install` | reproduced | fixed |
| PRA-02 | Medium | no offline simulation separate from the live dry run | reproduced (missing feature) | fixed |
| PRA-03 | High | a broken `cryptography` install crashes every cycle, STOP included | reproduced | fixed |
| PRA-04 | Medium | pure-Python signer override honoured on mainnet; gate keyed on auth mode | reproduced | fixed |
| PRA-05 | Medium | controlled stop can outlast `TimeoutStopSec`; `stop` reports success regardless | analytical | fixed |
| PRA-06 | Low | secret files checked by path, then opened (race) | analytical | fixed |
| PRA-07 | Low | `state.json` vanishing between check and read raises | analytical | fixed |
| PRA-08 | Low | no uniform severity on log events | reproduced | fixed |
| PRA-09 | Medium | a full disk disables STOP cancellation | reproduced | fixed |
| PRA-10 | Info | `cycle.py` 1457 lines with mixed responsibilities | inspection | fixed |
| PRA-11 | Info | no explicit exchange adapter interface | inspection | fixed |
| PRA-12 | Low | Python 3.10 declared but never run | inspection | partly fixed; NOT VERIFIED |
| PRA-13 | Low | README and docs did not match the code | inspection | fixed |
| PRA-14 | Low | CI gaps (no build/install test, no audit, no CodeQL, masking env var) | inspection | fixed in files; NOT VERIFIED |
| PRA-15 | Info | `NOBITEX_ALLOW_LOOSE_TOKEN_PERMS` relaxed more than the read bits | inspection | fixed |
| PRA-16 | Low | copyright holder, contacts and maintainers unconfirmed | inspection | open (owner decision) |
| PRA-17 | Info | Decimal -> float -> decimal text boundary of order amounts | analytical | verified, no defect |

### PRA-01 — commands and installer inconsistent after `pip install` (High)

* **Where**: `pyproject.toml` `[project.scripts]` (`avid-engine = avid_core.cycle:main`, commands
  check/plan/run only); `scripts/avid-agent` (bash) implemented `install/start/stop/restart/logs`; the
  Python entry point `avid_agent.__main__:main` did not; the unit's `ExecStart="$SELF" run` pointed at
  the checkout script.
* **Precondition**: AVID installed with pip (the documented `pip install .`).
* **Scenario**: `avid-agent install` or `avid-agent stop` -> `invalid choice` (exit 2).
* **Impact**: no supported controlled stop or service management for pip users; operators fall back to
  `systemctl stop` without the verdict, or leave the agent running.
* **Reproduction**: [pra01_repro_on_0.1.0a1.txt](evidence-0.1.0a2/pra01_repro_on_0.1.0a1.txt).
* **Fix**: `avid_core/cli.py` (check, plan, simulate, run, status, help) as the `avid-engine` entry
  point; `avid_agent/service.py` + `__main__.py` (install, uninstall, start, stop, restart, logs, help);
  the unit runs `<python> -m avid_agent run`, with `PYTHONPATH` only for a checkout; `scripts/avid-agent`
  is a thin wrapper of the same CLI.
* **Acceptance**: `tests/test_packaging.py::test_clean_venv_install_runs_both_commands_without_pythonpath`,
  `tests/test_service.py`, `tests/test_sim_cli.py::test_cli_help_version_and_usage_errors`,
  `tests/test_docs.py::test_cli_reference_lists_every_command`.
* **Status**: fixed; verified locally (stub systemd); real systemd user manager NOT VERIFIED.

### PRA-02 — no offline simulation separate from the live dry run (Medium)

* **Where**: `avid_core.cycle.main` offered `plan` (needs credentials and the real account) and
  `run` (dry run against the real account) only.
* **Scenario**: a new user can only see the engine decide by giving it real credentials.
* **Impact**: trying the software requires exposing a real API key; "simulation" and "live dry run"
  were easy to confuse.
* **Reproduction**: same evidence file (`simulate` -> `invalid choice`).
* **Fix**: `avid_core/sim.py` (`SimExchange`, strict scenario files, runner on the real
  `cycle.cycle`), bundled examples, `avid-engine simulate`; a `--root` holding real engine data is
  refused; results labelled SIMULATION / estimated vs realized.
* **Acceptance**: `tests/test_sim_cli.py` (reproducible, offline, refuses a real folder, limits hold
  during the run, invalid scenarios refused).
* **Status**: fixed, verified.

### PRA-03 — a broken `cryptography` install crashes every cycle, STOP included (High)

* **Where**: `avid_core/ed25519.py:_lib()` caught `ImportError` only.
* **Precondition**: a `cryptography` build for another Python (e.g. the distro package of Python 3.12
  used by Python 3.11): importing it raises `pyo3_runtime.PanicException`, a `BaseException`.
* **Scenario**: any call to `ed25519.backend()` / `sign()` raises past every `except Exception`.
* **Impact**: every engine cycle and agent step fails, so STOP cannot cancel the bot's orders.
* **Reproduction**: baseline suite on Python 3.11: 90 failed
  ([baseline/full_suite_python3.11.txt](evidence-0.1.0a2/baseline/full_suite_python3.11.txt),
  [baseline/probe_broken_cryptography_py311.txt](evidence-0.1.0a2/baseline/probe_broken_cryptography_py311.txt)).
* **Fix**: `_lib()` records any `BaseException` except `KeyboardInterrupt` / `SystemExit` as a broken
  backend; `backend()` falls back to `pure` with `backend_error()`; live API-key orders are refused
  with that reason.
* **Acceptance**: `tests/test_ed25519.py::test_broken_cryptography_install_fails_closed`; full suite on
  Python 3.11 with the broken distro package ([full_suite_python3.11.txt](evidence-0.1.0a2/full_suite_python3.11.txt)).
* **Status**: fixed, verified.

### PRA-04 — pure-Python signer override honoured on mainnet (Medium)

* **Where**: `cycle._cycle` signer gate: `auth_mode == 'api_key' and backend() == 'pure' and not
  AVID_ALLOW_PURE_ED25519`.
* **Scenario**: `AVID_ALLOW_PURE_ED25519=1` left set (it was set in CI) -> live mainnet orders signed by
  a non-constant-time implementation; a client whose signer differs from the global backend was not
  considered.
* **Impact**: possible timing side channel on the signing key (hypothetical in practice, see H1), and a
  documented "simulation-only" override that was not limited to simulation.
* **Reproduction**: `security/test_findings.py::test_F22_live_orders_need_a_constant_time_signer` and
  `tests/test_order_state.py::test_pure_signer_allowed_only_on_testnet_with_explicit_opt_in` fail on
  0.1.0a1 ([pra_regressions_on_0.1.0a1.txt](evidence-0.1.0a2/pra_regressions_on_0.1.0a1.txt)).
* **Fix**: gate on `client.signer`; `ed25519.pure_allowed(network)` is true only for `testnet` with the
  explicit opt-in; CI no longer sets the variable.
* **Status**: fixed, verified.

### PRA-05 — controlled stop can outlast `TimeoutStopSec`; `stop` reports success regardless (Medium)

* **Where**: `Agent.safe_shutdown` (3 attempts, each several HTTP calls with a 15 s timeout and read
  retries, then a cloud sync with a 20 s timeout); unit `TimeoutStopSec=180`; bash `stop` printed
  "agent stopped" whatever happened.
* **Scenario (analytical)**: a slow exchange during shutdown: worst case > 180 s -> SIGKILL in the middle
  of cancelling; the operator is told the agent stopped.
* **Impact**: bot orders may stay open while the operator believes they were cancelled.
* **Fix**: exchange send deadline (`NOT_SENT` before sending once passed, no retry sleep past it):
  60 s drain on SIGTERM, 150 s budget in `safe_shutdown`, final report ≤ 20 s; `TimeoutStopSec=300`;
  the agent writes its verdict to `last_stop.json`; `avid-agent stop` exits 0 / 3 / 4 from that verdict.
* **Acceptance**: `tests/test_client.py::test_deadline_stops_sending_and_says_not_sent`,
  `::test_no_retry_sleep_past_the_deadline`, `tests/test_performance_and_ops.py::test_shutdown_respects_its_time_budget`,
  `tests/test_service.py::test_stop_reports_only_what_the_agent_confirmed`,
  `::test_sigterm_is_a_controlled_stop_in_a_real_process`.
* **Status**: fixed; verified with a real process and a stub systemd; behaviour under a real systemd
  stop NOT VERIFIED.

### PRA-06 — secret files checked by path, then opened (Low)

* **Where**: `client._read_secret_file`, `config._private_file`.
* **Precondition**: an attacker who can replace files in `~/.nobitex` between the check and the open
  (in practice the same user, which is already out of scope).
* **Fix**: `fsio.read_private`: one `O_RDONLY | O_NOFOLLOW | O_CLOEXEC` descriptor, `fstat` checks
  (regular file, owner, no group/other write, no group/other read unless allowed), then read from it.
* **Acceptance**: `tests/test_fsio_and_events.py::test_check_and_read_use_one_descriptor` and the
  permission tests there.
* **Status**: fixed, verified.

### PRA-07 — `state.json` vanishing between check and read raises (Low)

* **Where**: `Store.load` (`exists()` then `read_text()`).
* **Impact**: one failed cycle (no unsafe action); recovery happens next cycle.
* **Fix**: read first, `FileNotFoundError` -> missing state (ledger recovery when a ledger exists).
* **Acceptance**: `tests/test_fsio_and_events.py::test_state_file_vanishing_mid_read_is_a_missing_state`.
* **Status**: fixed, verified.

### PRA-08 — no uniform severity on log events (Low)

* **Where**: `Store.log` wrote `level` only when a caller passed one; the agent kept its own event sets.
* **Impact**: dashboards and operators could not filter errors reliably.
* **Fix**: `avid_core/events.py` (one table), `Store.log` / `PreviewStore.log` always write `level`.
* **Acceptance**: `tests/test_fsio_and_events.py::test_every_logged_event_has_a_severity`.
* **Status**: fixed, verified.

### PRA-09 — a full disk disables STOP cancellation (Medium)

* **Where**: `Agent.step` -> `_sync` -> `_save_state` raised `OSError` before the engine cycle;
  `cycle._cycle` writes state before cancelling.
* **Scenario**: disk full + STOP -> the step raises, the bot's open order is never cancelled.
* **Reproduction**: [baseline/probe_disk_full.txt](evidence-0.1.0a2/baseline/probe_disk_full.txt)
  ("open bot orders after STOP under disk-full: [1000]"); `test_disk_full_still_lets_stop_cancel_and_changes_nothing`
  fails on 0.1.0a1.
* **Fix**: `cycle.cycle` catches `OSError` and, when cancelling is wanted (STOP, dry, invalid control),
  calls `emergency_cancel` (cancel requests, no writes); the agent's state save and logging survive
  storage errors and hold new orders.
* **Acceptance**: `tests/test_performance_and_ops.py::test_disk_full_still_lets_stop_cancel_and_changes_nothing`,
  `::test_disk_full_never_sends_an_order`.
* **Status**: fixed, verified.

### PRA-10 — `cycle.py` 1457 lines with mixed responsibilities (Info)

* **Fix**: split into `records.py` (record interpretation), `reconcile.py`, `store.py`; `cycle.py` keeps
  the cycle (875 lines) and re-exports the previous public names. No behaviour change: the whole suite,
  the security proofs and the fuzzers pass unchanged.
* **Status**: fixed, verified.

### PRA-11 — no explicit exchange adapter interface (Info)

* **Fix**: `avid_core/models.py` (`ExchangeAdapter` protocol, typed contracts),
  [EXCHANGE_ADAPTER.md](../EXCHANGE_ADAPTER.md); conformance test for `NobitexClient`, `SimExchange`
  and the test fake.
* **Status**: fixed, verified.

### PRA-12 — Python 3.10 declared but never run (Low)

* **Fix**: `scripts/check-syntax.py` parses every file with the 3.10 grammar (passes); the CI matrix
  includes 3.10. Python 3.11 (same f-string grammar as 3.10) passes the whole suite locally.
* **Status**: partly fixed; running the suite on 3.10 is **NOT VERIFIED** until CI runs on GitHub.

### PRA-13 — README and docs did not match the code (Low)

* **Fix**: documentation rewritten against the code; `tests/test_docs.py` checks the order state
  diagram against `orderstate.ALLOWED`, the command lists, the version, all relative links and anchors,
  and that superseded documents are gone. The six Mermaid diagrams render with mermaid-cli.
* **Status**: fixed, verified.

### PRA-14 — CI gaps (Low)

* **Where**: `.github/workflows/ci.yml` of 0.1.0a1: no package build or clean install test, no link
  check, no dependency audit, no CodeQL, no Dependabot, actions pinned to SHAs that were not re-checked,
  and `AVID_ALLOW_PURE_ED25519=1` set in the pure-signer leg (it masked PRA-04).
* **Fix**: `ci.yml` (syntax, links, matrix 3.10-3.13 × signer, packaging, secret scan of tree and
  history, pip-audit, dependency review), `codeql.yml`, `fuzz.yml` (weekly 10 000 × 8, artifacts),
  `release.yml` (draft release, SBOM, checksums, provenance; manual PyPI Trusted Publishing),
  `dependabot.yml`; every action pinned to a commit SHA resolved with `git ls-remote` on 2026-11-01.
* **Status**: fixed in the files (YAML parses); **NOT VERIFIED** on GitHub.

### PRA-15 — `NOBITEX_ALLOW_LOOSE_TOKEN_PERMS` too broad (Info)

* **Fix**: it now relaxes only the group/other **read** bits; a writable-by-others file, a symlink or a
  file of another user is still refused.
* **Acceptance**: `tests/test_fsio_and_events.py::test_client_secret_reader_honours_the_container_override`.
* **Status**: fixed, verified.

### PRA-16 — project metadata unconfirmed (Low)

* **Where**: `LICENSE` ("The AVID authors"), SECURITY / CODE_OF_CONDUCT contacts, GOVERNANCE maintainers,
  repository URL.
* **Decision**: not guessed. Explicit placeholders; listed in
  [PUBLISHING_CHECKLIST.md](../PUBLISHING_CHECKLIST.md). The MIT license itself is unchanged.
* **Status**: open — owner decision.

### PRA-17 — Decimal / float boundary of order amounts (Info)

* **Where**: `exact_order` computes in `Decimal`; the plan stores floats; `add_limit_order` formats
  `fmt_decimal(float, step)`.
* **Check**: property test over 6 000 random on-grid values per run (3 seeds × 2 000) and steps from
  1e-8 to 1: the text sent equals the planned Decimal every time.
* **Acceptance**: `tests/test_properties.py::test_decimal_text_round_trips_through_float`.
* **Status**: verified, no defect found.

## 5. Hypothetical risks (not reproduced)

| ID | risk | why it stays hypothetical | mitigation in place |
|---|---|---|---|
| H1 | timing side channel of the pure-Python Ed25519 | not measured | refused for live mainnet orders |
| H2 | forward clock jump without the exchange `Date` header opens the next UTC day early | needs NTP failure plus no response headers | backwards jumps stop new orders; at most one extra day of caps |
| H3 | `flock` on NFS | not tested | documented: local disk only |
| H4 | real power loss | simulated (kill after send, torn log), not on hardware | fsync of file and directory, ledger |
| H5 | malware running as the agent's user | out of the threat model | dedicated account, IP-restricted key |
| H6 | Nobitex behaves differently from its documentation (clientOrderId lookup, list lag, status words) | contract tests not run | unknown records fail closed; clientOrderId verified per account |
| H7 | AVID Cloud implementation errors | code not available | the agent validates everything and only tightens |
| H8 | systemd differences between distributions (unit parsing, lingering policy, journald retention) | no systemd user session here | `systemd-analyze verify`; stop verdict independent of systemd |
| H9 | GitHub action versions behave differently from their `action.yml` | workflows not run | SHAs pinned; inputs checked against each action's `action.yml` |

## 6. Summary

17 findings: 2 High, 4 Medium, 7 Low, 4 Info. 14 are fixed and verified locally (PRA-17 verified with
no defect found), 2 are fixed in the files but NOT VERIFIED until CI runs on GitHub (PRA-12, PRA-14),
1 is open for the owner (PRA-16). Two of the fixed ones also have a part that only a real systemd user
session can confirm (PRA-01, PRA-05).

Of the 0.1.0a2 tests that can run against 0.1.0a1 (security proofs, signer, client, disk-full and
shutdown, order state), 9 fail on 0.1.0a1 and pass on 0.1.0a2
([pra_regressions_on_0.1.0a1.txt](evidence-0.1.0a2/pra_regressions_on_0.1.0a1.txt)). The 44 security
proofs pass on 0.1.0a2; 35 of them fail on v1.2 ([findings_on_v1.2.txt](evidence-0.1.0a2/findings_on_v1.2.txt)).
