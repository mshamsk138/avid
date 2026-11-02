# Installation

Requirements: Linux (Debian, Ubuntu, Kali and similar; systemd for the background service), Python
3.10 or newer. AVID has **no runtime dependencies**. Live orders in API-key mode need the
`cryptography` package (constant-time Ed25519). No step needs root.

AVID is **not on PyPI** yet. Install from a release wheel or from the source.

## 1. Into a virtual environment (recommended)

```bash
python3 -m venv ~/.venvs/avid
~/.venvs/avid/bin/pip install avid_rebalancer-0.1.0a2-py3-none-any.whl            # from the release page
~/.venvs/avid/bin/pip install "avid_rebalancer-0.1.0a2-py3-none-any.whl[live]"    # + cryptography for live
~/.venvs/avid/bin/avid-engine --version
```

From a source checkout instead of a wheel: `~/.venvs/avid/bin/pip install .` (or `.[live]`). For
development: `pip install -e .` and `pip install -r requirements-dev.txt`.

Distribution packages instead of pip: `sudo apt install -y python3 python3-cryptography`.

Check the download first: `sha256sum -c SHA256SUMS` in the folder with the release files. Releases
built by the release workflow also carry a GitHub build-provenance attestation
(`gh attestation verify <file> --repo <owner>/<repo>`).

## 2. Try it without an account

```bash
avid-engine simulate --example btc-usdt          # offline: no network, no credentials, no real order
avid-engine simulate --example btc-paxg-usdt --json > sim.json
```

## 3. Nobitex credentials

```bash
mkdir -m 700 -p ~/.nobitex
# API-key mode (preferred): the key and its Ed25519 private key (seed)
install -m 600 /dev/null ~/.nobitex/api_key && $EDITOR ~/.nobitex/api_key
install -m 600 /dev/null ~/.nobitex/secret  && $EDITOR ~/.nobitex/secret
# or token mode: ~/.nobitex/token
```

The files must belong to you (or root), must not be symlinks, and must not be readable or writable by group or
others. In the Nobitex panel restrict the API key to this machine's public IP and give it only the
permissions it needs (trading, no withdrawal). For the official testnet use a separate folder and
`NOBITEX_NETWORK=testnet`.

Read-only check of the account, then a read-only preview:

```bash
NOBITEX_SECRETS_DIR=~/.nobitex avid-engine check --weights btc=0.5,usdt=0.5
NOBITEX_SECRETS_DIR=~/.nobitex avid-engine plan  --weights btc=0.5,usdt=0.5 --root ~/avid-engine
```

## 4. The agent as a background service

```bash
avid-agent install                                   # systemd --user unit, no root
avid-agent pair --cloud https://<your AVID Cloud> --code <code from the web app>
avid-agent ceilings                                  # live_allowed is false by default
avid-agent start
avid-agent status
avid-agent logs -f
```

What `install` does, in this order:

1. refuses to run as root (the agent is a per-user service);
2. checks that this Python can import the agent with the environment the unit will have;
3. checks that the systemd user manager answers (`--unit-only` writes the unit anyway);
4. writes `~/.config/systemd/user/avid-agent.service` (an identical unit is left alone; a different one
   is backed up as `avid-agent.service.bak-<time>` first); a failed `daemon-reload` restores the
   previous unit;
5. enables lingering so the agent keeps running after logout (if this needs an administrator, it says
   so and continues);
6. from a source checkout, links `~/.local/bin/avid-agent` to `scripts/avid-agent`.

The unit runs `<this python> -m avid_agent run` with `PYTHONPATH` set only for a source checkout; paths
with spaces and special characters are escaped for systemd. Service behaviour:

| | |
|---|---|
| stop (`avid-agent stop`, `systemctl --user stop`, SIGTERM) | controlled: no new orders, the bot's own orders are cancelled, the verdict goes to `~/.avid-agent/last_stop.json`; `avid-agent stop` exits 0 / 3 / 4 (see [CLI_REFERENCE.md](CLI_REFERENCE.md#exit-codes-1)) |
| `TimeoutStopSec` | 300 s: the agent's own stop is bounded by a 60 s step drain, a 150 s cancel budget and a 20 s final report |
| restart (`avid-agent restart`) | orders kept and managed again |
| crash | `Restart=always` after 30 s (at most 5 starts in 10 minutes); the engine recovers from its state and ledger |
| configuration error (exit 2) | not restarted (`RestartPreventExitStatus=2`) |
| logs | journald (`avid-agent logs`), rotated by journald; the engine's own `log.jsonl` rotates at 20 MB (three generations) |
| hardening | `NoNewPrivileges=yes`, `UMask=0077`, `KillMode=mixed` |

**Docker or another supervisor**: give the container at least 300 s to stop
(`stop_grace_period: 300s`); the default 10 s kills the agent before it can cancel its orders.

## 5. Going live (only after testing)

1. Run on the **official testnet** first (`NOBITEX_NETWORK=testnet`, a testnet key), including the
   contract tests ([TESTING.md](TESTING.md#exchange-contract-tests-testnet)).
2. Install `cryptography` (live API-key orders are refused without it).
3. Small ceilings: `avid-agent ceilings --live yes --max-order 15 --max-orders-day 4 --max-daily 60`.
4. Approve a policy with mode `live` in AVID Cloud. The agent trades live only when **both** its own
   `live_allowed` and the cloud directive say so.

The standalone engine trades live only with `avid-engine run --live`.

## Upgrade

Stop with orders kept, install the new version, start again:

```bash
avid-agent stop --keep-orders
~/.venvs/avid/bin/pip install --upgrade <new wheel>
avid-agent install            # rewrites the unit only if it changed (the old one is backed up)
avid-agent start
```

Read the CHANGELOG first: an alpha release can change file formats or the cloud protocol.

## Uninstall

```bash
avid-agent uninstall          # controlled stop (cancels the bot's orders), removes the service
pip uninstall avid-rebalancer
```

`uninstall` never deletes `~/.avid-agent` (agent identity, state, outbox, logs) or `~/.nobitex` (your
API key). To remove them for good: revoke the agent in AVID Cloud and the API key in the Nobitex panel,
then delete the folders yourself.

## Where things are

| path | content |
|---|---|
| `~/.avid-agent/` (0700) | `agent.json`, `agent_key`, `ceilings.json`, `agent_state.json`, `outbox.sqlite3`, `last_stop.json`, `data/bot/` |
| `~/.nobitex/` (0700) | `api_key` + `secret`, or `token` |
| `~/.config/systemd/user/avid-agent.service` | the unit |
| `<root>/data/bot/` | the standalone engine's folder (`avid-engine run --root <root>`) |

Problems: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
