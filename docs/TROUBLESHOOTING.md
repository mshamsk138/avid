# Troubleshooting

First look: `avid-agent status` (or `avid-engine status --root <root>`) and the last events in the log
(`avid-agent logs -n 50`, or `<root>/data/bot/log.jsonl`). Events at level `error` are the ones that
need you.

| symptom / event | cause | what to do |
|---|---|---|
| `no credentials in ...` | `NOBITEX_SECRETS_DIR` points to an empty folder | put `api_key` + `secret` (or `token`) there, mode 0600 ([INSTALLATION.md](INSTALLATION.md#3-nobitex-credentials)) |
| `... permissions 0o644 too open` / `writable by others` / `belongs to another user` / `is a symlink` | file permissions of a secret | `chmod 600 ~/.nobitex/*`; replace symlinks by real files; for read-only container secrets set `NOBITEX_ALLOW_LOOSE_TOKEN_PERMS=1` |
| HTTP 401/403, "IP ... not whitelisted" | the API key does not allow this machine's IP | add the IP in the Nobitex panel (API keys) |
| `config_conflict`: "live orders need a constant-time Ed25519 signer" | `cryptography` missing or broken (for example a package built for another Python version) | `sudo apt install -y python3-cryptography` or `pip install "avid-rebalancer[live]"`; `python3 -c "from avid_core import ed25519; print(ed25519.backend(), ed25519.backend_error())"` shows why |
| `client_order_id_unavailable` / `FeatureUnavailable` | clientOrderId is not enabled for the account | ask Nobitex to enable it, then `avid-agent reset-cid`; until then live trading stays off (simulation and cancels work) |
| `pending_unresolved` | an order got no reply and is not visible on the exchange | check **open and recent orders** in the Nobitex panel. If it exists, wait: the engine adopts it. Only if it does not exist: `avid-agent resolve --not-placed` |
| `order_status_unresolved` / `UNKNOWN` | the final record of a bot order cannot be read | check the order in the panel; if it is no longer open: `avid-agent resolve --closed <id>` |
| `clock_backwards` / `clock_mismatch` | system clock went back, or differs from the exchange by > 5 min | fix NTP (`timedatectl status`); new orders resume by themselves |
| `control_invalid` | `data/bot/control.json` unreadable, invalid, or deleted after use | fix or restore it; the last valid one stays in force meanwhile, no new orders |
| `state_recovered_from_ledger` | `state.json` was corrupt or missing | nothing to do: counters and unsettled orders were rebuilt; new orders resume the next UTC day. The corrupt file was kept as `state.corrupt-*.json` |
| `valuation_incomplete` | no valid price for a held or targeted asset | wait; trading resumes when the book is valid |
| `other_avid_instance` | open orders carry another AVID instance's clientOrderId | run **one** agent per Nobitex account; stop the other one |
| `cap_reached` | a daily cap is used up | expected; it resets at 00:00 UTC |
| `waiting` "non-bot open order(s)" | you have your own open order on a managed market | the bot does not trade next to your orders; cancel or fill yours |
| `storage_pressure` / outbox errors | the cloud has not acknowledged reports, the queue is full | check the cloud link; nothing financial is dropped; no new orders meanwhile |
| `emergency_cancel` | the disk is full or read-only | free space; the engine sent cancel requests without writing and resumes normally once it can write |
| `avid-agent stop` exits 3 | some cancellations were not confirmed | check those order ids in the Nobitex panel; cancel by hand if still open |
| `avid-agent stop` exits 4 | the service did not report (killed after 300 s, crashed) | check the panel for open bot orders; `avid-agent status` |
| `install`: "systemd user manager is not reachable" | no `systemctl --user` (container, WSL, SSH without a user session) | run `avid-agent run` in a terminal / tmux, or `install --unit-only` and start it later |
| `install`: "could not enable lingering" | `loginctl enable-linger` needs an administrator here | ask one to run `loginctl enable-linger <user>`; otherwise the agent stops at logout |
| `install`: "do not install the agent as root" | run as root | use your normal account |
| `agent_revoked` | the agent was revoked in AVID Cloud | pair again (`avid-agent pair`); revocation cancels the bot's orders |
| `simulate`: "holds engine data that is not from a simulation" | `--root` points at a real engine folder | use an empty folder; simulated data never mixes with real data |

Still stuck: [SUPPORT.md](../SUPPORT.md). Never post API keys, `agent_key`, `state.json` with real order
ids, or logs with balances in a public issue.
