# CLI reference

Two commands are installed by `pip install avid-rebalancer` (or the wheel): `avid-engine` and
`avid-agent`. From a source checkout without installing, use `python3 -m avid_core.cli` and
`scripts/avid-agent`. Every command has `--help`; `help <command>` prints the same.

Output is human-readable by default; `--json` (where listed) prints the machine record instead.

## avid-engine

The engine on its own: no cloud. Real orders only with `run --live`.

| command | what it does | network | sends orders |
|---|---|---|---|
| `check` | balances (total / available), books, portfolio value and weights | Nobitex, read-only | never |
| `plan` | one cycle against the real account in a read-only preview: what would be done now | Nobitex, read-only | never; never cancels |
| `simulate` | the real engine against stored synthetic prices on the offline `SimExchange` | none | never (simulated exchange only) |
| `run` | the rebalancing loop | Nobitex | only with `--live` |
| `status` | local state of an engine folder: mode, STOP files, today's counters, open bot orders, reconciliation, last log events | none | never |
| `help [command]` | help | none | never |

Credentials for `check`, `plan`, `run`: folder `$NOBITEX_SECRETS_DIR` (default `/run/secrets/nobitex`)
with `api_key` + `secret` (Ed25519 seed, API-key mode) or `token`. Network: `NOBITEX_NETWORK` = empty /
`mainnet` / `testnet`; anything else is refused.

### Options

