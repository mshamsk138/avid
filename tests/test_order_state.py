"""Order state machine and settlement (AVID-01, Invariants A and B)."""
import json
from pathlib import Path

import pytest

from fakes import FakeExchange, make_cfg, net_error
from avid_core import orderstate as ost
from avid_core.cycle import Store, cycle, order_numbers

T0 = 1_700_000_000.0


def events(root):
    p = Path(root) / 'data/bot/log.jsonl'
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def run(ex, root, cfg, t, **kw):
    try:
        return cycle(ex, Store(root), cfg, now=t, **({'live': True} | kw))
    except Exception as e:
        return {'event': 'raised', 'error': repr(e)}


def place(tmp_path, **ex_kw):
    ex = FakeExchange(**({'usdt': 100.0} | ex_kw))
    cfg = make_cfg()
    r = cycle(ex, Store(tmp_path), cfg, live=True, now=T0)
    assert r['event'] == 'order_placed'
    return ex, cfg, int(r['id'])


class _S:
    def __init__(self):
        self.logged = []

    def log(self, event, **kw):
        self.logged.append((event, kw))
        return kw


def test_transitions_are_logged_once_and_refused_when_not_allowed():
    s, info = _S(), {'cid': 'c1'}
    assert ost.transition(s, info, ost.SUBMISSION_PENDING)
    assert not ost.transition(s, info, ost.SUBMISSION_PENDING)          # same state: nothing logged
    assert ost.transition(s, info, ost.OPEN)
    assert ost.transition(s, info, ost.FILLED_CONFIRMED)
    assert not ost.transition(s, info, ost.OPEN)                        # terminal: refused
    names = [e for e, _ in s.logged]
    assert names == ['order_state', 'order_state', 'order_state', 'order_state_anomaly']
    assert s.logged[2][1]['eid'] == 'state:c1:FILLED_CONFIRMED'


@pytest.mark.parametrize('patch,why', [
    ({'status': 'Expiring'}, 'unrecognised'),
    ({'status': 'Canceled', 'matchedAmount': None, 'unmatchedAmount': None}, 'neither'),
    ({'status': 'Canceled', 'matchedAmount': '0.0001', 'unmatchedAmount': '0.0001'}, '!='),
    ({'status': 'Done', 'matchedAmount': '0.0001', 'unmatchedAmount': '0.000233'}, 'filled but'),
    ({'status': 'Canceled', 'matchedAmount': '-0.0001'}, 'negative'),
    ({'status': 'Canceled', 'amount': '0.5'}, 'differs'),
])
def test_unclear_final_records_are_unknown_and_release_nothing(tmp_path, patch, why):
    ex, cfg, oid = place(tmp_path)
    reserved = Store(tmp_path).load()['notional_today']
    ex.hide_open.add(oid)
    ex.status_patch[oid] = patch
    run(ex, tmp_path, cfg, T0 + 60)
    st = Store(tmp_path).load()
    assert st['notional_today'] == pytest.approx(reserved)
    assert st['closing'][str(oid)]['state'] == ost.UNKNOWN
    assert ex.add_attempts == 1
    msgs = [e.get('message', '') for e in events(tmp_path) if e['event'] == 'order_status_unknown']
    assert any(why in m for m in msgs), msgs


def test_fill_going_backwards_is_unknown(tmp_path):
    ex, cfg, oid = place(tmp_path)
    ex.fill(oid, ex.orders[oid]['amount'] / 2)
    run(ex, tmp_path, cfg, T0 + 60)                                    # progress recorded while open
    ex.hide_open.add(oid)
    ex.orders[oid]['status'] = 'Canceled'
    ex.status_patch[oid] = {'matchedAmount': '0.0000001', 'unmatchedAmount': repr(ex.orders[oid]['amount'] - 1e-7)}
    run(ex, tmp_path, cfg, T0 + 120)
    assert Store(tmp_path).load()['closing'][str(oid)]['state'] == ost.UNKNOWN


