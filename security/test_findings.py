"""Executable proofs for the findings of the v1.2 technical report (AVID-01..08) and of the
security-audit checklist (agent and engine side).

Every test states the SAFE behaviour. Run against v1.2 the tests marked "reproduces" FAIL (that is the
proof of the defect); run against this version they must all pass:

    python -m pytest -q security/                      # this version: all pass
    AVID_SRC=/path/to/v1.2 python -m pytest -q security/   # v1.2: the reproduced defects fail

Only simulated exchanges and clouds are used (tests/fakes.py). No network, no real money.
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import sys
import time
from pathlib import Path

import pytest

from fakes import LIVE_CEILINGS, FakeCloud, FakeExchange, directive, make_cfg, policy
from avid_core import cycle as cyc
from avid_core.cycle import Store, cycle

T0 = 1_700_000_000.0            # 2023-11-14 22:13:20 UTC
SRC = Path(os.environ.get('AVID_SRC') or Path(__file__).resolve().parents[1])


def _events(root):
    p = Path(root) / 'data/bot/log.jsonl'
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def _run(ex, root, cfg, t, live=True):
    try:
        return cycle(ex, Store(root), cfg, live=live, now=t)
    except Exception as e:          # an exchange error in one cycle is reported, the next cycle continues
        return {'event': 'raised', 'error': repr(e)}


def _agent(cloud, ex, root, ceil=None, **kw):
    from avid_agent.engine import Agent
    return Agent(cloud, ex, Path(root), (lambda: dict(ceil or LIVE_CEILINGS)), **kw)


# ---------------------------------------------------------------- AVID-01 (P0)
def test_F01_unknown_order_status_never_releases_the_reservation(tmp_path):
    """reproduces AVID-01: an unrecognised status with unmatchedAmount>0 was treated as 'canceled',
    the reserved notional was released and a second order was placed while the first was still live."""
    ex, cfg = FakeExchange(usdt=100.0), make_cfg()
    r = cycle(ex, Store(tmp_path), cfg, live=True, now=T0)
    assert r['event'] == 'order_placed'
    oid = int(r['id'])
    reserved = Store(tmp_path).load()['notional_today']
    ex.hide_open.add(oid)                                   # left the open list (lag) ...
    ex.status_patch[oid] = {'status': 'Expiring'}           # ... and the status record is not understood
    _run(ex, tmp_path, cfg, T0 + 60)
    _run(ex, tmp_path, cfg, T0 + 120)
    st = Store(tmp_path).load()
    assert st['notional_today'] >= reserved - 1e-9, 'reservation released without a confirmed final state'
    assert ex.add_attempts == 1, 'a new order was sent while the previous one was ambiguous'
    assert len(ex.open_bot_orders()) == 1


# ---------------------------------------------------------------- AVID-02 (P0)
@pytest.mark.parametrize('field,value', [
    ('max_daily_usdt', 'NaN'), ('max_daily_usdt', 'Infinity'), ('max_daily_usdt', '-Infinity'),
    ('max_daily_usdt', '1e309'), ('max_order_usdt', 'NaN'), ('max_orders_per_day', '6.7'),
    ('max_orders_per_day', 'NaN'), ('max_daily_usdt', 'true'), ('max_daily_usdt', '"20"'),
    ('max_daily_usdt', '-1'), ('max_daily_usdt', '0')])
def test_F02_invalid_local_ceiling_is_rejected(tmp_path, monkeypatch, field, value):
    """reproduces AVID-02 for NaN / Infinity / 1e309 / 6.7 (accepted by v1.2)."""
    from avid_agent import config
    monkeypatch.setattr(config, 'HOME', tmp_path)
    base = {'live_allowed': True, 'max_order_usdt': 20, 'max_orders_per_day': 6, 'max_daily_usdt': 100}
    text = json.dumps(base).replace(f'"{field}": {json.dumps(base[field])}', f'"{field}": {value}')
    (tmp_path / 'ceilings.json').write_text(text)
    os.chmod(tmp_path / 'ceilings.json', 0o600)
    with pytest.raises((config.ConfigError, ValueError)):
        config.load_ceilings()


def test_F02b_control_file_rejects_non_finite_risk_parameters():
    """reproduces AVID-02 (cloud/control path): max_spread / min_cash_usdt were never validated."""
    from avid_core import control
    for bad in ({'max_spread': float('nan')}, {'min_cash_usdt': float('nan')}, {'max_spread': 5.0}):
        with pytest.raises(ValueError):
            control.validate(bad)


# ---------------------------------------------------------------- AVID-03 (P1)
def test_F03_asset_removed_from_targets_stays_in_the_portfolio_value():
    """reproduces AVID-03: PAXG left out of the targets vanished from the NAV and was never sold."""
    from avid_core.rebalance import plan_multi
    p = plan_multi(balances={'btc': 0.001, 'paxg': 0.025, 'usdt': 60.0},
                   books={'btc': (59990.0, 60010.0), 'paxg': (2399.0, 2401.0)},
                   targets={'btc': 0.5, 'usdt': 0.5}, min_order_usdt=11.5, max_order_usdt=20.0)
    assert abs(p.value_usdt - 180.0) < 0.5, f'NAV {p.value_usdt} ignores the PAXG holding'
    assert (p.action, p.asset) == ('sell', 'paxg')


# ---------------------------------------------------------------- AVID-04 (P1)
def test_F04_log_rotation_loses_no_event(tmp_path):
    """reproduces AVID-04: after a rotation the new file grew past the old offset and the agent
    started reading in the middle of it."""
    agent = _agent(FakeCloud(), FakeExchange(), tmp_path)
    st = agent.store
    for i in range(20):
        st.log('waiting', reason=f'old-{i:03d}')
    agent._collect_log()
    st.log_path.replace(st.log_path.with_suffix('.jsonl.1'))
    for i in range(40):
        st.log('waiting', reason=f'new-{i:03d}')
    agent._collect_log()
    batch, _ = agent.outbox.batch(100_000, 10**9)
    seen = {e.get('message') for e in batch['events']}
    missing = [f'new-{i:03d}' for i in range(40) if f'new-{i:03d}' not in seen]
    assert not missing, f'{len(missing)} events after the rotation were never queued'


# ---------------------------------------------------------------- AVID-05 (P1)
def _err(i):
    return {'key': f'e{i}', 'at': i, 'kind': 'error', 'level': 'error', 'message': f'error {i}', 'data': {}}


def test_F05_full_queue_never_drops_error_events(tmp_path):
    """reproduces AVID-05: with a cap of one item both error events were deleted."""
    from avid_agent.outbox import Outbox
    ob = Outbox(tmp_path / 'o.sqlite3', max_items=1)
    ob.put('events', _err(1))
    ob.put('events', _err(2))
    assert len(ob) == 2


def test_F05b_eviction_removes_only_what_is_needed(tmp_path):
    """reproduces AVID-05: one item over the cap deleted up to 500 items."""
    from avid_agent.outbox import Outbox
    ob = Outbox(tmp_path / 'o.sqlite3', max_items=10)
    for i in range(11):
        ob.put('snapshots', {'key': f's{i}', 'at': i, 'balances': {}, 'books': {}, 'value_usdt': 0})
    assert len(ob) == 10


def test_F16_oversized_order_record_is_never_dropped_silently(tmp_path):
    """reproduces (audit 4.10/58): an order record over 16 KB was discarded without a trace."""
    from avid_agent.outbox import Outbox
    ob = Outbox(tmp_path / 'o.sqlite3')
    ob.put('orders', {'exchange_order_id': '1', 'intent_key': 'oid:1', 'status': 'filled', 'note': 'x' * 20_000})
    assert len(ob) == 1


# ---------------------------------------------------------------- AVID-06 (P1)
def test_F06_http_200_without_a_valid_ack_deletes_nothing(tmp_path):
    """reproduces AVID-06: {"error": "unexpected result"} with HTTP 200 emptied the queue."""
    cloud = FakeCloud()
    cloud.respond = lambda report: {'error': 'unexpected result'}
    agent = _agent(cloud, FakeExchange(), tmp_path)
    agent.outbox.put('events', {'key': 'k-important', 'at': 1, 'kind': 'error', 'level': 'error',
                                'message': 'must reach the cloud', 'data': {}})
    res = agent.step(now=T0)
    batch, _ = agent.outbox.batch(1000, 10**9)
    assert any(e.get('message') == 'must reach the cloud' for e in batch['events'])
    assert res['fresh'] is False


def test_F06b_partial_ack_deletes_only_acknowledged_items(tmp_path):
    """reproduces AVID-06: every sent item was deleted, whatever the cloud said it stored."""
    cloud = FakeCloud()

    def partial(report):
        keys = [e['item_key'] for e in report.get('events') or [] if e.get('item_key')]
        stored = [k for k in keys if k.endswith(':e1')]          # the cloud stored only the first event
        resp = {'protocol': 2, 'agent_id': cloud.agent_id, 'batch_id': report.get('batch_id'),
                'ack': {'accepted': stored, 'rejected': []}, 'directive': cloud.directive}
        resp.update(cloud.directive)
        return resp
    cloud.respond = partial
    agent = _agent(cloud, FakeExchange(), tmp_path)
    for i in (1, 2):
        agent.outbox.put('events', _err(i))
    agent._sync(dict(LIVE_CEILINGS), T0)
    batch, _ = agent.outbox.batch(1000, 10**9)
    assert [e['message'] for e in batch['events']] == ['error 2']


# ---------------------------------------------------------------- AVID-07 (needs real validation)
def test_F07_lost_reply_never_leads_to_a_second_order(tmp_path):
    """regression (v1.2 already safe): unanswered placement + lagging lists + no status-by-cid."""
    ex, cfg = FakeExchange(), make_cfg()
    ex.faults.append(('add', 'lose_reply'))
    _run(ex, tmp_path, cfg, T0)
    oid = max(ex.orders)
    ex.hide_open.add(oid)
    ex.hide_all.add(oid)
    ex.cid_feature = False
    for k in range(5):
        _run(ex, tmp_path, cfg, T0 + 60 * (k + 1))
    assert ex.add_attempts == 1


def test_F25_lost_reply_order_is_found_by_client_order_id_and_accounted(tmp_path):
    """reproduces: v1.2 looked for a lost-reply order only in the two lists; when they lagged, the order
    (already filled) was never adopted, and the only way out was a manual 'not placed' - its fill was
    never accounted."""
    ex, cfg = FakeExchange(usdt=300.0), make_cfg()
    ex.faults.append(('add', 'lose_reply_fill'))
    _run(ex, tmp_path, cfg, T0)
    oid = max(ex.orders)
    ex.hide_all.add(oid)
    for k in range(4):
        _run(ex, tmp_path, cfg, T0 + 60 * (k + 1))
    trades = [e for e in _events(tmp_path) if e.get('event') == 'trade' and e.get('id') == str(oid)]
    assert trades, 'the executed lost-reply order never reached the accounting'


def test_F25b_resolve_not_placed_refuses_when_the_order_exists(tmp_path):
    """reproduces (audit 45): 'resolve --not-placed' cleared the placement without looking at the exchange."""
    ex, cfg = FakeExchange(usdt=300.0), make_cfg()
    ex.faults.append(('add', 'lose_reply'))
    _run(ex, tmp_path, cfg, T0)
    oid = max(ex.orders)
    ex.hide_open.add(oid)
    ex.hide_all.add(oid)
    kwargs = {'pending_not_placed': True}
    if 'client' in inspect.signature(cyc.resolve_manually).parameters:
        kwargs['client'] = ex
    try:
        cyc.resolve_manually(Store(tmp_path), **kwargs)
    except cyc.ManualResolution:
        pass
    assert Store(tmp_path).load().get('pending'), 'pending placement cleared although the order exists'


# ---------------------------------------------------------------- AVID-08 (P1)
def test_F08_bot_trade_beyond_ten_pages_is_still_reconciled(tmp_path):
    """reproduces AVID-08: only 10 pages were read, so a bot trade behind 12 pages of newer trades was
    never seen inside the history window."""
    ex, cfg = FakeExchange(), make_cfg()
    r = cycle(ex, Store(tmp_path), cfg, live=True, now=T0)
    oid = int(r['id'])
    ex.fill(oid, at=T0 + 10)
    for i in range(12 * 30):
        ex.foreign_trade(at=T0 + 20 + i)
    _run(ex, tmp_path, cfg, T0 + 600)
    _run(ex, tmp_path, cfg, T0 + 1200)
    assert str(oid) not in Store(tmp_path).load().get('fills_pending', {})
    assert any(e.get('event') == 'trade' and e.get('id') == str(oid) for e in _events(tmp_path))


def test_F08b_unknown_filled_amount_is_never_reported_as_reconciled(tmp_path):
    """reproduces AVID-08: an order settled by hand (filled amount unknown) became
    'reconciled_from_history' after the window although the history may be incomplete."""
    ex, cfg = FakeExchange(), make_cfg()
    r = cycle(ex, Store(tmp_path), cfg, live=True, now=T0)
    oid = int(r['id'])
    ex.fill(oid, amount=ex.orders[oid]['amount'] / 2, at=T0 + 5)
    ex.orders[oid]['status'] = 'Canceled'
    ex.status_fail.add(oid)
    for k in range(8):
        _run(ex, tmp_path, cfg, T0 + 60 * (k + 1))
    cyc.resolve_manually(Store(tmp_path), closing_ids=[str(oid)])
    _run(ex, tmp_path, cfg, T0 + 3 * 86400)
    claims = [e for e in _events(tmp_path) if e.get('id') == str(oid)
              and e.get('accounting') in ('reconciled', 'reconciled_from_history')]
    assert not claims, claims


# ---------------------------------------------------------------- numeric / state integrity (audit 21, 36, 40)
def test_F09_nan_in_state_file_cannot_disable_the_daily_cap(tmp_path):
    """reproduces (audit 21/36): json accepts NaN; notional_today=NaN made every cap comparison False."""
    ex, cfg = FakeExchange(usdt=300.0), make_cfg(max_daily_usdt=30.0)
    _run(ex, tmp_path, make_cfg(weights={'btc': 0.0, 'paxg': 0.0, 'usdt': 1.0}), T0)
    p = Store(tmp_path).state_path
    st = json.loads(p.read_text())
    st['notional_today'] = float('nan')
    p.write_text(json.dumps(st))
    for k in range(4):
        _run(ex, tmp_path, cfg, T0 + 60 * (k + 1))
        for o in ex.open_bot_orders():
            ex.fill(o['id'])
    total = sum(o['amount'] * o['price'] for o in ex.bot_orders())
    assert total <= 30.0 + 1e-6, f'{total:.2f} USDT sent with a daily cap of 30'


def test_F17_clock_going_back_does_not_reset_daily_caps(tmp_path):
    """reproduces (audit 41): a clock that went back one day reset the day's counters, twice."""
    ex, cfg = FakeExchange(usdt=300.0), make_cfg(max_orders_per_day=1)
    for t in (T0, T0 + 60, T0 - 86400, T0 - 86340, T0 + 120, T0 + 180):
        _run(ex, tmp_path, cfg, t)
        for o in ex.open_bot_orders():
            ex.fill(o['id'])
    assert len(ex.bot_orders()) == 1