| option | commands | default | meaning |
|---|---|---|---|
| `--json` | all except `help` | off | JSON output |
| `--root DIR` | `plan`, `run`, `status` | `.` | engine root; state in `DIR/data/bot` |
| `--weights` | `check`, `plan`, `run` | `btc=0.34,paxg=0.33,usdt=0.33` | target weights; an asset left out has target 0; `usdt` is the rest if omitted |
| `--band-rel` | `plan`, `run` | 0.25 | trade when a weight is off target by more than this fraction of it |
| `--min-order-usdt` | `plan`, `run` | 11.5 | never below the exchange minimum |
| `--max-order-usdt` | `plan`, `run` | 20 | ceiling of one order |
| `--max-orders-per-day` | `plan`, `run` | 6 | submissions per UTC day |
| `--max-daily-usdt` | `plan`, `run` | 100 | filled + open notional per UTC day; at least `--max-order-usdt` |
| `--stale-minutes` | `plan`, `run` | 30 | cancel a bot order older than this |
| `--max-spread` | `plan`, `run` | 0.01 | widest book that is traded |
| `--min-cash-usdt` | `plan`, `run` | 0 | USDT a buy must leave free |
| `--live` | `run` | off | actually send orders (otherwise dry run: orders are shown, not sent; the bot's own open orders are cancelled) |
| `--once` | `run` | off | one cycle, then exit |
| `--interval S` | `run` | 600 | seconds between cycles (60..86400) |
| `--example NAME` | `simulate` | `btc-usdt` | bundled scenario: `btc-usdt`, `btc-paxg-usdt` |
| `--scenario FILE` | `simulate` | - | your own scenario file (format `avid-sim-scenario/1`, see below) |
| `--steps N` | `simulate` | all | replay only the first N steps |
| `--root DIR` | `simulate` | temporary | keep the simulated engine data in an empty folder (or one from an earlier simulation; a folder with real engine data is refused) |
| `--list` | `simulate` | - | list the bundled examples |

Every number is validated (finite, typed, in range); an invalid value is a usage error (exit 2).

### Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | `status`: something needs attention (STOP present, unsettled order, alert, unreadable state) |
| 2 | usage error, invalid scenario or configuration, exchange error (message on stderr) |
| 130 | interrupted (Ctrl+C) |

### Simulation scenario file

```json
{
  "format": "avid-sim-scenario/1",
  "name": "my-test",
  "start_time": "2026-01-05T00:00:00Z",
  "interval_s": 600,
  "balances": {"usdt": 200, "btc": 0},
  "targets": {"btc": 0.5, "usdt": 0.5},
  "settings": {"band_rel": 0.25, "max_order_usdt": 20, "max_orders_per_day": 6, "max_daily_usdt": 100},
  "exchange": {"fee_rate": 0.002, "spread_rel": 0.0004},
  "prices": {"btc": [60000, 60120.5, 59870.25]}
}
```

`prices` are mid prices per step (all lists the same length, at most 10 000 steps); `settings` accepts
`band_rel`, `min_order_usdt`, `max_order_usdt`, `max_orders_per_day`, `max_daily_usdt`, `stale_minutes`,
`max_spread`, `min_cash_usdt`, `fee_reserve` with the same ranges as the CLI. `scripts/make-sim-prices.py`
writes a reproducible synthetic path. Fill rule: a resting buy fills completely at its price once a
later step's best ask is at or below it (a sell: best bid at or above it); no partial fills, slippage
or outages.

`--json` output fields: `kind` (`SIMULATION`), `first_decision` (`portfolio_value_usdt`,
`current_weights`, `target_weights`, `band`, `decision`, `order`, `risk_checks`, `reason`), `orders`
(placed / filled / canceled), `fills`, `fees_usdt_realized`, `start_value_usdt_estimated`,
`end_value_usdt_estimated`, `end_weights`, `hold_value_usdt_estimated`, `timeline`, `exchange_orders`,
`assumptions`.

## avid-agent

The Private Agent. Every command works the same from a pip install and from `scripts/avid-agent`.

| command | what it does |
|---|---|
| `install [--no-linger] [--unit-only]` | writes `~/.config/systemd/user/avid-agent.service` (checks first; idempotent; a changed unit is backed up), `daemon-reload`, enables lingering; refuses to run as root; from a source checkout also links `~/.local/bin/avid-agent` |
| `uninstall [--keep-orders] [--force]` | controlled stop, then removes the unit; keeps `~/.avid-agent` and `~/.nobitex`; stops before removing if a cancellation was not confirmed (unless `--force`) |
| `pair --cloud URL --code CODE` | creates the agent key and pairs this machine with an AVID Cloud account |
| `ceilings [--live yes|no] [--max-order N] [--max-orders-day N] [--max-daily N] [--max-spread F] [--min-cash N]` | shows or changes the local hard limits; `--live yes` asks you to type `LIVE` |
| `start` | enables and starts the service (needs `install` and `pair`) |
| `stop [--keep-orders]` | controlled stop: no new orders, the bot's own open orders are cancelled, the agent's verdict is reported |
| `restart` | restarts the service and keeps the bot's open orders (one-shot flag, valid 10 minutes) |
| `status [--json]` | service state, cloud link, ceilings, open bot orders, unsettled orders, last stop |
| `logs [-f] [-n N]` | service output from journald |
| `once` | one step in the foreground |
| `run [--interval S] [--offline-grace S]` | the loop in the foreground (what the service runs); SIGTERM / Ctrl+C = controlled stop |
| `safe-stop` | in the foreground: cancel the bot's open orders and report |
| `resolve --not-placed` / `resolve --closed ID...` | settle an order by hand **after** checking the Nobitex panel; `--not-placed` looks the order up on Nobitex first and refuses if it exists |
| `reset-cid` | try clientOrderId again after Nobitex enabled it for the account |
| `help` | the command list |

Environment: `AVID_AGENT_HOME` (default `~/.avid-agent`), `NOBITEX_SECRETS_DIR` (default `~/.nobitex`),
`NOBITEX_NETWORK`; `install` copies these into the unit when they are set.

### Exit codes

| code | meaning |
|---|---|
| 0 | ok; for `stop` / `uninstall`: no bot order is left open, or they were kept on purpose |
| 1 | a confirmation prompt was answered with something else (nothing changed) |
| 2 | usage or configuration error (not paired, not installed, invalid ceilings, no systemd user manager) |
| 3 | `stop` / `uninstall` / `safe-stop` / `run`: the cancellation of some bot orders was **not confirmed** - check the Nobitex panel |
| 4 | `stop` / `uninstall`: the service stopped without reporting (killed after `TimeoutStopSec`, crashed) - bot orders **may** still be open |
| 130 | interrupted |
