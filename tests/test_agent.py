"""Agent behaviour against a simulated cloud and exchange (trust boundary B4, Invariants D and E)."""
import json

from fakes import LIVE_CEILINGS, FakeCloud, FakeExchange, directive, policy
from avid_agent.cloud import CloudError
from avid_agent.engine import CLOUD_STOP, CLOUD_STOP_MARK, Agent

T0 = 1_700_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def agent(tmp_path, cloud=None, ex=None, ceil=None, **kw):
    clock = kw.pop('clock', None) or Clock()
    return Agent(cloud or FakeCloud(directive=directive('live')), ex or FakeExchange(), tmp_path,
                 lambda: dict(ceil or LIVE_CEILINGS), clock=clock, **kw), clock


def events(a):
    p = a.store.log_path
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_live_needs_cloud_live_and_local_permission(tmp_path):
    ex = FakeExchange()
    a, _ = agent(tmp_path / 'sim', FakeCloud(directive=directive('simulation')), ex)
    a.step()
    assert ex.add_attempts == 0 and any(e['event'] == 'dry_run_would_place' for e in events(a))
    a2, _ = agent(tmp_path / 'live', FakeCloud(directive=directive('live')), ex)
    a2.step()
    assert ex.add_attempts == 1


def test_cloud_down_manages_but_places_nothing_then_offline_cancels(tmp_path):
    ex = FakeExchange()
    cloud = FakeCloud(directive=directive('live'))
    a, clock = agent(tmp_path, cloud, ex, offline_grace_s=1800)
    a.step()
    assert len(ex.open_bot_orders()) == 1
    oid = ex.open_bot_orders()[0]['id']
    ex.fill(oid)
    cloud.fail = CloudError('cloud unreachable: timeout')
    clock.t += 600
    ex.now = clock.t
    r = a.step()
    assert not r['fresh'] and ex.add_attempts == 1                 # settled, but nothing new without a fresh sync
    cloud.fail = None
    clock.t += 600
    ex.now = clock.t
    a.step()
    assert ex.add_attempts == 2
    cloud.fail = CloudError('cloud unreachable: timeout')
    clock.t += 3600
    ex.now = clock.t
    r = a.step()
    assert r['offline'] and not ex.open_bot_orders()               # offline beyond grace: own order cancelled


def test_revocation_is_permanent_and_cancels(tmp_path):
    ex = FakeExchange()
    cloud = FakeCloud(directive=directive('live'))
    a, clock = agent(tmp_path, cloud, ex)
    a.step()
    assert ex.open_bot_orders()
    cloud.fail = CloudError('cloud HTTP 401: revoked', status=401, code='agent_revoked')
    clock.t += 600
    a.step()
    assert not ex.open_bot_orders() and a.revoked
    cloud.fail = None
    n = len(cloud.reports)
    b, _ = agent(tmp_path, cloud, ex)                              # restart: still revoked, no sync at all
    b.step()
    assert b.revoked and len(cloud.reports) == n and ex.add_attempts == 1


def test_kill_switch_uses_its_own_file(tmp_path):
    ex = FakeExchange()
    cloud = FakeCloud(directive=directive('live'))
    a, clock = agent(tmp_path, cloud, ex)
    a.step()
    cloud.directive = directive('live', kill=True)
    clock.t += 600
    a.step()
    assert (a.store.dir / CLOUD_STOP).exists() and not (a.store.dir / 'STOP').exists()
    assert not ex.open_bot_orders()
    cloud.directive = directive('live')
    clock.t += 600
    a.step()
    assert not (a.store.dir / CLOUD_STOP).exists()


def test_v12_cloud_stop_file_is_migrated(tmp_path):
    d = tmp_path / 'data/bot'
    d.mkdir(parents=True)
    (d / 'STOP').write_text(CLOUD_STOP_MARK)
    a, _ = agent(tmp_path)
    assert (d / CLOUD_STOP).exists() and not (d / 'STOP').exists()


