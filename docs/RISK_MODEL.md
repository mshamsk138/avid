# Risk model

The risk model limits **how much** the bot can do and **stops** it when it cannot be sure of what it is
doing. It does not judge whether a trade is a good idea (that is the strategy,
[STRATEGY_MATH.md](STRATEGY_MATH.md)), and it cannot make a loss impossible: prices move, fees are paid,
and a rebalanced portfolio can end below simply holding.

## Layers of limits

| layer | set by | can it loosen a lower layer? |
|---|---|---|
| exchange rules | Nobitex (minimum order > 11 USDT on USDT markets, amount step, price tick) | no |
| local ceilings | the customer: CLI flags of `avid-engine`, or `~/.avid-agent/ceilings.json` (`avid-agent ceilings`) | - |
| control file | dashboard / agent: `data/bot/control.json` | no: **tighten only** |
| cloud policy | AVID Cloud directive | no: **tighten only** |

Effective value of each limit:

| limit | effective value | valid range (`validation.SPECS`) |
|---|---|---|
| `max_order_usdt` | min(local, control, cloud) | (0, 1 000 000] |
| `max_orders_per_day` | min(local, control, cloud) | whole number (0, 1000] |
| `max_daily_usdt` | min(local, control, cloud) | (0, 10 000 000], and ≥ `max_order_usdt` |
| `max_spread` | min(local, control, cloud) | (0, 0.05] |
| `min_cash_usdt` | max(local, control, cloud) | [0, 1e9] |
| `min_order_usdt` | max(configured, exchange minimum) | ≥ 11.5 by default |
| `band_rel` | control / cloud (a strategy choice, not a cap) | [0.05, 0.90] |
| `stale_minutes` | local | [1, 1440] |
| live trading | `--live` (engine) / `live_allowed: true` in the agent's own ceilings **and** a live directive | the cloud can never switch it on by itself |

## Pre-trade checks (`avid_core/risk.py`)

The strategy already applied the per-order cap, the exchange minimum, the spread limit and the free
balance. These account-level checks run next, in this order, and **every** result is reported (also
when it passes) in the cycle record as `risk_checks`:

| check | passes when | on failure |
|---|---|---|
| `cash_reserve` | a buy leaves at least `min_cash_usdt` free after the order, its fee reserve and a 0.5 USDT margin; a sell always passes | `no_trade` |
| `day_lock` | the state was not rebuilt from the ledger today | `cap_reached` (no new orders until the next UTC day) |
| `orders_per_day` | orders **submitted** today < cap (cancelled ones count) | `cap_reached` |
| `daily_notional` | filled + still open notional today + this order ≤ cap | `cap_reached` |

The first failing check decides; nothing in this module relaxes anything.

## Holds: no new orders, cancels continue

A hold stops **new** orders. It never stops the bot from cancelling its own orders.

* the customer's `STOP` or the cloud's `STOP.cloud` exists (these also cancel every bot order);
* an order whose placement reply was lost is not settled; a final state is `UNKNOWN`; a bot order is
  still open; a non-bot order is open on a managed market;
* `control.json` is invalid, or missing after one was in use (the last valid one stays in force);
* an invalid risk number from any source (NaN, infinity, wrong type, out of range, daily < per-order);
* `markets.json` is invalid;
* the system clock went back more than 5 minutes, or differs from the exchange's `Date` header by more
  than 5 minutes;
* the state file was corrupt or missing (day locked until the next UTC day);
* agent: the cloud link is not fresh, the agent is revoked, offline longer than the grace period
  (default 30 minutes; then it cancels its orders), the outbox is under storage pressure or broken,
  the policy is invalid or expired;
* live orders in API-key mode without the constant-time signer (`cryptography`); the pure-Python signer
  is accepted only on the official testnet with `AVID_ALLOW_PURE_ED25519=1`;
* the account's clientOrderId support is unverified (lookup did not answer) or unavailable.

## Fail closed, but keep the right to cancel (Rule 1)

When an order state, a risk limit, a financial record or a policy is uncertain, the engine:

* sends **no** new order;
* releases **no** reservation automatically (an `UNKNOWN` order keeps its caps counted);
* never turns an ambiguous answer into a success, and never re-sends a possibly placed order;
* records and reports the situation (error events reach the dashboard through the agent);
* still tries to cancel the bot's own orders where that is safe: STOP, dry mode, revocation, offline
  timeout, exchange listing errors, and even a full disk (write-free emergency cancel, PRA-09).

## What the risk model does not cover

* **A cloud acting within the allowed limits** can still move exposure by up to `max_daily_usdt` per
  UTC day and churn at roughly `max_daily_usdt × (fee + max_spread)` per day. Keep the local ceilings
  small.
* **Market risk**: weights are kept, value is not protected. PAXG and BTC can both fall.
* **Exchange risk**: an exchange outage, a frozen account, sanctions or a change of the API rules.
  The documented Nobitex values (`rebalance.MARKETS`) can be overridden with a validated
  `markets.json` when the exchange changes them.
* **Same-user compromise**: a process running as the agent's Unix user can rewrite `ceilings.json`.
  See [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md).