def test_F18_deleted_state_file_does_not_reset_daily_caps(tmp_path):
    """reproduces (audit 36): deleting state.json gave the day's caps back."""
    ex, cfg = FakeExchange(usdt=300.0), make_cfg(max_orders_per_day=1)
    _run(ex, tmp_path, cfg, T0)
    for o in ex.open_bot_orders():
        ex.fill(o['id'])
    _run(ex, tmp_path, cfg, T0 + 60)
    Store(tmp_path).state_path.unlink()
    _run(ex, tmp_path, cfg, T0 + 120)
    _run(ex, tmp_path, cfg, T0 + 180)
    assert len(ex.bot_orders()) == 1


# ---------------------------------------------------------------- trust boundary cloud -> agent (audit 18, 10.2)
def test_F10_cloud_nan_max_spread_cannot_disable_the_spread_check(tmp_path):
    """reproduces (audit 18/21): a NaN max_spread from the cloud switched the spread check off."""
    ex = FakeExchange(books={'btc': (60000.0, 63000.0), 'paxg': (2400.0, 2401.0)})      # 5 % spread
    agent = _agent(FakeCloud(directive=directive('live', pol=policy(max_spread=float('nan')))), ex, tmp_path)
    for k in range(3):
        agent.step(now=T0 + 600 * k)
    assert ex.add_attempts == 0


