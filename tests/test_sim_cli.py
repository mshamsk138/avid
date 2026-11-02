"""Offline simulator and the avid-engine command line (PRA-01, PRA-02).

The simulator runs the REAL engine (cycle.cycle) against avid_core.sim.SimExchange: these tests check
that it is reproducible, never touches the network or credentials, never mixes with a real engine
folder, and that the engine keeps its limits while it runs."""
from __future__ import annotations

import collections
import json
import socket
import urllib.request

import pytest

from avid_core import cli, models, sim
from avid_core.client import NobitexClient
from avid_core.rebalance import MARKETS

from fakes import FakeExchange


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError('the simulator tried to open a network connection')
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    monkeypatch.setenv('NOBITEX_SECRETS_DIR', '/nonexistent/avid-test-secrets')


def _strip(report):
    return {k: v for k, v in report.items() if k != 'engine_root'}


def test_example_is_reproducible_and_offline(no_network):
    a = sim.run(sim.load_example('btc-usdt'))
    b = sim.run(sim.load_example('btc-usdt'))
    assert _strip(a) == _strip(b)
    assert a['kind'] == 'SIMULATION' and a['errors'] == 0 and a['engine_root'] is None


def test_first_decision_of_the_200_usdt_example(no_network):
    d = sim.run(sim.load_example('btc-usdt'), steps=1)['first_decision']
    assert d['portfolio_value_usdt'] == 200.0
    assert d['current_weights'] == {'btc': 0.0, 'usdt': 1.0}
    assert d['target_weights']['btc'] == 0.5 and d['target_weights']['usdt'] == 0.5
    assert d['band']['btc'] == [0.375, 0.625]
    o = d['order']
    # 20 USDT cap / best bid 59988.00 = 0.00033339.. floored to the 1e-6 BTC step
    assert (o['side'], o['asset'], o['amount'], o['price']) == ('buy', 'btc', 0.000333, 59988.0)
    assert o['notional_usdt'] == pytest.approx(19.98, abs=0.005)
    assert [c['check'] for c in d['risk_checks']] == ['cash_reserve', 'day_lock', 'orders_per_day', 'daily_notional']
    assert all(c['ok'] for c in d['risk_checks'])
    assert d['reason'] == 'btc weight 0.000 vs target 0.500'


@pytest.mark.parametrize('name', sim.EXAMPLES)
def test_engine_limits_hold_during_the_simulation(no_network, name):
    scn = sim.load_example(name)
    r = sim.run(scn)
    s = scn.settings
    per_day = collections.defaultdict(list)
    for o in r['exchange_orders']:
        per_day[o['placed_at'][:10]].append(o)
        assert 11.0 < o['notional_usdt'] <= s['max_order_usdt'] + 1e-9
        assert o['client_order_id_len'] == 32
    for day, orders in per_day.items():
        assert len(orders) <= s['max_orders_per_day'], day
        committed = sum(o['notional_usdt'] if o['status'] != 'Canceled' else o['filled'] * o['price'] for o in orders)
        assert committed <= s['max_daily_usdt'] + 1e-6, day
    # never two bot orders resting at the same time: each order is placed after the previous one closed
    orders = sorted(r['exchange_orders'], key=lambda o: o['id'])
    for prev, nxt in zip(orders, orders[1:]):
        assert prev['closed_at'] is not None and prev['closed_at'] <= nxt['placed_at']
    for t in r['timeline']:
        for f in t['fills']:
            assert f['fee'] >= 0
    assert all(v >= -1e-12 for v in r['end_balances'].values())


def test_a_real_engine_folder_is_never_used(tmp_path, no_network):
    bot = tmp_path / 'data' / 'bot'
    bot.mkdir(parents=True)
    (bot / 'state.json').write_text('{}')
    with pytest.raises(sim.ScenarioError, match='not from a simulation'):
        sim.run(sim.load_example('btc-usdt'), root=tmp_path, steps=2)
    assert (bot / 'state.json').read_text() == '{}'
    assert cli.main(['simulate', '--root', str(tmp_path), '--steps', '2']) == 2