def test_global_halt_and_expired_policy(tmp_path):
    ex = FakeExchange()
    a, _ = agent(tmp_path / 'halt', FakeCloud(directive=directive('live', halt=True)), ex)
    a.step()
    assert ex.add_attempts == 0
    exp = int(T0 * 1000) - 1
    b, _ = agent(tmp_path / 'exp', FakeCloud(directive=directive('live', pol=policy(expires_at=exp))), ex)
    r = b.step()
    assert ex.add_attempts == 0 and any('expired' in h for h in r['holds'])


def test_invalid_ack_deletes_nothing_and_blocks_new_orders(tmp_path):
    ex = FakeExchange()
    cloud = FakeCloud(directive=directive('live'))
    cloud.respond = lambda rep: {'protocol': 2, 'agent_id': 'someone-else', 'batch_id': rep['batch_id'],
                                 'ack': {'accepted': [], 'rejected': []}, 'directive': directive('live')}
    a, _ = agent(tmp_path, cloud, ex)
    a.outbox.put('events', {'key': 'x', 'at': 1, 'kind': 'error', 'level': 'error', 'message': 'keep me', 'data': {}})
    r = a.step()
    assert not r['fresh'] and ex.add_attempts == 0 and len(a.outbox) >= 1
    assert any(e['event'] == 'cloud_bad_ack' for e in events(a))


def test_storage_pressure_blocks_new_orders(tmp_path):
    ex = FakeExchange()
    cloud = FakeCloud(directive=directive('live'))
    cloud.accept = False                                              # the cloud stores nothing
    a, _ = agent(tmp_path, cloud, ex, outbox_max_items=3)
    for i in range(5):
        a.outbox.put('orders', {'exchange_order_id': str(i), 'status': 'open'})
    r = a.step()
    assert a.outbox.pressure and ex.add_attempts == 0
    assert any('pressure' in h for h in r['holds'])


def test_cloud_rate_limit_backs_off(tmp_path):
    cloud = FakeCloud(directive=directive('simulation'))
    a, clock = agent(tmp_path, cloud)
    cloud.fail = CloudError('cloud HTTP 429', status=429, retry_after=900)
    a.step()
    n = len(cloud.reports)
    cloud.fail = None
    clock.t += 300
    a.step()
    assert len(cloud.reports) == n                                    # still backing off
    clock.t += 700
    a.step()
    assert len(cloud.reports) > n


def test_too_large_batches_shrink(tmp_path):
    cloud = FakeCloud(directive=directive('simulation'))
    seen = []

    def respond(rep):
        n = sum(len(rep.get(k) or []) for k in ('snapshots', 'intents', 'orders', 'fills', 'events'))
        seen.append(n)
        if n > 50:
            raise CloudError('cloud HTTP 413', status=413)
        cloud.respond = None
        r = cloud.sync(rep)
        cloud.respond = respond
        return r
    cloud.respond = respond
    a, _ = agent(tmp_path, cloud)
    for i in range(300):
        a.outbox.put('events', {'key': f'e{i}', 'at': i, 'kind': 'x', 'level': 'warning', 'message': '', 'data': {}})
    a._sync(dict(LIVE_CEILINGS), T0)
    assert seen[0] > 50 and min(seen) <= 50 and len(a.outbox) < 300


def test_reports_carry_keys_network_and_ceilings(tmp_path):
    cloud = FakeCloud(directive=directive('simulation'))
    a, _ = agent(tmp_path, cloud)
    a.step()
    rep = cloud.reports[-1]
    assert rep['protocol'] == 2 and rep['network'] == 'fake' and rep['local_ceilings']['live_allowed'] is True
    items = [it for k in ('snapshots', 'events', 'intents') for it in rep.get(k) or []]
    assert items and all(it.get('item_key') for it in items)