def test_F10b_cloud_nan_min_cash_cannot_disable_the_cash_reserve(tmp_path):
    """reproduces (audit 18/21): a NaN min_cash_usdt disabled the cash-reserve check."""
    ex = FakeExchange(usdt=100.0)
    agent = _agent(FakeCloud(directive=directive('live', pol=policy(min_cash_usdt=float('nan')))), ex, tmp_path)
    agent.step(now=T0)
    assert ex.add_attempts == 0


def test_F30_cloud_cannot_widen_the_local_spread_limit(tmp_path):
    """reproduces (audit 18, Invariant E): a valid but loose cloud max_spread (5 %) let orders rest on a
    3 % spread although the local default is 1 %."""
    ex = FakeExchange(books={'btc': (60000.0, 61800.0), 'paxg': (2400.0, 2401.0)})
    agent = _agent(FakeCloud(directive=directive('live', pol=policy(max_spread=0.05))), ex, tmp_path)
    agent.step(now=T0)
    assert ex.add_attempts == 0


def test_F28_cloud_cannot_enable_live_without_local_permission(tmp_path):
    """regression (Invariant E)."""
    ex = FakeExchange()
    agent = _agent(FakeCloud(directive=directive('live')), ex, tmp_path, ceil={**LIVE_CEILINGS, 'live_allowed': False})
    for k in range(3):
        agent.step(now=T0 + 600 * k)
    assert ex.add_attempts == 0


