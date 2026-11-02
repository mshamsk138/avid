# Installation verification — AVID 0.1.0a2

What was installed, how, and what was checked afterwards. All runs were offline (no package index):
the wheel installs with `--no-index --no-deps` because AVID has no runtime dependencies. Transcripts:
[evidence-0.1.0a2/](evidence-0.1.0a2/). The release files and their SHA-256 sums are listed in the
release's `SHA256SUMS` (the sums cannot be written into this file, which is itself inside the sdist).

## 1. Build

`python3 scripts/build-dist.py` (setuptools 84 via its PEP 517 hooks) produces:

| file | content |
|---|---|
| `avid_rebalancer-0.1.0a2-py3-none-any.whl` | `avid_core` (with `examples/*.json`, `py.typed`), `avid_agent`, license, entry points `avid-engine`, `avid-agent` |
| `avid_rebalancer-0.1.0a2.tar.gz` | sdist: packages, tests, security proofs, docs, scripts, workflows (`MANIFEST.in`) |
| `avid-rebalancer-0.1.0a2-source.tar.gz` | the source tree (reproducible tar: sorted, fixed mtime and owner) |
| `avid-rebalancer-0.1.0a2.cdx.json` | CycloneDX 1.5 SBOM |
| `SHA256SUMS` | SHA-256 of the four files above |

Wheel metadata checked by `tests/test_packaging.py`: `License-Expression: MIT`, `Requires-Python: >=3.10`,
`Provides-Extra: live`, both entry points, no tests or caches inside.

## 2. Installation paths

| path | how | result | evidence |
|---|---|---|---|
| wheel into a clean venv (no system site packages), path with spaces | `scripts/verify-install.py` | PASS on CPython 3.11.18, 3.12.3, 3.13.18 | [3.11](evidence-0.1.0a2/install_verification_python3.11.txt), [3.12](evidence-0.1.0a2/install_verification_python3.12.txt), [3.13](evidence-0.1.0a2/install_verification_python3.13.txt) |
| sdist (local build of a wheel) | same script, venv with system site packages for setuptools | PASS on 3.13 | [3.13](evidence-0.1.0a2/install_verification_python3.13.txt) |
| editable install (`pip install -e`) | `tests/test_packaging.py::test_editable_install` | PASS on 3.13, 3.11 | full-suite outputs |
| `[live]` extra | `tests/test_packaging.py::test_live_extra_resolves_when_cryptography_is_present` | PASS on 3.13 (local cryptography 50.0.1); skipped where no usable cryptography | full-suite outputs |
| reinstall (`--force-reinstall`) and `pip uninstall` | script + packaging test | PASS; both commands removed | transcripts |
| source checkout without installing (`scripts/avid-agent`, `python -m avid_core.cli`) | `tests/test_performance_and_ops.py::test_installer_runs_without_sudo_into_the_users_path` | PASS (unit gets `PYTHONPATH` of the checkout; `~/.local/bin/avid-agent` link) | full-suite outputs |
| PyPI | — | NOT DONE (not published) | — |
| Python 3.10 | — | NOT VERIFIED | — |

## 3. Checks after installation (each transcript)

| check | expected | result |
|---|---|---|
| `avid-engine --version`, `avid-agent --version` without `PYTHONPATH`, from another directory | `0.1.0a2` | PASS |
| `avid-engine help`, `avid-agent help` | every command listed | PASS |
| `avid-engine simulate --example btc-usdt` | package data found; first decision 200.00 USDT, BUY 0.000333 BTC @ 59988.0 | PASS |
| `avid-engine simulate --example btc-paxg-usdt --json` | `"kind": "SIMULATION"` | PASS |
| `avid-engine status --root <empty>` | "no engine data", nothing created | PASS |
| `avid-engine check` without credentials | exit 2, clear message, no traceback | PASS |
| `avid-agent status` before pairing | works, "not paired" | PASS |
| `avid-agent install --unit-only` (temporary HOME with spaces) | unit written; `systemd-analyze verify` ok; `ExecStart` quoted | PASS |
| `avid-agent install` again | "unchanged" (idempotent) | PASS |
| `avid-agent start` before pairing | refused, exit 2 | PASS |
| `avid-agent uninstall` | unit removed; `~/.avid-agent` and `~/.nobitex` kept | PASS |
| `avid-agent install` as root without `--allow-root` | refused, exit 2, nothing written | PASS ([install_refuses_root.txt](evidence-0.1.0a2/install_refuses_root.txt)) |

The container runs as root, so the transcripts use `--allow-root` for the `install` steps; the root
refusal itself is shown separately and tested in `tests/test_service.py`.

## 4. Service behaviour

Verified with a stub `systemctl` (records calls, plays the service) and a real child process:

* install: checks before writing (root, import with the unit's environment, user manager), backup of a
  changed unit, restore after a failed `daemon-reload`, lingering failure is a note;
* stop: verdict from `last_stop.json`; exit 0 / 3 (not confirmed) / 4 (no report); `--keep-orders`
  writes the one-shot flag before the stop; not running -> in-process cancel;
* restart: keep flag present at restart, removed afterwards; start needs install and pairing;
* uninstall: refuses a unit it did not write; stops first and keeps the unit if the stop was not
  confirmed; never deletes user data;
* SIGTERM to a real `serve()` process with an open bot order: the order is cancelled, exit 0,
  `last_stop.json` says ok.

**NOT VERIFIED**: the same under a real systemd user manager (no user session in this environment);
lingering policy; journald retention on a real system.

## 5. Reproduce

```bash
python3 scripts/build-dist.py --out dist
sha256sum -c dist/SHA256SUMS   # run inside dist/
python3 scripts/verify-install.py dist/avid_rebalancer-0.1.0a2-py3-none-any.whl dist/avid_rebalancer-0.1.0a2.tar.gz
```
