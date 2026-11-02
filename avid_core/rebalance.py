"""Pure rebalancing arithmetic (no network, no side effects).

Weights and deviations are computed in float (they only decide WHETHER to trade). The order that
is actually sent - price on the market's tick, amount on its step, and the notional checked
against min/max - is computed in decimal.Decimal, so rounding at the exchange's boundaries
(minimum order value, available balance, caps) is exact (audit P2-22).

Portfolio value (AVID-03): the NAV is computed over every MANAGED asset the account holds, not only
the assets named in the current targets. An asset dropped from the strategy gets target 0 explicitly
(so it is sold down), and a held asset without a valid price makes the valuation INCOMPLETE
(ValuationIncomplete) - it is never silently left out of the NAV.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal


@dataclass(frozen=True)
class Plan:
    action: str              # 'none' | 'buy' | 'sell'
    reason: str
    weight_btc: float
    value_usdt: float
    amount_btc: float = 0.0
    price: float = 0.0
    notional_usdt: float = 0.0


def floor_to(x, step):
    return math.floor(x / step + 1e-9) * step


def plan_rebalance(*, btc: float, usdt: float, bid: float, ask: float, target=0.5,
                   band=0.10, min_order_usdt=10.0, max_order_usdt=20.0,
                   max_spread=0.01, amount_step=1e-6, price_tick=0.01) -> Plan:
    """Decide ONE maker limit order that moves the BTC weight toward `target`.

    Trades only when |weight - target| > band. Buys rest at the best bid and
    sells at the best ask (maker side). Size is capped by max_order_usdt, so a
    large imbalance is corrected over several cycles.
    """
    for name, v in (('btc', btc), ('usdt', usdt)):
        if not math.isfinite(v) or v < 0:
            raise ValueError(f'invalid {name} balance')
    if not (0 < bid < ask) or not all(map(math.isfinite, (bid, ask))):
        raise ValueError('invalid order book')
    if not (0 <= target <= 1 and 0 < band < 0.5):
        raise ValueError('invalid target/band')
    if min_order_usdt <= 0 or max_order_usdt < min_order_usdt:
        raise ValueError('invalid order size limits')
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid
    value = btc * mid + usdt
    if value <= 0:
        return Plan('none', 'empty account', 0.0, 0.0)
    w = btc * mid / value
    if abs(w - target) <= band:
        return Plan('none', f'inside band: weight {w:.3f}, target {target}±{band}', w, value)
    if spread > max_spread:
        return Plan('none', f'spread {spread*1e4:.1f}bp above limit; waiting', w, value)
    need_usdt = (target - w) * value                     # + buy BTC, - sell BTC
    side = 'buy' if need_usdt > 0 else 'sell'
    notional = min(abs(need_usdt), max_order_usdt)
    price = floor_to(bid, price_tick) if side == 'buy' else math.ceil(ask / price_tick - 1e-9) * price_tick
    amount = floor_to(notional / price, amount_step)
    if side == 'buy':
        amount = min(amount, floor_to(usdt / price, amount_step))
    else:
        amount = min(amount, floor_to(btc, amount_step))
    notional = amount * price
    if notional < min_order_usdt:
        return Plan('none', f'order {notional:.2f} USDT below minimum {min_order_usdt}', w, value)
    return Plan(side, f'weight {w:.3f} outside {target}±{band}', w, value,
                round(amount, 8), round(price, 8), round(notional, 4))


# ------------------------------------------------------------------ multi-asset
# USDT is the hub: every asset trades in its <ASSET>USDT market.
# Market specs (report 10.5). Source: Nobitex public API documentation (github.com/nobitex/docs-api):
# amount precision / price precision of the BTCUSDT and PAXGUSDT markets and the SmallOrder rule
# (USDT-market orders must be worth MORE than 11 USDT). They are NOT permanent: the exchange can change
# them. The operator can override them in data/bot/markets.json (validated by apply_market_overrides);
# an order the exchange refuses for its size or precision is reported, never retried blindly.
MARKETS = {'btc': {'symbol': 'BTCUSDT', 'amount_step': 1e-6, 'price_tick': 0.01, 'min_notional': 11.0},
           'paxg': {'symbol': 'PAXGUSDT', 'amount_step': 1e-4, 'price_tick': 0.01, 'min_notional': 11.0}}
MARKET_SPECS_META = {'source': 'Nobitex API docs (precisions, SmallOrder > 11 USDT)', 'as_of': '2026-10 (v1.2 values)',
                     'override_file': 'data/bot/markets.json'}
MIN_NOTIONAL_MARGIN = 0.5
_DEFAULT_MARKETS = {a: dict(m) for a, m in MARKETS.items()}


class ValuationIncomplete(ValueError):
    """A held (or targeted) asset has no valid price: the NAV cannot be computed honestly."""

    def __init__(self, assets):
        self.assets = sorted(assets)
        super().__init__('valuation incomplete: no valid price for ' + ', '.join(self.assets))


def _is_pow10(x: float) -> bool:
    if not (isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 1e-12 <= x <= 1e4):
        return False
    e = round(math.log10(x))
    return abs(x - 10.0 ** e) <= 1e-12 * max(1.0, x)


def apply_market_overrides(obj, *, as_of=None) -> dict:
    """Validate and apply operator-supplied market specs {asset: {amount_step, price_tick, min_notional}}.
    Unknown assets or symbols are refused (adding a market needs code review); steps must be powers of
    ten; min_notional must be finite and between 1 and 100000 USDT."""
    if not isinstance(obj, dict):
        raise ValueError('markets override must be an object')
    staged = {a: dict(m) for a, m in _DEFAULT_MARKETS.items()}
    for a, spec in obj.items():
        if a in ('as_of', 'source'):
            continue
        if a not in staged or not isinstance(spec, dict):
            raise ValueError(f'markets override: unsupported asset {a!r}')
        for k, v in spec.items():
            if k in ('amount_step', 'price_tick'):
                if not _is_pow10(v):
                    raise ValueError(f'markets override: {a}.{k} must be a power of ten')
            elif k == 'min_notional':
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 1 <= v <= 100_000:
                    raise ValueError(f'markets override: {a}.min_notional out of range')
            else:
                raise ValueError(f'markets override: unknown field {a}.{k}')
            staged[a][k] = float(v)
    MARKETS.clear()
    MARKETS.update(staged)
    MARKET_SPECS_META['as_of'] = str(obj.get('as_of') or as_of or 'override')
    MARKET_SPECS_META['source'] = str(obj.get('source') or 'operator override')[:120]
    return MARKETS


def reset_markets():
    MARKETS.clear()
    MARKETS.update({a: dict(m) for a, m in _DEFAULT_MARKETS.items()})


def market_min_order(asset: str) -> float:
    """Smallest order value (USDT) the engine will send on this market."""
    return MARKETS[asset]['min_notional'] + MIN_NOTIONAL_MARGIN


def exchange_min_order() -> float:
    return max(market_min_order(a) for a in MARKETS)


def _D(x) -> Decimal:
    return Decimal(str(x))


def exact_order(side: str, *, bid: float, ask: float, want_usdt: float, max_order_usdt: float,
                balance_asset: float, balance_usdt: float, amount_step: float, price_tick: float,
                fee_reserve: float = 0.0):
    """(amount, price, notional) as Decimals for ONE limit order, rounded on the market grid.

    buy: price = best bid floored to the tick; sell: best ask raised to the tick (resting side).
    The amount is floored to the step and never exceeds the available balance (for a buy, the
    USDT balance minus a fee reserve). The notional is never above max_order_usdt. Any non-positive
    or non-finite input gives a zero order (nothing is sent).
    """
    zero = (Decimal(0), Decimal(0), Decimal(0))
    vals = (bid, ask, want_usdt, max_order_usdt, balance_asset, balance_usdt, amount_step, price_tick, fee_reserve)
    if any(isinstance(v, bool) or not isinstance(v, (int, float, Decimal)) or not _D(v).is_finite() for v in vals):
        return zero
    tick, step = _D(price_tick), _D(amount_step)
    if tick <= 0 or step <= 0 or _D(fee_reserve) < 0:
        return zero
    if side == 'buy':
        price = (_D(bid) / tick).to_integral_value(ROUND_FLOOR) * tick
    else:
        price = (_D(ask) / tick).to_integral_value(ROUND_CEILING) * tick
    if price <= 0:
        return zero
    cap = min(_D(want_usdt), _D(max_order_usdt))
    if cap <= 0:
        return zero
    amount = (cap / price / step).to_integral_value(ROUND_DOWN) * step
    if side == 'buy':
        usable = max(Decimal(0), _D(balance_usdt)) / (1 + _D(fee_reserve))
        amount = min(amount, (usable / price / step).to_integral_value(ROUND_DOWN) * step)
    else:
        amount = min(amount, (max(Decimal(0), _D(balance_asset)) / step).to_integral_value(ROUND_DOWN) * step)
    amount = max(amount, Decimal(0))
    return amount, price, amount * price


@dataclass(frozen=True)
class MultiPlan:
    action: str                  # none | buy | sell
    reason: str
    weights: dict
    value_usdt: float
    asset: str = ''
    market: str = ''
    amount: float = 0.0
    price: float = 0.0
    notional_usdt: float = 0.0


def parse_weights(text: str) -> dict:
    w = {}
    for part in text.split(','):
        k, v = part.split('=')
        w[k.strip().lower()] = float(v)
    if 'usdt' not in w:
        w['usdt'] = 1.0 - sum(w.values())
    unknown = set(w) - set(MARKETS) - {'usdt'}
    if unknown:
        raise ValueError(f'unsupported assets: {sorted(unknown)}; supported: {sorted(MARKETS)} + usdt')
    if any(not math.isfinite(v) or v < 0 for v in w.values()) or abs(sum(w.values()) - 1) > 1e-6:
        raise ValueError(f'weights must be finite, >= 0 and sum to 1: {w}')
    for a in MARKETS:                         # an asset not named has target 0 (AVID-03)
        w.setdefault(a, 0.0)
    return w


def full_targets(targets: dict, managed) -> dict:
    """Targets over every managed asset: an asset that is not named gets weight 0 (AVID-03)."""
    if not isinstance(targets, dict) or not targets:
        raise ValueError('targets must be a non-empty object')
    t = {}
    for k, v in targets.items():
        k = str(k).lower()
        if k not in MARKETS and k != 'usdt':
            raise ValueError(f'unsupported asset {k!r} in targets')
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f'target of {k} must be a finite number >= 0')
        t[k] = float(v)
    for a in managed:
        t.setdefault(a, 0.0)
    t.setdefault('usdt', 0.0)
    if abs(sum(t.values()) - 1) > 1e-6:
        raise ValueError(f'targets must sum to 1 (got {sum(t.values()):.6f})')
    return t


def _book_ok(ba) -> bool:
    try:
        bid, ask = float(ba[0]), float(ba[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return False
    return math.isfinite(bid) and math.isfinite(ask) and 0 < bid < ask


def plan_multi(*, balances: dict, books: dict, targets: dict, band_rel=0.25,
               min_order_usdt=10.0, max_order_usdt=20.0, max_spread=0.01, fee_reserve=0.0,
               managed=None, available: dict | None = None) -> MultiPlan:
    """ONE resting limit order per cycle toward `targets` (USDT as hub). The price is the
    resting side of the book (bid for buys, ask for sells) so the order normally executes as
    maker, but Nobitex has no post-only flag: if the book moves before it arrives it can match
    immediately as taker. Fees are therefore estimated at the taker rate.

    balances   TOTAL holdings per asset (they value the portfolio)
    available  free balances per asset (they size the order); defaults to `balances`
    managed    assets the engine watches and manages (default: every supported market). Each one with
               a holding or a target is valued; one without a valid price -> ValuationIncomplete.

    Trades only an asset whose weight is off target by more than band_rel * target
    (any holding of a 0-target asset counts). Overweight assets are SOLD before
    underweight ones are bought, so USDT is freed first.
    """
    managed = sorted((set(MARKETS) if managed is None else set(managed))
                     | {str(a).lower() for a in targets if str(a).lower() != 'usdt'})
    for a in managed:
        if a not in MARKETS:
            raise ValueError(f'unsupported managed asset {a!r}')
    t = full_targets(targets, managed)
    avail = balances if available is None else available
    for a in managed + ['usdt']:
        for src, name in ((balances, 'balance'), (avail, 'available balance')):
            v = src.get(a, 0.0)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f'invalid {a} {name}')
    if not all(math.isfinite(float(x)) for x in (band_rel, min_order_usdt, max_order_usdt, max_spread, fee_reserve)):
        raise ValueError('non-finite planning parameter')
    valued = [a for a in managed if balances.get(a, 0.0) > 0 or t[a] > 0]
    missing = [a for a in valued if a not in books or not _book_ok(books[a])]
    if missing:
        raise ValuationIncomplete(missing)
    mid = {a: (float(books[a][0]) + float(books[a][1])) / 2 for a in valued}
    vals = {a: balances.get(a, 0.0) * mid[a] for a in valued}
    vals['usdt'] = balances.get('usdt', 0.0)
    V = sum(vals.values())
    if V <= 0:
        return MultiPlan('none', 'empty account', {}, 0.0)
    w = {a: vals[a] / V for a in vals}
    rw = {a: round(x, 4) for a, x in w.items()}
    off, too_small = [], []
    for a in valued:
        dev = w[a] - t[a]
        limit = band_rel * t[a] if t[a] > 0 else 0.0
        if abs(dev) > limit:
            if abs(dev) * V >= min_order_usdt:
                off.append((dev > 0, abs(dev) / max(t[a], 1e-9), a, dev))
            else:
                too_small.append(f'{a} {abs(dev) * V:.2f}')
    if not off:
        if too_small:
            return MultiPlan('none', 'outside band but corrections below minimum order '
                             f'{min_order_usdt} USDT: ' + ', '.join(too_small) + ' USDT', rw, V)
        return MultiPlan('none', f'all assets inside ±{int(band_rel*100)}% band', rw, V)
    off.sort(key=lambda x: (not x[0], -x[1]))          # sells first, then largest deviation
    sell, _, a, dev = off[0]
    bid, ask = float(books[a][0]), float(books[a][1])
    spec = MARKETS[a]
    if (ask - bid) / mid[a] > max_spread:
        return MultiPlan('none', f'{spec["symbol"]} spread too wide; waiting', rw, V)
    amount, price, notional = exact_order('sell' if sell else 'buy', bid=bid, ask=ask, want_usdt=abs(dev) * V,
                                          max_order_usdt=max_order_usdt, balance_asset=avail.get(a, 0.0),
                                          balance_usdt=avail.get('usdt', 0.0), amount_step=spec['amount_step'],
                                          price_tick=spec['price_tick'], fee_reserve=fee_reserve)
    if notional < _D(min_order_usdt) or notional <= _D(spec.get('min_notional', 0)):
        return MultiPlan('none', f'{a} order {float(notional):.2f} USDT below minimum {min_order_usdt}', rw, V)
    if notional > _D(max_order_usdt):                    # cannot happen; checked on the final numbers anyway
        return MultiPlan('none', f'{a} order above max_order_usdt', rw, V)
    return MultiPlan('sell' if sell else 'buy',
                     f'{a} weight {w[a]:.3f} vs target {t[a]:.3f}', rw, V,
                     a, spec['symbol'], float(amount), float(price), float(notional))