def test_F29_cloud_cannot_raise_local_caps(tmp_path):
    """regression (Invariant E): cloud max_order_usdt 1000 vs local 20."""
    ex = FakeExchange(usdt=10_000.0)
    agent = _agent(FakeCloud(directive=directive('live', pol=policy(max_order_usdt=1000, max_daily_usdt=5000))),
                   ex, tmp_path)
    agent.step(now=T0)
    assert ex.bot_orders() and all(o['amount'] * o['price'] <= 20.0 + 1e-6 for o in ex.bot_orders())


def test_F11_corrupt_or_deleted_control_file_never_widens_permissions(tmp_path):
    """reproduces (report 10.2): an invalid control file was ignored and the CLI ceiling (--live) applied."""
    from avid_core import control
    ex, cfg = FakeExchange(), make_cfg()
    control.save(tmp_path, {'mode': 'dry'})
    r = cycle(ex, Store(tmp_path), cfg, live=True, now=T0)
    assert r['event'] == 'dry_run_would_place'
    control.control_path(tmp_path).write_text('{"mode": "dry", ')
    _run(ex, tmp_path, cfg, T0 + 60)
    control.control_path(tmp_path).unlink()
    _run(ex, tmp_path, cfg, T0 + 120)
    assert ex.add_attempts == 0


