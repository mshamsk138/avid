"""A hostile or broken cloud against the agent (audit 18, Invariants D and E).

Random sync answers - valid, garbage, someone else's batch, partial or contradictory ACKs, NaN or
out-of-range policies, huge caps, live mode with and without local permission, kill switch flapping,
revocation - while the exchange fills and the local ceilings change. After every step:

  E  no order above the LOCAL per-order cap, no more than the LOCAL daily caps, no order at all while
     the local ceilings forbid live trading
  D  an item disappears from the outbox only if the cloud acknowledged it in a VALID answer
"""
from __future__ import annotations

import collections
import datetime as dt
import os
import random

import pytest

from fakes import FakeCloud, FakeExchange, directive, policy
from avid_agent.cloud import CloudError
from avid_agent.engine import Agent

T0 = 1_700_000_000.0
STEPS = int(os.environ.get('AVID_AGENT_FUZZ_STEPS', '400'))
KINDS = ('snapshots', 'intents', 'orders', 'fills', 'events')


def day(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%d')


@pytest.mark.parametrize('seed', range(3))
def test_hostile_cloud_never_widens_local_limits(tmp_path, seed):
    r = random.Random(7000 + seed)
    ex = FakeExchange(usdt=500.0, btc=0.002, now=T0)
    ceil = {'live_allowed': True, 'max_order_usdt': 15.0, 'max_orders_per_day': 3, 'max_daily_usdt': 40.0}
    cloud = FakeCloud(directive=directive('live'))
    valid_acked: set[str] = set()
    created_day = {}
    real_add = ex.add_limit_order

    def add(*a, **kw):
        before = set(ex.orders)
        out = None
        try:
            out = real_add(*a, **kw)
            return out
        finally:
            for oid in set(ex.orders) - before:
                created_day[oid] = (day(ex.now), ceil['live_allowed'])
    ex.add_limit_order = add

    def respond(rep):
        keys = [it['item_key'] for k in KINDS for it in rep.get(k) or []]
        mode = r.choice(['ok', 'ok', 'ok', 'partial', 'garbage', 'other_batch', 'unknown_key', 'both', 'nan_policy',
                         'huge_caps', 'bad_types', 'error'])
        pol = policy(max_order_usdt=r.choice([15, 20, 500]), max_daily_usdt=r.choice([40, 100, 5000]),
                     max_orders_per_day=r.choice([3, 6, 100]), max_spread=r.choice([0.005, 0.01, 0.05]),
                     weights=r.choice([{'btc': 0.5, 'usdt': 0.5}, {'btc': 0.9, 'usdt': 0.1}, {'usdt': 1.0}]))
        d = directive(r.choice(['live', 'live', 'simulation']), kill=r.random() < 0.1, halt=r.random() < 0.05, pol=pol)
        resp = {'protocol': 2, 'agent_id': cloud.agent_id, 'batch_id': rep['batch_id'],
                'ack': {'accepted': list(keys), 'rejected': []}, 'directive': d}
        if mode == 'partial':
            resp['ack']['accepted'] = keys[: len(keys) // 2]
        elif mode == 'garbage':
            return r.choice([{'error': 'unexpected result'}, [], 'ok', None, {'protocol': 2}])
        elif mode == 'other_batch':
            resp['batch_id'] = 'x' + str(rep['batch_id'])
        elif mode == 'unknown_key':
            resp['ack']['accepted'] = keys + ['never-sent']
        elif mode == 'both' and keys:
            resp['ack']['rejected'] = [{'item_key': keys[0], 'reason': 'x'}]
        elif mode == 'nan_policy':
            d['portfolio']['policy']['max_daily_usdt'] = float('nan')
        elif mode == 'huge_caps':
            d['portfolio']['policy'].update(max_order_usdt=1e9, max_daily_usdt=1e12)
        elif mode == 'bad_types':
            d['portfolio']['kill_switch'] = 'no'
        elif mode == 'error':
            raise r.choice([CloudError('cloud unreachable'), CloudError('cloud HTTP 500', status=500),
                            CloudError('cloud HTTP 413', status=413)])
        # what a correct agent may delete: accepted keys of a VALID ack only
        if mode in ('ok', 'partial', 'nan_policy', 'huge_caps', 'bad_types'):
            valid_acked.update(resp['ack']['accepted'])
        return resp
    cloud.respond = respond
    clock = [T0]
    a = Agent(cloud, ex, tmp_path, lambda: dict(ceil), clock=lambda: clock[0])
    violations = []
    for i in range(STEPS):
        before = {it for it in _keys(a)}
        try:
            a.step()
        except Exception as e:
            violations.append(f'step {i}: step raised {type(e).__name__}: {e}')
        after = set(_keys(a))
        lost = before - after - valid_acked
        if lost:
            violations.append(f'step {i}: D {len(lost)} item(s) deleted without a valid ACK')
        per_day = collections.defaultdict(list)
        for oid, (d_, allowed) in created_day.items():
            o = ex.orders[oid]
            per_day[d_].append(o)
            if not allowed:
                violations.append(f'step {i}: E order {oid} while live_allowed=false')
            if o['amount'] * o['price'] > 15.0 + 1e-6:
                violations.append(f'step {i}: E order {oid} of {o["amount"] * o["price"]:.2f} > local 15')
        for d_, orders in per_day.items():
            if len(orders) > 3:
                violations.append(f'step {i}: E {len(orders)} orders on {d_} > local 3')
            committed = sum((o['matched'] + ((o['amount'] - o['matched']) if o['status'] == 'Active' else 0)) * o['price']
                            for o in orders)
            if committed > 40.0 + 1e-6:
                violations.append(f'step {i}: E {committed:.2f} USDT committed on {d_} > local 40')
        created_day = {k: (v[0], True) for k, v in created_day.items()}  # report each permission breach once
        for o in ex.open_bot_orders():
            if r.random() < 0.5:
                ex.fill(o['id'])
        if r.random() < 0.1:
            ceil['live_allowed'] = not ceil['live_allowed']
        if i > STEPS * 0.9 and r.random() < 0.1:          # revocation late in the run (it is permanent)
            cloud.fail = CloudError('cloud HTTP 401', status=401, code='agent_revoked')
        clock[0] += r.choice([600, 600, 3600, 6 * 3600])
        ex.now = clock[0]
        if len(violations) > 10:
            break
    cloud.fail = None
    assert not violations, '\n'.join(violations[:10])
    assert created_day, 'the hostile cloud never let the agent trade at all (weak run)'


def _keys(a):
    _, keys = a.outbox.batch(max_items=100_000, max_bytes=10**12)
    return keys
