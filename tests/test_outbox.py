"""Outbox classes, storage pressure, acknowledgement by key, migration (AVID-05, AVID-06)."""
import json
import os
import sqlite3

import pytest

from avid_agent.outbox import (CRITICAL, DROPPABLE, DURABLE, Outbox, OutboxError, OutboxFull, classify)


def ev(i, level='info', kind='no_trade'):
    return {'key': f'k{i}', 'at': i, 'kind': kind, 'level': level, 'message': f'm{i}', 'data': {}}


def snap(i):
    return {'key': f's{i}', 'at': i, 'balances': {}, 'books': {}, 'value_usdt': 0}


def test_classification():
    assert classify('snapshots', {}) == DROPPABLE
    assert classify('events', ev(1)) == DROPPABLE
    assert classify('events', ev(1, 'warning')) == DURABLE
    assert classify('events', ev(1, 'error')) == CRITICAL
    assert classify('events', ev(1, 'info', 'order_state')) == CRITICAL
    assert classify('intents', {'mode': 'simulation'}) == DURABLE
    assert classify('intents', {'mode': 'live'}) == CRITICAL
    assert classify('orders', {}) == CRITICAL and classify('fills', {}) == CRITICAL


def test_duplicates_are_ignored_and_ack_is_by_key(tmp_path):
    ob = Outbox(tmp_path / 'o.db')
    assert ob.put('events', ev(1)) == 'stored'
    assert ob.put('events', ev(1)) == 'duplicate'
    ob.put('events', ev(2))
    batch, keys = ob.batch()
    assert len(keys) == 2 and all(e['item_key'] in keys for e in batch['events'])
    ob.ack([keys[0], 'never-sent'])
    assert len(ob) == 1


def test_eviction_order_and_exact_count(tmp_path):
    ob = Outbox(tmp_path / 'o.db', max_items=5)
    for i in range(3):
        ob.put('events', ev(i, 'error'))
    ob.put('snapshots', snap(1))
    ob.put('events', ev(10, 'warning'))
    ob.put('snapshots', snap(2))                 # 6 > 5: exactly one droppable item goes (the oldest)
    batch, _ = ob.batch()
    assert len(ob) == 5
    assert [s['key'] for s in batch['snapshots']] == ['s2']
    assert ob.take_dropped() == {'droppable': 1}
    assert ob.take_dropped() == {}


def test_storage_pressure_keeps_critical_items_and_refuses_low_ones(tmp_path):
    ob = Outbox(tmp_path / 'o.db', max_items=3)
    for i in range(3):
        ob.put('orders', {'exchange_order_id': str(i), 'status': 'open'})
    assert not ob.pressure
    ob.put('orders', {'exchange_order_id': '9', 'status': 'open'})
    assert ob.pressure and len(ob) == 4                 # critical: kept beyond the cap
    assert ob.put('snapshots', snap(1)) == 'dropped'    # low class: refused, counted
    assert ob.put('events', ev(5, 'warning')) == 'dropped'
    for i in range(10, 12):
        ob.put('fills', {'trade_id': str(i)})
    with pytest.raises(OutboxFull):                     # hard limit (2x cap): the caller keeps it in the log
        ob.put('fills', {'trade_id': '99'})
    assert ob.pressure
    _, keys = ob.batch(max_items=10)
    ob.ack(keys)                                        # cloud caught up: pressure clears
    assert len(ob) == 0 and not ob.pressure


def test_durable_items_only_go_after_retention(tmp_path):
    now = [1_000_000.0]
    ob = Outbox(tmp_path / 'o.db', max_items=2, clock=lambda: now[0])
    ob.put('events', ev(1, 'warning'))
    ob.put('events', ev(2, 'warning'))
    ob.put('events', ev(3, 'warning'))
    assert len(ob) == 3 and ob.pressure                 # nothing evictable yet
    now[0] += 31 * 86400
    ob.put('events', ev(4, 'warning'))
    assert len(ob) == 2                                 # old durable items past retention went, exactly enough


def test_oversized_items(tmp_path):
    ob = Outbox(tmp_path / 'o.db')
    assert ob.put('snapshots', {'key': 'big', 'pad': 'x' * 20_000}) == 'dropped'
    assert ob.put('events', {**ev(1, 'error'), 'message': 'y' * 30_000}) == 'stored'
    assert ob.put('orders', {'exchange_order_id': '1', 'status': 'filled', 'note': 'z' * 300_000}) == 'stored'
    batch, _ = ob.batch()
    assert batch['orders'][0]['truncated'] and batch['orders'][0]['exchange_order_id'] == '1'
    assert batch['events'][0]['truncated']


def test_reject_moves_items_to_the_audit_table(tmp_path):
    ob = Outbox(tmp_path / 'o.db')
    ob.put('events', ev(1, 'error'))
    _, keys = ob.batch()
    ob.reject({keys[0]: 'schema'})
    assert len(ob) == 0 and ob.stats()['rejected'] == 1


def test_cursor_is_transactional(tmp_path):
    ob = Outbox(tmp_path / 'o.db')
    with pytest.raises(RuntimeError):
        with ob.tx():
            ob.put('events', ev(1, 'error'))
            ob.set_cursor({'dev': 1, 'ino': 2, 'off': 10})
            raise RuntimeError('crash before commit')
    assert len(ob) == 0 and ob.get_cursor() is None
    with ob.tx():
        ob.put('events', ev(1, 'error'))
        ob.set_cursor({'dev': 1, 'ino': 2, 'off': 10})
    assert len(ob) == 1 and ob.get_cursor() == {'dev': 1, 'ino': 2, 'off': 10}


def test_v12_queue_is_migrated(tmp_path):
    p = tmp_path / 'old.db'
    db = sqlite3.connect(p)
    db.execute('CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, prio INTEGER NOT NULL, '
               'level TEXT NOT NULL DEFAULT "", size INTEGER NOT NULL, payload TEXT NOT NULL)')
    for kind, item in (('orders', {'exchange_order_id': '1'}), ('snapshots', snap(1)), ('events', ev(1, 'error'))):
        t = json.dumps(item)
        db.execute('INSERT INTO items (kind, prio, level, size, payload) VALUES (?,?,?,?,?)', (kind, 0, '', len(t), t))
    db.commit()
    db.close()
    ob = Outbox(p, max_items=2)
    assert len(ob) == 3
    ob.put('snapshots', snap(2))
    batch, keys = ob.batch()
    assert len(batch['orders']) == 1 and len(batch['events']) == 1     # critical legacy items kept
    assert all(k for k in keys)


def test_storage_errors_are_never_success(tmp_path):
    p = tmp_path / 'o.db'
    ob = Outbox(p)
    ob.put('events', ev(1, 'error'))
    ob.db.close()
    with pytest.raises(OutboxError):
        ob.put('events', ev(2, 'error'))
    with pytest.raises(OutboxError):
        len(ob)
    bad = tmp_path / 'not-a-db'
    bad.write_bytes(os.urandom(4096))
    with pytest.raises(OutboxError):
        Outbox(bad)
