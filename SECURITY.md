# Security policy

AVID places real orders on a real exchange account. A bug that sends an unwanted or duplicated order,
fails to stop, cancels an order that is not the bot's, or misreports money is a **security
vulnerability** here, not just a bug.

## Reporting

* Do **not** open a public issue for a vulnerability.
* Use [GitHub private vulnerability reporting](https://github.com/mshamsk138/avid/security/advisories/new)
  ("Report a vulnerability" in the repository's **Security** tab), once enabled by the maintainer.
* Maintainer: [Mehdi Shams (@mshamsk138)](https://github.com/mshamsk138).
  The supplied contact identifier `m.shams3141` is **not a complete email address**;
  no private email fallback is published until a deliverable address is confirmed.
  If private vulnerability reporting is unavailable, **do not disclose sensitive details in a public issue**;
  first establish a private communication channel with the maintainer.
* Include the version, the scenario and, if possible, a pytest reproduction against
  `tests/fakes.py` or an `avid-engine simulate --scenario` file (never against a real account).
* We aim to acknowledge within 7 days, agree on a fix and a disclosure date with you, and publish a
  fixed release and an advisory.

## Scope

In scope: `avid_core`, `agent/avid_agent`, `scripts/`, the GitHub workflows, and the agent ↔ cloud
protocol ([docs/AGENT_PROTOCOL.md](docs/AGENT_PROTOCOL.md)). The AVID Cloud service is a separate
codebase. The threat model and its stated limits: [docs/SECURITY_ARCHITECTURE.md](docs/SECURITY_ARCHITECTURE.md).

## Rules for testing

Never test with real money or a real Nobitex account. Use `tests/fakes.py`, the offline simulator, a
local HTTP server, or the official testnet (`NOBITEX_NETWORK=testnet`) with a testnet key.

## Supported versions

Only the latest release of the current line (0.1.x alpha) receives fixes.

## If a key leaked

* **Nobitex API key**: revoke it in the Nobitex panel (API keys) and create a new one restricted to your
  machine's IP; then replace `~/.nobitex/api_key` and `secret`.
* **Agent key** (`~/.avid-agent/agent_key`): revoke the agent in AVID Cloud and pair again
  (`avid-agent pair`), which creates a new key.
* Deleting a file that held a key is not enough once it was shared or committed: rotate it. If it was
  committed, also remove it from the git history and treat every clone as compromised.