def test_a_simulation_root_is_reused_and_marked(tmp_path, no_network, capsys):
    root = tmp_path / 'sim root with spaces'
    a = sim.run(sim.load_example('btc-usdt'), root=root, steps=10, keep=True)
    assert (root / 'data' / 'bot' / sim.SIM_MARKER).is_file()
    b = sim.run(sim.load_example('btc-usdt'), root=root, steps=10, keep=True)
    assert _strip(a) == _strip(b)
    assert cli.main(['status', '--root', str(root), '--json']) in (0, 1)
    st = json.loads(capsys.readouterr().out)
    assert st['simulation'] is True and st['engine_data'] is True


@pytest.mark.parametrize('patch, msg', [
    ({'prices': {'btc': [60000.0, float('nan')]}}, 'not valid JSON'),
    ({'prices': {'btc': [60000.0], 'paxg': [1.0, 2.0]}}, 'same length'),
    ({'prices': {'btc': [-1.0]}}, 'prices.btc'),
    ({'balances': {'eth': 1}}, 'balances'),
    ({'targets': {'btc': 0.7, 'usdt': 0.5}}, 'sum to 1'),
    ({'settings': {'band_rel': 0.01}}, 'band_rel'),
    ({'settings': {'max_order_usdt': 50, 'max_daily_usdt': 20}}, 'max_daily_usdt'),
    ({'settings': {'unknown': 1}}, 'settings may only'),
    ({'format': 'other'}, 'format'),
    ({'start_time': '2026-01-05T00:00:00'}, 'time zone'),
    ({'exchange': {'fee_rate': 0.5}}, 'fee_rate'),
])
def test_invalid_scenarios_are_refused(patch, msg):
    d = json.loads(sim.resources.files('avid_core').joinpath('examples', 'btc-usdt.json').read_text())
    d.update(patch)
    text = json.dumps(d, allow_nan=True)
    with pytest.raises(sim.ScenarioError, match=msg):
        sim.parse_scenario(text)


def test_sim_exchange_refuses_what_nobitex_refuses():
    ex = sim.SimExchange({'usdt': 100}, fee_rate=0.002, spread_rel=0.0004, now=1.7e9)
    ex.set_mid('btc', 60000)
    for args, code in ((('buy', 0.0001, 60000.0), 'SmallOrder'), (('buy', 0.0003334, 60000.0), 'InvalidPrecision'),
                       (('buy', 0.002, 60000.0), 'InsufficientBalance'), (('sell', 0.0003, 60000.0), 'InsufficientBalance')):
        with pytest.raises(Exception) as e:
            ex.add_limit_order(*args, client_order_id='x' * 32)
        assert getattr(e.value, 'code', None) == code
    ex.add_limit_order('buy', 0.0003, 59000.0, client_order_id='c' * 32)
    with pytest.raises(Exception, match='DuplicateClientOrderId'):
        ex.add_limit_order('buy', 0.0003, 59000.0, client_order_id='c' * 32)
    assert ex.wallets(('usdt',))['usdt']['available'] == pytest.approx(100 - 0.0003 * 59000)
    ex.match()                                     # same step: never fills where it was placed
    assert ex.order_status(client_order_id='c' * 32)['status'] == 'Active'
    ex.now += 600
    ex.set_mid('btc', 58000)
    assert len(ex.match()) == 1
    assert ex.order_status(1)['status'] == 'Done'
    with pytest.raises(Exception, match='InvalidOrderStatus'):
        ex.cancel(1)


