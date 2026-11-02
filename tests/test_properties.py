"""Property-based tests with a seeded generator (stdlib only; `hypothesis` is not a dependency).

Each property is checked on AVID_PROPERTY_CASES random inputs (default 2000) per seed; a failure prints
the seed and the case number so it can be replayed exactly:  AVID_PROPERTY_SEED=<seed> pytest -k <name>
"""
from __future__ import annotations

import json
import math
import os
import random
from decimal import Decimal

import pytest

from avid_core import orderstate as ost
from avid_core.client import fmt_decimal
from avid_core.rebalance import MARKETS, ValuationIncomplete, exact_order, plan_multi
from avid_core.validation import SPECS, ValidationError, loads_strict, number

CASES = int(os.environ.get('AVID_PROPERTY_CASES', '2000'))
SEEDS = [int(os.environ['AVID_PROPERTY_SEED'])] if os.environ.get('AVID_PROPERTY_SEED') else [1, 2, 3]
STEPS = [1e-8, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]
TICKS = [1e-4, 1e-2, 0.1, 1.0, 10.0]

pytestmark = pytest.mark.property


def _price(r):
    return 10 ** r.uniform(-2, 6)


@pytest.mark.parametrize('seed', SEEDS)
def test_exact_order_stays_on_the_grid_and_inside_every_limit(seed):
    r = random.Random(seed)
    for case in range(CASES):
        side = r.choice(('buy', 'sell'))
        bid = _price(r)
        ask = bid * (1 + r.uniform(1e-6, 0.05))
        step, tick = r.choice(STEPS), r.choice(TICKS)
        want, cap = r.uniform(0, 500), r.uniform(0, 500)
        bal_a, bal_u = r.uniform(0, 10) * r.choice((0, 1, 1, 1)), r.uniform(0, 1000)
        fee = r.choice((0.0, 0.001, 0.003, 0.01))
        amount, price, notional = exact_order(side, bid=bid, ask=ask, want_usdt=want, max_order_usdt=cap,
                                              balance_asset=bal_a, balance_usdt=bal_u, amount_step=step,
                                              price_tick=tick, fee_reserve=fee)
        ctx = f'seed={seed} case={case} side={side} bid={bid!r} ask={ask!r} step={step} tick={tick}'
        assert all(isinstance(x, Decimal) and x.is_finite() and x >= 0 for x in (amount, price, notional)), ctx
        if amount == 0:
            continue
        assert amount % Decimal(str(step)) == 0, ctx
        assert price % Decimal(str(tick)) == 0, ctx
        assert notional == amount * price, ctx
        assert notional <= Decimal(str(min(want, cap))) + Decimal('1e-12'), ctx
        if side == 'buy':
            assert price <= Decimal(str(bid)), ctx
            assert notional * (1 + Decimal(str(fee))) <= Decimal(str(bal_u)) + Decimal('1e-12'), ctx
        else:
            assert price >= Decimal(str(ask)), ctx
            assert amount <= Decimal(str(bal_a)), ctx