def test_confirmed_partial_cancel_releases_only_the_unfilled_part_once(tmp_path):
    ex, cfg, oid = place(tmp_path)
    o = ex.orders[oid]
    ex.fill(oid, o['amount'] / 4)
    ex.orders[oid]['status'] = 'Canceled'
    run(ex, tmp_path, cfg, T0 + 60)
    st = Store(tmp_path).load()
    expected = o['matched'] * o['price']
    # the next order may already be counted on top; the refund itself is exactly the unfilled part
    finals = [e for e in events(tmp_path) if e['event'] == 'order_final' and e['id'] == str(oid)]
    assert len(finals) == 1 and finals[0]['state'] == ost.PARTIALLY_FILLED_CANCELED
    assert finals[0]['cap_refund_usdt'] == pytest.approx((o['amount'] - o['matched']) * o['price'], abs=1e-4)
    assert st['notional_today'] >= expected - 1e-9
    run(ex, tmp_path, cfg, T0 + 120)
    assert len([e for e in events(tmp_path) if e['event'] == 'order_final' and e['id'] == str(oid)]) == 1


def test_cancel_request_is_not_a_cancellation(tmp_path):
    ex, cfg, oid = place(tmp_path)
    ex.cancel_lag.add(oid)
    (Store(tmp_path).dir / 'STOP').write_text('x')
    run(ex, tmp_path, cfg, T0 + 60)
    st = Store(tmp_path).load()
    assert st['bot_orders'][str(oid)]['state'] == ost.CANCEL_REQUESTED
    assert st['notional_today'] > 0                                     # nothing released yet
    ex.fill(oid, ex.orders[oid]['amount'] / 2)                          # it still fills meanwhile
    ex.apply_cancels()
    run(ex, tmp_path, cfg, T0 + 120)
    fin = [e for e in events(tmp_path) if e['event'] == 'order_final']
    assert fin and fin[-1]['state'] == ost.PARTIALLY_FILLED_CANCELED


def test_stop_also_cancels_an_order_whose_state_is_unknown(tmp_path):
    ex, cfg, oid = place(tmp_path)
    ex.hide_open.add(oid)                     # lists lag and the status cannot be read: UNKNOWN ...
    ex.status_fail.add(oid)
    run(ex, tmp_path, cfg, T0 + 60)
    assert Store(tmp_path).load()['closing'][str(oid)]['state'] == ost.UNKNOWN
    (Store(tmp_path).dir / 'STOP').write_text('x')
    run(ex, tmp_path, cfg, T0 + 120)          # ... but it may still be on the book: STOP reaches it
    assert ex.orders[oid]['status'] == 'Canceled'


def test_exchange_list_error_still_lets_stop_cancel(tmp_path):
    ex, cfg, oid = place(tmp_path)
    (Store(tmp_path).dir / 'STOP').write_text('x')
    ex.faults.append(('open_orders', net_error('open_orders')))
    r = run(ex, tmp_path, cfg, T0 + 60)
    assert r['event'] == 'raised'
    assert ex.orders[oid]['status'] == 'Canceled'


def test_lost_reply_cancelled_by_client_order_id_under_stop(tmp_path):
    ex, cfg = FakeExchange(), make_cfg()
    ex.faults.append(('add', 'lose_reply'))
    run(ex, tmp_path, cfg, T0)
    oid = max(ex.orders)
    ex.hide_open.add(oid)
    ex.hide_all.add(oid)
    ex.cid_feature = False
    (Store(tmp_path).dir / 'STOP').write_text('x')
    run(ex, tmp_path, cfg, T0 + 60)
    assert ex.orders[oid]['status'] == 'Canceled'
    assert Store(tmp_path).load()['pending']['state'] == ost.CANCEL_REQUESTED


def test_definite_rejection_releases_and_records(tmp_path):
    ex, cfg = FakeExchange(), make_cfg()
    ex.faults.append(('add', 'reject:InsufficientBalance'))
    run(ex, tmp_path, cfg, T0)
    st = Store(tmp_path).load()
    assert st['pending'] is None and st['orders_today'] == 0 and st['notional_today'] == 0
    assert any(e['event'] == 'order_state' and e['to_state'] == ost.REJECTED_CONFIRMED for e in events(tmp_path))


@pytest.mark.parametrize('action', ['lose_reply', 'network_before'])
def test_unanswered_placement_keeps_caps_and_blocks(tmp_path, action):
    ex, cfg = FakeExchange(), make_cfg()
    ex.faults.append(('add', action))
    run(ex, tmp_path, cfg, T0)
    st = Store(tmp_path).load()
    assert st['pending'] and st['orders_today'] == 1 and st['notional_today'] > 0
    assert any(e['event'] == 'order_submit_unconfirmed' for e in events(tmp_path))