def test_every_adapter_has_the_exchange_adapter_surface():
    ex = sim.SimExchange({'usdt': 1}, fee_rate=0, spread_rel=0.001, now=0)
    for obj in (ex, FakeExchange(), NobitexClient(token='t' * 16)):
        assert isinstance(obj, models.ExchangeAdapter), type(obj).__name__
    specs = NobitexClient(token='t' * 16).market_specs()
    assert set(specs['markets']) == set(MARKETS)
    assert ex.market_specs()['markets'] == specs['markets']


def test_simulate_cli_human_and_json(no_network, capsys):
    assert cli.main(['simulate', '--steps', '3']) == 0
    out = capsys.readouterr().out
    for field in ('Portfolio Value', 'Current Weights', 'Target Weights', 'Band', 'Decision', 'Order Amount',
                  'Order Price', 'Risk Checks', 'Reason', 'SIMULATION'):
        assert field in out
    assert cli.main(['simulate', '--example', 'btc-paxg-usdt', '--steps', '2', '--json']) == 0
    r = json.loads(capsys.readouterr().out)
    assert r['scenario'] == 'btc-paxg-usdt' and r['steps'] == 2
    assert cli.main(['simulate', '--list']) == 0
    assert 'btc-usdt' in capsys.readouterr().out


def test_cli_help_version_and_usage_errors(capsys):
    assert cli.main([]) == 0
    assert cli.main(['help']) == 0
    with pytest.raises(SystemExit) as e:
        cli.main(['help', 'run'])
    assert e.value.code == 0
    assert '--live' in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(['--version'])
    assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        cli.main(['plan', '--live'])                   # --live only exists for run
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        cli.main(['run', '--max-order-usdt', 'nan'])
    assert e.value.code == 2


def test_status_reads_without_side_effects(tmp_path, capsys):
    assert cli.main(['status', '--root', str(tmp_path)]) == 0
    assert 'no engine data' in capsys.readouterr().out
    assert not (tmp_path / 'data').exists()            # status never creates the engine folder
    bot = tmp_path / 'data' / 'bot'
    bot.mkdir(parents=True)
    (bot / 'state.json').write_text('{"bot_orders": ')
    (bot / 'STOP').write_text('')
    assert cli.main(['status', '--root', str(tmp_path), '--json']) == 1
    r = json.loads(capsys.readouterr().out)
    assert r['state_error'] and r['stop_files'] == ['STOP']
    assert (bot / 'state.json').read_text() == '{"bot_orders": '   # the corrupt file is left as it is
    assert sorted(p.name for p in bot.iterdir()) == ['STOP', 'state.json']


def test_check_and_plan_use_the_adapter_read_only(tmp_path, monkeypatch, capsys):
    ex = FakeExchange(usdt=200.0)
    monkeypatch.setattr(cli, '_client', lambda: ex)
    assert cli.main(['check', '--weights', 'btc=0.5,usdt=0.5', '--json']) == 0
    r = json.loads(capsys.readouterr().out)
    assert r['orders_sent'] == 0 and r['portfolio_value_usdt'] == 200.0
    assert cli.main(['plan', '--root', str(tmp_path), '--weights', 'btc=0.5,usdt=0.5']) == 0
    out = capsys.readouterr().out
    assert 'read-only preview' in out and 'would be placed (dry run' in out
    assert ex.add_attempts == 0 and not [c for c in ex.calls if c[0] == 'cancel']
    assert not (tmp_path / 'data' / 'bot' / 'state.json').exists()


def test_run_once_dry_by_default(tmp_path, monkeypatch, capsys):
    ex = FakeExchange(usdt=200.0)
    monkeypatch.setattr(cli, '_client', lambda: ex)
    assert cli.main(['run', '--once', '--root', str(tmp_path), '--weights', 'btc=0.5,usdt=0.5', '--json']) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[0]['mode'] == 'DRY_RUN' and lines[1]['event'] == 'dry_run_would_place'
    assert ex.add_attempts == 0


def test_engine_entry_point_still_works_via_cycle_module(capsys):
    from avid_core import cycle
    assert cycle.main(['help']) == 0
