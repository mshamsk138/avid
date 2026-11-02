"""Typed contracts of the data the engine keeps and exchanges (report sections 3.3 and 8.3).

The engine stores plain JSON-compatible dicts (state.json, the log, the outbox); these TypedDicts
document their shape for readers, type checkers and new adapters. They do not change any behaviour.

Money: the ORDER that is sent is computed in decimal.Decimal on the market grid (rebalance.exact_order)
and leaves the engine as the exact decimal text of that value (client.fmt_decimal). Weights,
deviations and day counters are floats: they decide WHETHER to trade and are compared with tolerances,
never sent to the exchange.
"""
from __future__ import annotations

from typing import Literal, Protocol, TypedDict, runtime_checkable

OrderState = Literal['CREATED', 'SUBMISSION_PENDING', 'OPEN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED', 'UNKNOWN',
                     'MANUAL_RECONCILIATION', 'FILLED_CONFIRMED', 'CANCELED_CONFIRMED', 'PARTIALLY_FILLED_CANCELED',
                     'REJECTED_CONFIRMED']
ReconciliationStatus = Literal['PENDING', 'PARTIALLY_RECONCILED', 'RECONCILED', 'AGGREGATE_ONLY',
                               'INCOMPLETE_RECONCILIATION']
Side = Literal['buy', 'sell']


class MarketSpec(TypedDict):
    symbol: str                  # e.g. 'BTCUSDT'
    amount_step: float           # power of ten
    price_tick: float            # power of ten
    min_notional: float          # USDT; the exchange wants MORE than this


class RiskLimits(TypedDict, total=False):
    live_allowed: bool
    max_order_usdt: float
    max_orders_per_day: int
    max_daily_usdt: float
    max_spread: float
    min_cash_usdt: float


class Order(TypedDict, total=False):
    """One tracked bot order (state.json: bot_orders / closing / pending)."""
    asset: str
    side: Side
    amount: float                # the amount SENT (already on the market grid)
    price: float
    notional: float
    placed_at: float             # engine clock, epoch seconds
    day: str                     # UTC day whose caps it uses
    cid: str                     # clientOrderId, 32 chars
    matched: float               # highest filled amount seen so far
    state: OrderState
    cancel_requested: bool
    settled: bool                # its final record was applied to the caps (exactly once)


class Fill(TypedDict, total=False):
    trade_id: str
    exchange_order_id: str
    asset: str
    side: Side
    amount: float
    price: float
    fee: float
    fee_asset: str
    aggregate: bool              # True: known only from order totals, NOT an execution record


class Portfolio(TypedDict):
    balances: dict               # TOTAL per asset (values the portfolio)
    available: dict              # free per asset (sizes orders)
    books: dict                  # asset -> [best bid, best ask]
    targets: dict                # asset -> weight; unnamed managed assets are 0


class PolicyDirective(TypedDict, total=False):
    version_id: str
    weights: dict
    band_rel: float
    max_order_usdt: float
    max_orders_per_day: int
    max_daily_usdt: float
    min_cash_usdt: float
    max_spread: float
    expires_at: int              # epoch milliseconds


class CloudDirective(TypedDict, total=False):
    global_halt: bool
    portfolio: dict | None       # {'name', 'mode', 'kill_switch', 'policy': PolicyDirective | None}


class Ack(TypedDict):
    accepted: list               # item_keys durably stored by the cloud
    rejected: list               # [{'item_key', 'reason'}]


class ReconciliationResult(TypedDict, total=False):
    reconciliation_status: ReconciliationStatus
    expected_filled_amount: float | None
    observed_filled_amount: float
    missing_filled_amount: float | None
    executions_count: int
    pagination_complete: bool
    last_reconciliation_at: float | None


@runtime_checkable
class ExchangeAdapter(Protocol):
    """What the engine needs from an exchange. NobitexClient implements it for Nobitex (the only
    supported exchange); avid_core.sim.SimExchange implements it offline. Another exchange needs an
    adapter with the same behaviour - see docs/EXCHANGE_ADAPTER.md for the exact contract (errors,
    retries, idempotency)."""

    network: str                 # 'mainnet' | 'testnet' | 'simulation' ...
    signer: str                  # 'cryptography' | 'pure' | 'none' - the engine refuses live 'pure' on mainnet

    def best_bid_ask(self, symbol: str) -> tuple: ...
    def wallets(self, currencies=...) -> dict: ...
    def balances(self, currencies=...) -> dict: ...
    def open_orders(self, src: str = ..., dst: str = ...) -> list: ...
    def list_orders(self, src: str = ..., dst: str = ..., status: str = ...) -> list: ...
    def add_limit_order(self, side, amount, price, src=..., dst=..., amount_decimals=..., price_decimals=...,
                        client_order_id=...) -> dict: ...
    def order_status(self, order_id=None, *, client_order_id=None) -> dict: ...
    def cancel(self, order_id=None, *, client_order_id=None) -> None: ...
    def trades_page(self, src: str = ..., dst: str = ..., page: int = ...) -> dict: ...
    def market_specs(self) -> dict: ...
    def server_time(self) -> float | None: ...