def test_F12_cloud_never_removes_a_manual_stop(tmp_path):
    """reproduces (audit 18): STOP was shared; a 'touch STOP' while the cloud kill switch was on was
    deleted when the cloud switched it off."""
    cloud = FakeCloud(directive=directive('live', kill=True))
    agent = _agent(cloud, FakeExchange(), tmp_path)
    agent.step(now=T0)
    stop = agent.store.dir / 'STOP'
    stop.touch()
    cloud.directive = directive('live', kill=False)
    agent.step(now=T0 + 600)
    assert stop.exists()


def test_F20_malformed_directive_does_not_crash_the_step(tmp_path):
    """reproduces (audit 18): a portfolio that is not an object raised AttributeError, so the cycle (and
    the cancellation of open orders) never ran."""
    agent = _agent(FakeCloud(directive={'global_halt': False, 'portfolio': ['not', 'an', 'object']}),
                   FakeExchange(), tmp_path)
    res = agent.step(now=T0)
    assert res['fresh'] is False


def test_F21_hostile_cloud_json_is_a_cloud_error(tmp_path):
    """reproduces (audit 18): deeply nested JSON raised RecursionError out of the cloud client."""
    from avid_agent.cloud import CloudClient, CloudError

    class R:
        def __init__(self, b):
            self.b = b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return self.b
    body = b'[' * 200_000 + b']' * 200_000
    c = CloudClient('https://cloud.example', 'agent-1', b'\x01' * 32, opener=lambda req, timeout=None: R(body))
    with pytest.raises(CloudError):
        c.sync({})


