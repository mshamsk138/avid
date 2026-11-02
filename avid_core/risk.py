"""Pre-trade risk checks: may the order the strategy proposed be sent? (report section 3.2)

The strategy (rebalance.plan_multi) already applied the per-order cap, the exchange minimum, the
spread limit and the available balance. These checks add the account-level limits, in this order:

  cash_reserve     a buy must leave at least min_cash_usdt free after the order and its fee reserve
  day_lock         no new order on a day whose counters had to be rebuilt from the ledger
  orders_per_day   submissions today (cancelled ones included) below max_orders_per_day
  daily_notional   filled + still-open notional today, plus this order, within max_daily_usdt

Every check is reported (also when it passes) so the simulator and the logs can show WHY an order was
or was not sent. The first failing check decides; nothing here relaxes anything.
"""
from __future__ import annotations

from dataclasses import dataclass

CASH_MARGIN_USDT = 0.5


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    event: str = 'cap_reached'           # the engine event logged when this check is the first to fail


def pre_trade_checks(plan, *, available_usdt: float, fee_reserve: float, min_cash_usdt: float, state: dict, day: str,
                     max_orders_per_day: int, max_daily_usdt: float) -> list[Check]:
    checks = []
    if plan.action == 'buy':
        need = plan.notional_usdt * (1 + fee_reserve) + CASH_MARGIN_USDT
        left = available_usdt - need
        checks.append(Check('cash_reserve', left >= min_cash_usdt,
                            f'{available_usdt:.2f} free - {need:.2f} (order + fee reserve + margin) = {left:.2f} USDT; '
                            f'floor {min_cash_usdt:g}', event='no_trade'))
    else:
        checks.append(Check('cash_reserve', True, 'sell: frees USDT', event='no_trade'))
    locked = state.get('locked_day') == day
    checks.append(Check('day_lock', not locked, 'state was rebuilt from the ledger today: no new orders until tomorrow '
                        '(UTC)' if locked else 'not locked'))
    n = int(state.get('orders_today', 0))
    checks.append(Check('orders_per_day', n < max_orders_per_day, f'{n} submitted today, cap {max_orders_per_day}'))
    committed = float(state.get('notional_today', 0.0))
    after = committed + plan.notional_usdt
    checks.append(Check('daily_notional', after <= max_daily_usdt + 1e-9,
                        f'{committed:.2f} committed + {plan.notional_usdt:.2f} = {after:.2f} USDT, cap {max_daily_usdt:g}'))
    return checks


def first_failure(checks: list[Check]) -> Check | None:
    return next((c for c in checks if not c.ok), None)


CAP_MESSAGES = {
    'day_lock': 'state file was corrupt or missing; no new orders until tomorrow (UTC)',
    'orders_per_day': 'max orders per day (submissions, cancelled ones included)',
    'daily_notional': 'max daily notional (filled + open)',
}