def test_order_numbers_helper():
    info = {'amount': 0.001, 'asset': 'btc', 'matched': 0.0}
    assert order_numbers({'amount': '0.001', 'matchedAmount': '0.0004'}, info)[0] == pytest.approx((0.001, 0.0004, 0.0006))
    assert order_numbers({'amount': '0.001', 'unmatchedAmount': '0.001'}, info)[0][1] == 0.0
    assert order_numbers({}, info)[0] is None


def _place_and_close(ex, root, cfg, t):
    r = cycle(ex, Store(root), cfg, live=True, now=t)
    assert r['event'] == 'order_placed'
    ex.fill(int(r['id']))
    return int(r['id'])


def test_client_order_id_support_is_verified_before_it_is_relied_on_again(tmp_path):
    ex, cfg = FakeExchange(usdt=300.0), make_cfg()
    _place_and_close(ex, tmp_path, cfg, T0)
    assert Store(tmp_path).load()['cid_verify']                          # first order: not verified yet
    run(ex, tmp_path, cfg, T0 + 60)
    st = Store(tmp_path).load()
    assert st['cid_supported'] is True and st['cid_verify'] is None
    assert any(e['event'] == 'client_order_id_verified' for e in events(tmp_path))
    assert ex.add_attempts == 2


def test_unverifiable_client_order_id_turns_live_off(tmp_path):
    ex, cfg = FakeExchange(usdt=300.0), make_cfg()
    _place_and_close(ex, tmp_path, cfg, T0)
    ex.cid_feature = False                                               # lookup by clientOrderId refused
    run(ex, tmp_path, cfg, T0 + 60)
    st = Store(tmp_path).load()
    assert st['cid_supported'] is False and ex.add_attempts == 1
    assert any(e['event'] == 'config_conflict' and 'clientOrderId' in e['reason'] for e in events(tmp_path))


def test_unanswered_verification_waits(tmp_path):
    ex, cfg = FakeExchange(usdt=300.0), make_cfg()
    _place_and_close(ex, tmp_path, cfg, T0)
    real = ex.order_status

    def flaky(order_id=None, *, client_order_id=None):
        if client_order_id is not None:
            raise net_error('status')
        return real(order_id)
    ex.order_status = flaky
    r = run(ex, tmp_path, cfg, T0 + 60)
    assert r['event'] == 'waiting' and 'not verified' in r['reason'] and ex.add_attempts == 1


def test_other_client_order_id_echo_turns_live_off(tmp_path):
    ex, cfg = FakeExchange(usdt=300.0), make_cfg()
    real = ex.add_limit_order

    def echo_other(*a, **kw):
        rec = real(*a, **kw)
        return {**rec, 'clientOrderId': 'someoneelse'}
    ex.add_limit_order = echo_other
    cycle(ex, Store(tmp_path), cfg, live=True, now=T0)
    st = Store(tmp_path).load()
    assert st['cid_supported'] is False and len(st['bot_orders']) == 1     # still tracked by its id
    assert any(e['event'] == 'client_order_id_mismatch' for e in events(tmp_path))


def test_second_engine_on_the_same_folder_is_refused(tmp_path):
    import fcntl
    from avid_core.cycle import EngineBusy
    s = Store(tmp_path)
    with open(s.dir / '.lock', 'a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EngineBusy):
            cycle(FakeExchange(), Store(tmp_path), make_cfg(), live=True, now=T0)


def test_pure_signer_allowed_only_on_testnet_with_explicit_opt_in(tmp_path, monkeypatch):
    for network, env, placed in (('testnet', '1', True), ('testnet', None, False), ('mainnet', '1', False)):
        ex, cfg = FakeExchange(), make_cfg()
        ex.signer, ex.network = 'pure', network
        if env:
            monkeypatch.setenv('AVID_ALLOW_PURE_ED25519', env)
        else:
            monkeypatch.delenv('AVID_ALLOW_PURE_ED25519', raising=False)
        root = tmp_path / f'{network}-{env}'
        run(ex, root, cfg, T0)
        assert (ex.add_attempts == 1) is placed, (network, env)
