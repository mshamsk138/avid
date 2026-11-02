"""Accounting reconciliation statuses (AVID-08, Invariant F)."""
import json
from pathlib import Path

import pytest

from fakes import FakeExchange, make_cfg, net_error
from avid_core import cycle as cyc
from avid_core.cycle import Store, cycle

T0 = 1_700_000_000.0
DAY = 86400


def events(root):
    p = Path(root) / 'data/bot/log.jsonl'
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def run(ex, root, t, cfg=None):
    ex.now = t
    try:
        return cycle(ex, Store(root), cfg or make_cfg(weights={'btc': 0.0, 'paxg': 0.0, 'usdt': 1.0}), live=True, now=t)
    except Exception as e:
        return {'event': 'raised', 'error': repr(e)}


def filled_order(tmp_path, fraction=1.0):
    ex = FakeExchange(now=T0)
    r = cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0)
    oid = int(r['id'])
    ex.fill(oid, ex.orders[oid]['amount'] * fraction, at=T0 + 5)
    if fraction < 1:
        ex.orders[oid]['status'] = 'Canceled'
    return ex, oid


def recon(root, oid):
    return [e for e in events(root) if e['event'] == 'reconciliation' and e['id'] == str(oid)]


def test_reconciled_with_fields(tmp_path):
    ex, oid = filled_order(tmp_path)
    run(ex, tmp_path, T0 + 60)
    r = recon(tmp_path, oid)[-1]
    assert r['reconciliation_status'] == 'RECONCILED' and r['missing_filled_amount'] == 0
    assert r['executions_count'] == 1 and r['pagination_complete'] is True
    assert str(oid) not in Store(tmp_path).load()['fills_pending']


def test_missing_history_stays_pending_then_aggregate_only(tmp_path):
    ex, oid = filled_order(tmp_path)
    ex.trades.clear()                                 # the history does not show the execution
    run(ex, tmp_path, T0 + 60)
    assert Store(tmp_path).load()['fills_pending'][str(oid)]['status'] == 'PENDING'
    run(ex, tmp_path, T0 + 3 * DAY)
    agg = [e for e in events(tmp_path) if e['event'] == 'fill_aggregate']
    assert agg and agg[0]['reconciliation_status'] == 'AGGREGATE_ONLY' and agg[0]['missing_filled_amount'] > 0
    assert not any(e.get('accounting') == 'reconciled' for e in events(tmp_path) if e.get('id') == str(oid))


def test_partial_history_is_partially_reconciled(tmp_path):
    ex, oid = filled_order(tmp_path)
    t = ex.trades[-1]
    half = float(t['amount']) / 2
    t['amount'] = repr(half)
    run(ex, tmp_path, T0 + 60)
    p = Store(tmp_path).load()['fills_pending'][str(oid)]
    assert p['status'] == 'PARTIALLY_RECONCILED' and p['recorded'] == pytest.approx(half)


def test_more_executions_than_filled_is_incomplete(tmp_path):
    ex, oid = filled_order(tmp_path)
    t = dict(ex.trades[-1])
    t['id'] = 9999
    ex.trades.append(t)                               # a second execution record for the same fill
    run(ex, tmp_path, T0 + 60)
    inc = [e for e in events(tmp_path) if e['event'] == 'reconciliation_incomplete']
    assert inc and inc[0]['observed_filled_amount'] > inc[0]['expected_filled_amount']


def test_invalid_trade_record_prevents_reconciled(tmp_path):
    ex, oid = filled_order(tmp_path)
    ex.trades[-1]['amount'] = 'NaN'
    run(ex, tmp_path, T0 + 60)
    assert any(e['event'] == 'trade_record_invalid' for e in events(tmp_path))
    run(ex, tmp_path, T0 + 3 * DAY)
    assert any(e['event'] == 'reconciliation_incomplete' and e['id'] == str(oid) for e in events(tmp_path))


def test_history_api_failure_keeps_it_pending(tmp_path):
    ex, oid = filled_order(tmp_path)
    ex.faults.append(('trades_page', net_error('trades')))
    run(ex, tmp_path, T0 + 60)
    assert any(e['event'] == 'trades_unavailable' for e in events(tmp_path))
    assert str(oid) in Store(tmp_path).load()['fills_pending']
    run(ex, tmp_path, T0 + 120)
    assert str(oid) not in Store(tmp_path).load()['fills_pending']


def test_each_trade_is_logged_once_with_a_stable_event_id(tmp_path):
    ex, oid = filled_order(tmp_path, 0.5)
    run(ex, tmp_path, T0 + 60)
    run(ex, tmp_path, T0 + 120)
    trades = [e for e in events(tmp_path) if e['event'] == 'trade']
    assert len(trades) == 1 and trades[0]['eid'] == f'trade:{ex.trades[-1]["id"]}'


def test_paging_stops_at_trades_older_than_the_order(tmp_path):
    ex, oid = filled_order(tmp_path)
    for i in range(5 * 30):                           # older history that is irrelevant
        ex.trades.insert(0, {'id': 50_000 + i, 'orderId': 1, 'srcCurrency': 'btc', 'dstCurrency': 'usdt', 'type': 'buy',
                             'amount': '0.0001', 'price': '1', 'fee': '0',
                             'timestamp': '2023-11-01T00:00:00+00:00'})
    run(ex, tmp_path, T0 + 60)
    pages = [c for c in ex.calls if c[0] == 'trades_page']
    assert len(pages) <= 2


def test_manual_closed_with_readable_record_later_becomes_exact(tmp_path):
    ex, oid = filled_order(tmp_path, 0.5)
    ex.status_fail.add(oid)
    ex.hide_open.add(oid)
    for k in range(3):
        run(ex, tmp_path, T0 + 60 * (k + 1))
    cyc.resolve_manually(Store(tmp_path), closing_ids=[str(oid)])
    ex.status_fail.discard(oid)                       # the final record is readable again
    run(ex, tmp_path, T0 + 600)
    assert any(e['event'] == 'reconciliation_total_found' for e in events(tmp_path))
    assert recon(tmp_path, oid)[-1]['reconciliation_status'] == 'RECONCILED'