def test_local_bounds_win_over_the_cloud(tmp_path):
    ex = FakeExchange(usdt=40.0)
    ceil = {**LIVE_CEILINGS, 'min_cash_usdt': 30.0}
    a, _ = agent(tmp_path, FakeCloud(directive=directive('live', pol=policy(min_cash_usdt=0))), ex, ceil=ceil)
    a.step()
    assert ex.add_attempts == 0                                       # 40 - 20 order < local floor of 30


def test_log_gap_is_reported(tmp_path):
    a, _ = agent(tmp_path, FakeCloud(directive=directive('simulation')))
    a.step()
    a.outbox.set_cursor({'dev': 1, 'ino': 123456789, 'off': 10})
    a._collect_log()
    assert any(e['event'] == 'log_gap' for e in events(a))


def test_v12_agent_state_is_upgraded(tmp_path):
    (tmp_path / 'data/bot').mkdir(parents=True)
    (tmp_path / 'data/bot/log.jsonl').write_text(json.dumps({'at': '2023-11-14T22:00:00+00:00', 'event': 'no_trade',
                                                             'reason': 'old'}) + '\n')
    size = (tmp_path / 'data/bot/log.jsonl').stat().st_size
    (tmp_path / 'agent_state.json').write_text(json.dumps({'log_offset': size, 'last_sync_ok': T0,
                                                           'directive': {'portfolio': ['broken']}}))
    a, _ = agent(tmp_path, FakeCloud(directive=directive('simulation')))
    assert a.state['directive'] is None
    a._collect_log()
    batch, _ = a.outbox.batch()
    assert not any(e.get('message') == 'old' for e in batch['events'])     # already sent by v1.2: not resent


def test_safe_shutdown_cancels_and_reports(tmp_path):
    ex = FakeExchange()
    a, _ = agent(tmp_path, FakeCloud(directive=directive('live')), ex)
    a.step()
    assert ex.open_bot_orders()
    res = a.safe_shutdown()
    assert res['ok'] and not ex.open_bot_orders()
    ex2 = FakeExchange()
    b, _ = agent(tmp_path / 'b', FakeCloud(directive=directive('live')), ex2)
    b.step()
    ex2.cancel_lag.add(ex2.open_bot_orders()[0]['id'])          # the exchange never applies the cancel
    res = b.safe_shutdown()
    assert not res['ok'] and res['open_bot_orders']


def test_unexpected_exchange_exception_does_not_stop_reporting(tmp_path):
    ex = FakeExchange()
    a, _ = agent(tmp_path, FakeCloud(directive=directive('live')), ex)
    ex.faults.append(('open_orders', ZeroDivisionError('boom')))
    r = a.step()
    assert r['record']['event'] == 'error' and 'UNEXPECTED' in r['record']['message']


def test_should_stop_cuts_the_drain_short(tmp_path):
    """A stop that arrives while the backlog drains ends the drain after the request in flight; a stop that
    is already pending sends nothing (the controlled stop sends its own, budgeted, final report)."""
    cloud = FakeCloud(directive=directive('simulation'))
    stop = []
    a, _ = agent(tmp_path, cloud, should_stop=lambda: bool(stop))
    for i in range(2000):
        a.outbox.put('events', {'key': f'e{i}', 'at': i, 'kind': 'x', 'level': 'warning', 'message': '', 'data': {}})
    real = cloud.sync

    def sync_then_signal(report):
        stop.append(1)                                     # SIGTERM arrives during this request
        return real(report)
    cloud.sync = sync_then_signal
    a._sync(dict(LIVE_CEILINGS), T0)
    assert len(cloud.reports) == 1 and len(a.outbox) > 0
    a._sync(dict(LIVE_CEILINGS), T0)
    assert len(cloud.reports) == 1                         # already stopping: nothing sent
    a._sync(dict(LIVE_CEILINGS), T0, budget_s=5, ignore_stop=True)
    assert len(cloud.reports) > 1                          # the shutdown's final report still goes out


