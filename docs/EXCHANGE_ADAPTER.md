# Exchange adapter

The engine reaches an exchange only through one object with the surface below
(`avid_core.models.ExchangeAdapter`, a `typing.Protocol`). Two implementations ship:

| implementation | for | network |
|---|---|---|
| `avid_core.client.NobitexClient` | **Nobitex**, mainnet or the official testnet (`NOBITEX_NETWORK=testnet`) | HTTPS |
| `avid_core.sim.SimExchange` | `avid-engine simulate` (deterministic, offline) | none |

`tests/fakes.py:FakeExchange` is a third, fault-injecting implementation used by the tests.
`tests/test_sim_cli.py::test_every_adapter_has_the_exchange_adapter_surface` checks all three against
the protocol.

**Nobitex is the only supported exchange.** Another exchange would need a new adapter that keeps every
rule on this page, its own contract tests against that exchange's test environment, and a review of the
market specs - none of which exists today.

## Methods

| method | returns | contract |
|---|---|---|
| `network`, `signer` | attributes | `network`: `mainnet`, `testnet`, `simulation`, ...; `signer`: `cryptography`, `pure` or `none` (the engine refuses live orders signed by `pure` except on the official testnet with an explicit opt-in) |
| `best_bid_ask(symbol)` | `(bid, ask)` floats | best levels; raise on an empty, crossed or zero book |
| `wallets(currencies)` | `{cur: {'total', 'available'}}` | **total** values the portfolio (locked funds still belong to it), **available** sizes orders; finite, ≥ 0, available ≤ total |
| `balances(currencies)` | `{cur: available}` | fallback for simple clients |
| `open_orders(src, dst)` | list of order records | the account's open orders on one market |
| `list_orders(src, dst, status)` | list of order records | `status='all'` (or the exchange's equivalent) is used to find a lost reply |
| `add_limit_order(side, amount, price, src, dst, amount_decimals, price_decimals, client_order_id)` | order record with `id` | ONE limit order; amount and price sent as exact decimal text on the grid; **never retried** by the adapter |
| `order_status(order_id=None, *, client_order_id=None)` | order record | exactly one of the two keys |
| `cancel(order_id=None, *, client_order_id=None)` | `None` | a **request**; success does not mean cancelled - the engine waits for the final record |
| `trades_page(src, dst, page)` | `{'trades': [...], 'hasNext': bool}` | the account's own executions, newest first |
| `market_specs()` | `{'markets': {...}, 'meta': {...}}` | step, tick and minimum notional in force |
| `server_time()` | epoch seconds or `None` | the exchange's clock (Nobitex: from the HTTP `Date` header) |

## Order record (Nobitex wire shape)

```json
{"id": 1001, "clientOrderId": "av3f2a000001...", "type": "buy", "srcCurrency": "btc", "dstCurrency": "usdt",
 "amount": "0.000333", "matchedAmount": "0", "unmatchedAmount": "0.000333", "price": "59988",
 "status": "Active", "fee": "0", "averagePrice": "0", "created_at": "2026-01-05T18:00:00+00:00"}
```

The engine (`records.py`) accepts numbers as strings or numbers, and requires `amount = matched +
unmatched` within a tolerance; an amount different from what was sent, negative amounts, a missing pair
of amounts or an unknown status word make the record `UNKNOWN` (never a guess). Known status words:
open `new`, `active`, `inactive`, `open`; final `done` (filled), `canceled` / `cancelled`, `rejected`.

## Errors

Every failure is an `avid_core.client.NobitexError` with `code` and `http_status`:

| situation | code / status | the engine treats it as |
|---|---|---|
| DNS failure, connection refused, the send deadline passed before sending | `NOT_SENT` | nothing left this machine: a placement is definitely not placed |
| timeout, reset, TLS error mid-way | `NETWORK` | **may have reached the exchange** |
| HTTP 408, 409, 425, 429, 5xx | status | may have reached the exchange |
| any code containing "duplicate" | code | an order with these parameters **may exist** |
| other HTTP 4xx, or `status != ok` with an API code | status / code | a definite refusal |
| unreadable or oversized (> 2 MB) body, NaN / duplicate keys / deep nesting | `BAD_RESPONSE` | unknown |
| a redirect | `REDIRECT` | refused; credentials are never sent to another host |
| clientOrderId not enabled for the account | `FeatureUnavailable` | live trading turned off |

## Retries and idempotency

* Read-only calls are retried on 429 / 5xx / network errors with exponential backoff, honouring the
  exchange's `backOff` / `Retry-After`; nothing is retried past the send deadline.
* **Placing an order is never retried.** A possibly placed order is looked up by its clientOrderId in
  later cycles; it is never sent a second time.
* Cancels are repeated by the engine in later cycles until a final record arrives; a cancel of an
  order that is no longer open is harmless.
* Trades are deduplicated by their exchange trade id.
* Client-side rate limits follow the documented Nobitex limits (`client.RATE_LIMITS`).

## Send deadline

`adapter.deadline` (monotonic seconds, optional attribute) bounds a controlled stop: once it has passed
the client raises `NOT_SENT` **before** sending anything, and no retry sleeps past it. The agent sets it
on SIGTERM (`STEP_DRAIN_S` = 60 s for the step in progress) and during `safe_shutdown` (150 s budget).

## Signing and transport (Nobitex)

API-key mode signs `timestamp + METHOD + path?query + body` with Ed25519 (`Nobitex-Key`,
`Nobitex-Timestamp`, `Nobitex-Signature`); token mode sends `Authorization: Token ...`. TLS is verified
by the Python defaults, only the two official hosts are allowed, redirects are refused, the
`User-Agent` is `TraderBot/avid-<version>` as Nobitex recommends.

## Market specs

`rebalance.MARKETS` holds the documented Nobitex values (BTCUSDT: amount step 1e-6, tick 0.01;
PAXGUSDT: step 1e-4, tick 0.01; minimum notional more than 11 USDT). If Nobitex changes them, an
operator can put a validated `data/bot/markets.json` next to the state (powers of ten only, minimum
notional within a sane range); an invalid file stops new orders.

## Contract tests

`tests/contract/test_nobitex_testnet.py` checks the assumptions above against the **official
testnet** (clientOrderId lookup and echo, list lag, status words, cancel by clientOrderId). They run
only with `AVID_CONTRACT_TESTNET=1 NOBITEX_NETWORK=testnet NOBITEX_SECRETS_DIR=<testnet key>` and are
skipped otherwise. They have **not been run** for this release (no testnet key was available); see
[audits/TEST_EVIDENCE.md](audits/TEST_EVIDENCE.md).