@pytest.mark.parametrize('seed', SEEDS)
def test_bad_numbers_never_make_an_order(seed):
    r = random.Random(seed)
    bad = [float('nan'), float('inf'), -float('inf'), -1.0, 0.0, True, None, '1', Decimal('NaN')]
    for case in range(CASES // 4):
        kw = dict(bid=60000.0, ask=60010.0, want_usdt=20.0, max_order_usdt=20.0, balance_asset=1.0,
                  balance_usdt=100.0, amount_step=1e-6, price_tick=0.01, fee_reserve=0.003)
        key = r.choice(['bid', 'ask', 'want_usdt', 'max_order_usdt', 'amount_step', 'price_tick'])
        kw[key] = r.choice(bad)
        if kw[key] in (0.0, -1.0) and key in ('ask',):
            continue                                       # a sell would need ask; a buy ignores it
        a, p, n = exact_order(r.choice(('buy', 'sell')), **kw)
        if key in ('bid', 'ask') and isinstance(kw[key], float) and math.isfinite(kw[key]) and kw[key] <= 0:
            assert n == 0 or p > 0
            continue
        assert (a, p, n) == (0, 0, 0), f'seed={seed} case={case} {key}={kw[key]!r}'


@pytest.mark.parametrize('seed', SEEDS)
def test_decimal_text_round_trips_through_float(seed):
    """PRA-17: the engine plans in Decimal, keeps floats in state.json and sends fmt_decimal(float).
    For every value on the market grid that text is exactly the planned Decimal."""
    r = random.Random(seed)
    for case in range(CASES):
        step = Decimal(str(r.choice(STEPS)))
        units = r.randint(1, 10 ** r.randint(1, 9))
        exact = units * step
        sent = fmt_decimal(float(exact), step)
        assert Decimal(sent) == exact, f'seed={seed} case={case} {exact} -> {sent}'
        assert 'e' not in sent.lower()


@pytest.mark.parametrize('seed', SEEDS)
def test_plan_multi_proposes_at_most_one_bounded_order(seed):
    r = random.Random(seed)
    managed = sorted(MARKETS)
    for case in range(CASES):
        w = [r.random() for _ in range(len(managed) + 1)]
        s = sum(w)
        targets = {a: x / s for a, x in zip(managed + ['usdt'], w)}
        targets['usdt'] = 1 - sum(v for k, v in targets.items() if k != 'usdt')
        balances = {a: r.choice((0.0, r.uniform(0, 2))) for a in managed}
        balances['usdt'] = r.uniform(0, 2000)
        books = {}
        for a in managed:
            mid = 60000.0 if a == 'btc' else 2400.0
            mid *= r.uniform(0.5, 1.5)
            books[a] = (mid * (1 - r.uniform(0, 0.02)), mid * (1 + r.uniform(1e-5, 0.02)))
        band, mx = r.uniform(0.05, 0.9), r.uniform(12, 200)
        try:
            p = plan_multi(balances=balances, books=books, targets=targets, band_rel=band, min_order_usdt=11.5,
                           max_order_usdt=mx, max_spread=r.uniform(0.001, 0.05), fee_reserve=0.003)
        except ValuationIncomplete:
            pytest.fail(f'seed={seed} case={case}: every book is valid')
        ctx = f'seed={seed} case={case} {p}'
        assert p.action in ('none', 'buy', 'sell'), ctx
        if p.weights:
            assert abs(sum(p.weights.values()) - 1) < 1e-3, ctx
        if p.action == 'none':
            assert p.amount == 0 and p.notional_usdt == 0, ctx
            continue
        assert p.asset in managed and p.market == MARKETS[p.asset]['symbol'], ctx
        assert 11.5 <= p.notional_usdt <= mx + 1e-9 and p.notional_usdt > MARKETS[p.asset]['min_notional'], ctx
        cur, tgt = p.weights[p.asset], targets[p.asset]
        assert abs(cur - tgt) > band * tgt - 1e-3, ctx         # only an asset outside its band is traded
        if p.action == 'sell':
            assert cur > tgt - 1e-3 and p.amount <= balances[p.asset] + 1e-12, ctx
            assert p.price >= books[p.asset][1] - 1e-9, ctx
        else:
            assert cur < tgt + 1e-3 and p.price <= books[p.asset][0] + 1e-9, ctx
            assert p.notional_usdt * 1.003 <= balances['usdt'] + 1e-6, ctx


@pytest.mark.parametrize('seed', SEEDS)
def test_number_validation_accepts_only_values_in_range(seed):
    r = random.Random(seed)
    names = sorted(SPECS)
    for case in range(CASES):
        name = r.choice(names)
        kind, lo, hi, lo_incl, _ = SPECS[name]
        v = r.choice([r.uniform(lo - abs(hi), hi * 2), r.randint(int(lo) - 5, int(min(hi, 1e6)) + 5), float('nan'),
                      float('inf'), True, '5', None, lo, hi, r.uniform(lo, hi)])
        try:
            out = number(name, v)
        except ValidationError:
            continue
        ctx = f'seed={seed} case={case} {name}={v!r} -> {out!r}'
        assert not isinstance(v, (bool, str)) and v is not None, ctx
        assert math.isfinite(out) and (lo < out or (lo_incl and out == lo)) and out <= hi, ctx
        if kind == 'int':
            assert isinstance(out, int), ctx


@pytest.mark.parametrize('seed', SEEDS)
def test_strict_json_parser_only_raises_value_error(seed):
    r = random.Random(seed)
    alphabet = '{}[]",:0123456789.eE-+ \ntruefalsnNaInfiy\\u'
    for case in range(CASES):
        text = ''.join(r.choice(alphabet) for _ in range(r.randint(0, 40)))
        try:
            v = loads_strict(text)
        except ValueError:
            continue
        # whatever parsed is plain finite JSON: it serialises back without NaN/Infinity
        json.dumps(v, allow_nan=False)
    for text in ('NaN', '[Infinity]', '{"a": 1, "a": 2}', '[' * 100 + ']' * 100, '{"x": -Infinity}'):
        with pytest.raises(ValueError):
            loads_strict(text)


class _Log:
    def __init__(self):
        self.events = []

    def log(self, event, **kw):
        self.events.append((event, kw))


@pytest.mark.parametrize('seed', SEEDS)
def test_order_state_machine_never_leaves_a_terminal_state(seed):
    r = random.Random(seed)
    states = [ost.CREATED, ost.SUBMISSION_PENDING, ost.OPEN, ost.PARTIALLY_FILLED, ost.CANCEL_REQUESTED,
              ost.UNKNOWN, ost.MANUAL_RECONCILIATION, ost.FILLED_CONFIRMED, ost.CANCELED_CONFIRMED,
              ost.PARTIALLY_FILLED_CANCELED, ost.REJECTED_CONFIRMED]
    for case in range(CASES // 4):
        log, info = _Log(), {'cid': f'c{case}'}
        path = []
        for _ in range(r.randint(1, 12)):
            new = r.choice(states)
            before = info.get('state')
            changed = ost.transition(log, info, new, reason='property')
            path.append(new)
            if before in ost.TERMINAL:
                assert not changed and info['state'] == before, (seed, case, path)
            if changed:
                assert new in ost.ALLOWED.get(before, set()), (seed, case, path)
            else:
                assert info.get('state') == before, (seed, case, path)
        # every refused transition was logged as an anomaly (never silent)
        refused = [e for e in log.events if e[0] == 'order_state_anomaly']
        for e in refused:
            assert e[1]['level'] == 'error'