def test_corrupt_agent_state_is_set_aside(tmp_path):
    (tmp_path / 'data/bot').mkdir(parents=True)
    (tmp_path / 'agent_state.json').write_text('{"revoked": tru')
    a, _ = agent(tmp_path, FakeCloud(directive=directive('simulation')))
    assert a.state.get('revoked') is None
    assert list(tmp_path.glob('agent_state.corrupt-*.json'))
    assert any(e['event'] == 'state_corrupt' for e in events(a))


def test_several_rotations_between_reads_lose_nothing(tmp_path):
    a, _ = agent(tmp_path, FakeCloud(directive=directive('simulation')))
    s = a.store
    s.log('waiting', reason='r-0')
    a._collect_log()
    for gen in range(3):                                   # three rotations before the agent reads again
        for i in range(5):
            s.log('waiting', reason=f'g{gen}-{i}')
        s.LOG_MAX_BYTES = 0
        s.rotate_log()
    for i in range(5):
        s.log('waiting', reason=f'cur-{i}')
    a._collect_log()
    batch, _ = a.outbox.batch(10_000, 10**9)
    msgs = {e['message'] for e in batch['events']}
    want = {f'g{g}-{i}' for g in range(3) for i in range(5)} | {f'cur-{i}' for i in range(5)}
    assert want <= msgs
    assert not any(e['event'] == 'log_gap' for e in events(a))


def test_more_rotations_than_kept_generations_are_reported(tmp_path):
    a, _ = agent(tmp_path, FakeCloud(directive=directive('simulation')))
    s = a.store
    s.log('waiting', reason='start')
    a._collect_log()
    s.LOG_MAX_BYTES = 0
    for gen in range(5):
        s.log('waiting', reason=f'g{gen}')
        s.rotate_log()
    a._collect_log()
    assert any(e['event'] == 'log_gap' for e in events(a))


def test_rereading_the_log_never_duplicates(tmp_path):
    a, _ = agent(tmp_path, FakeCloud(directive=directive('simulation')))
    for i in range(10):
        a.store.log('waiting', reason=f'x{i}')
    a._collect_log()
    n = len(a.outbox)
    a.outbox.set_cursor({'dev': 0, 'ino': 0, 'off': 0})        # cursor lost: everything is read again
    a._collect_log()
    batch, _ = a.outbox.batch(10_000, 10**9)
    assert len(a.outbox) == n + 1                               # only the log_gap report itself is new
    assert sum(1 for e in batch['events'] if e['kind'] == 'log_gap') == 1


def test_timeout_after_the_cloud_committed_is_safe_to_resend(tmp_path):
    cloud = FakeCloud(directive=directive('simulation'))
    stored = []
    first = [True]

    def respond(rep):
        keys = [it['item_key'] for k in ('snapshots', 'intents', 'orders', 'fills', 'events') for it in rep.get(k) or []]
        stored.extend(keys)                                     # the cloud commits ...
        if first[0]:
            first[0] = False
            raise CloudError('cloud unreachable: TimeoutError')  # ... but the answer is lost
        return {'protocol': 2, 'agent_id': cloud.agent_id, 'batch_id': rep['batch_id'],
                'ack': {'accepted': keys, 'rejected': []}, 'directive': cloud.directive}
    cloud.respond = respond
    a, _ = agent(tmp_path, cloud)
    a.outbox.put('orders', {'exchange_order_id': '7', 'status': 'filled'})
    a._sync(dict(LIVE_CEILINGS), T0)
    assert len(a.outbox) >= 1                                   # nothing deleted without the ACK
    a._sync(dict(LIVE_CEILINGS), T0)
    assert len(a.outbox) == 0
    assert len(stored) >= 2 and len(set(stored)) < len(stored)  # resent with the SAME key: the cloud de-duplicates