def test_F19_broken_ceilings_file_still_lets_stop_cancel(tmp_path, monkeypatch):
    """reproduces (audit 59): a broken ceilings.json raised before the cycle, so STOP could not cancel."""
    from avid_agent import config
    monkeypatch.setattr(config, 'HOME', tmp_path)
    config.save_ceilings(dict(LIVE_CEILINGS))
    ex = FakeExchange()
    from avid_agent.engine import Agent
    agent = Agent(FakeCloud(directive=directive('live')), ex, tmp_path, config.load_ceilings)
    agent.step(now=T0)
    assert len(ex.open_bot_orders()) == 1
    (tmp_path / 'ceilings.json').write_text('{"live_allowed": tru')
    (agent.store.dir / 'STOP').write_text('stop')
    try:
        agent.step(now=T0 + 600)
    except Exception:
        pass
    assert ex.open_bot_orders() == []


# ---------------------------------------------------------------- local host / config (audit 34, 43, 44, 54)
def test_F13_unknown_network_name_is_an_error_not_production(monkeypatch):
    """reproduces (audit 43): NOBITEX_NETWORK=testnett silently meant PRODUCTION."""
    from avid_core import client
    monkeypatch.setenv('NOBITEX_NETWORK', 'testnett')
    with pytest.raises((client.NobitexError, ValueError)):
        client.NobitexClient(token='x')


def test_F14_stale_keep_orders_flag_is_ignored(tmp_path):
    """reproduces (audit 44): a keep-orders flag left over from an old 'restart' made a later shutdown
    keep the bot's orders open."""
    from avid_agent import __main__ as cli
    flag = tmp_path / cli.KEEP_FLAG
    old = time.time() - 86400
    flag.write_text(str(int(old)))
    os.utime(flag, (old, old))
    called = []

    class A:
        def safe_shutdown(self):
            called.append(1)
            return {'ok': True, 'open_bot_orders': [], 'message': 'ok'}
    cli.on_stop(A(), tmp_path)
    assert called


def test_F15_installer_does_not_link_a_user_writable_script_into_the_system_path():
    """reproduces (audit 54): 'sudo ln -sf' put a user-writable script on root's PATH."""
    text = (SRC / 'scripts/avid-agent').read_text()
    assert 'sudo ln' not in text and '/usr/local/bin' not in text


def test_F22_live_orders_need_a_constant_time_signer(tmp_path, monkeypatch):
    """reproduces (report 10.3, audit 33): live orders were signed by the pure-Python Ed25519.
    0.1.0a2 (PRA-04): on mainnet there is no override at all - not even AVID_ALLOW_PURE_ED25519=1."""
    monkeypatch.setitem(sys.modules, 'cryptography.hazmat.primitives.asymmetric.ed25519', None)
    monkeypatch.setenv('AVID_ALLOW_PURE_ED25519', '1')       # e.g. copied from a test setup
    ex = FakeExchange()
    ex.auth_mode, ex.signer, ex.network = 'api_key', 'pure', 'mainnet'   # a client that signs with pure Python
    _run(ex, tmp_path, make_cfg(), T0)
    assert ex.add_attempts == 0


def test_F23_agent_id_from_the_cloud_is_validated(tmp_path, monkeypatch):
    """reproduces (audit 16): any agent_id was stored and later sent as an HTTP header."""
    from avid_agent import config
    monkeypatch.setattr(config, 'HOME', tmp_path)
    with pytest.raises((config.ConfigError, ValueError)):
        config.save_agent('https://cloud.example', 'a1\r\nX-Evil: 1', b'\x01' * 32)


def test_F24_64_byte_key_with_a_wrong_public_half_is_rejected():
    """reproduces (audit 33): seed||public keys were accepted without checking the public half."""
    from avid_core import ed25519
    with pytest.raises(ValueError):
        ed25519.parse_private_key(base64.b64encode(bytes(range(32)) + b'\x00' * 32).decode())
