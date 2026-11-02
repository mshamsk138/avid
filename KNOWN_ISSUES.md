# Known issues and limits — 0.1.0a2

Not production-ready. Do not describe this release as production-ready or live-trading validated.
The release gates and what was verified: [docs/audits/RELEASE_READINESS.md](docs/audits/RELEASE_READINESS.md).

## Blocking for real money

1. **AVID Cloud must implement protocol v2** ([docs/AGENT_PROTOCOL.md](docs/AGENT_PROTOCOL.md)) before the
   agent can report or place new orders. The cloud code is not part of this repository; see
   [docs/audits/CLOUD_REQUIRED_CHANGES_FA.md](docs/audits/CLOUD_REQUIRED_CHANGES_FA.md).
2. **Nobitex contract tests have not been run** (`tests/contract`, AVID-07): clientOrderId behaviour,
   list lag and status words are taken from the documentation and must be confirmed on the official
   testnet.
3. **The systemd service was not run under a real systemd user manager** for this release: the unit was
   verified with `systemd-analyze verify`, the commands against a stub `systemctl`, and SIGTERM handling
   with a real process. Test `avid-agent install/start/stop/restart/uninstall` on your distribution
   before relying on them.

## Verification gaps

4. **Python 3.10** is supported by the code and in the CI matrix, but no 3.10 interpreter was available
   locally; 3.11, 3.12 and 3.13 were run. The GitHub workflows (CI, CodeQL, release, fuzz) were written
   and statically checked but **have not run on GitHub yet**.
5. `pip-audit`, CodeQL, `ruff` / `mypy` were not run locally (no package index access); they run in CI.
6. The original 163 tests of v1.2 were not available; this release ships its own suite.

## Design limits

7. **Same-user compromise** (malware running as the agent's Unix user) can read the Nobitex key and change
   the local ceilings. Use a dedicated account and an IP-restricted API key.
8. **Two agents on one Nobitex account** are detected and block each other, but there is no exchange-side
   lock. Run one agent per account.
9. **NFS / network volumes** are not supported for the data folder (`flock` semantics).
10. **Power loss** is handled by design (fsync, ledger) and simulated, not tested on hardware.
11. **Docker / other supervisors**: give the agent at least 300 s to stop (`stop_grace_period: 300s`);
    the default 10 s kills it before it can cancel its orders.
12. **Pure-Python Ed25519** is not constant time; it is refused for live orders except on the official
    testnet with `AVID_ALLOW_PURE_ED25519=1`. Its timing behaviour was not measured.
13. **A cloud acting within the allowed limits** can still move exposure by up to `max_daily_usdt` per
    UTC day. Keep local ceilings small.
14. A clock that jumps **forward** while the exchange's `Date` header is unavailable can open the next UTC
    day early (one extra day of caps at most).
15. The **offline simulator** fills a resting order completely once the book crosses it: no partial
    fills, queue position, slippage or outages. Its results show how the engine decides, not what a
    market will do.
16. Test dependencies are version-pinned but not hash-locked.
17. Relative links in README.md do not resolve on PyPI's project page (the package is not on PyPI).

## Project metadata to confirm before publication

18. Owner **Mehdi Shams** and repository **mshamsk138/avid** are now recorded in metadata.
    A working private security/conduct contact is still needed: `m.shams3141` is only a contact identifier,
    not a complete email address. See [docs/PUBLISHING_CHECKLIST.md](docs/PUBLISHING_CHECKLIST.md).
