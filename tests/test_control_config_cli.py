"""Control file trust boundary, local ceilings, keep-orders flag, resolve CLI, file permissions."""
import json
import os
import stat
import time
from pathlib import Path

import pytest

from fakes import FakeExchange, make_cfg
from avid_agent import __main__ as cli
from avid_agent import config
from avid_core import control
from avid_core.cycle import Store, cycle

T0 = 1_700_000_000.0


def events(root):
    p = Path(root) / 'data/bot/log.jsonl'
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# ---------------------------------------------------------------- control file (report 10.2)
def test_control_can_only_tighten():
    with pytest.raises(control.ControlError):
        control.validate({'max_order_usdt': 50}, ceilings={'max_order_usdt': 20})
    with pytest.raises(control.ControlError):
        control.validate({'min_cash_usdt': 0}, ceilings={'min_cash_usdt': 10})
    eff, live = control.effective(make_cfg(max_spread=0.01, min_cash_usdt=5.0),
                                  {'max_spread': 0.04, 'min_cash_usdt': 1.0, 'max_order_usdt': 15.0}, live_allowed=False)
    assert eff.max_spread == 0.01 and eff.min_cash_usdt == 5.0 and eff.max_order_usdt == 15.0 and not live


def test_last_valid_control_stays_in_force(tmp_path):
    ex = FakeExchange()
    control.save(tmp_path, {'mode': 'live', 'max_order_usdt': 12.0})
    cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0)
    assert ex.bot_orders()[0]['amount'] * ex.bot_orders()[0]['price'] <= 12.0
    ex.fill(ex.bot_orders()[0]['id'])
    control.control_path(tmp_path).write_text('{"mode": "live", "max_order_usdt": NaN}')
    r = cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0 + 60)
    assert ex.add_attempts == 1 and r['event'] in ('hold', 'no_trade')
    eff = json.loads((Store(tmp_path).dir / 'effective.json').read_text())
    assert eff['max_order_usdt'] == 12.0 and not eff['allow_new_orders']
    assert sum(1 for e in events(tmp_path) if e['event'] == 'control_invalid') == 1      # alerted once
    cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0 + 120)
    assert sum(1 for e in events(tmp_path) if e['event'] == 'control_invalid') == 1


def test_invalid_cfg_numbers_stop_new_orders(tmp_path):
    ex = FakeExchange()
    r = cycle(ex, Store(tmp_path), make_cfg(max_daily_usdt=float('nan')), live=True, now=T0)
    assert ex.add_attempts == 0 and r['event'] == 'hold'


def test_market_spec_file_is_validated(tmp_path):
    ex = FakeExchange()
    d = Store(tmp_path).dir
    (d / 'markets.json').write_text('{"btc": {"amount_step": 0.3}}')
    cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0)
    assert ex.add_attempts == 0 and any(e['event'] == 'markets_invalid' for e in events(tmp_path))
    (d / 'markets.json').unlink()
    cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0 + 60)
    assert ex.add_attempts == 1


def test_another_avid_instance_is_detected(tmp_path):
    ex = FakeExchange()
    ex.place_foreign('buy', 'btc', 0.0003, 50000.0, cid='av9999000001' + 'a' * 20)
    cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0)
    assert ex.add_attempts == 0 and any(e['event'] == 'other_avid_instance' for e in events(tmp_path))


def test_data_folder_is_private(tmp_path):
    s = Store(tmp_path)
    s.log('x')
    s.save(s.fresh())
    assert stat.S_IMODE(s.dir.stat().st_mode) == 0o700
    for p in (s.log_path, s.state_path):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_symlinked_log_is_not_followed(tmp_path):
    s = Store(tmp_path)
    target = tmp_path / 'elsewhere'
    target.write_text('')
    os.symlink(target, s.log_path)
    with pytest.raises(OSError):
        s.log('x')
    assert target.read_text() == ''


# ---------------------------------------------------------------- local ceilings (AVID-02)
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HOME', tmp_path)
    return tmp_path


def test_ceilings_defaults_and_validation(home):
    c = config.load_ceilings()
    assert c['live_allowed'] is False and c['max_spread'] == 0.01
    assert stat.S_IMODE((home / 'ceilings.json').stat().st_mode) == 0o600
    for raw in ({'live_allowed': 'true'}, {'max_daily': 100}, {'max_order_usdt': 50, 'max_daily_usdt': 40},
                {'max_spread': 0.2}, {'min_cash_usdt': -1}):
        with pytest.raises(config.ConfigError):
            config.validate_ceilings(raw)
    with pytest.raises(config.ConfigError):
        config.save_ceilings({'max_order_usdt': float('nan')})


def test_agent_key_checks(home):
    config.save_agent('https://cloud.example', 'agent-1', b'\x02' * 32)
    a = config.load_agent()
    assert a['seed'] == b'\x02' * 32 and a['agent_id'] == 'agent-1'
    (home / 'agent_key').write_text('not base64!!')
    with pytest.raises(config.ConfigError):
        config.load_agent()
    os.chmod(home / 'agent_key', 0o644)
    with pytest.raises(config.ConfigError, match='permissions'):
        config.load_agent()


@pytest.mark.parametrize('url', ['http://evil.example', 'https://u:p@x.example', 'https://x.example/path',
                                 'https://x.example?q', 'http://localhost.attacker.example', 'https://x .example',
                                 'https://exämple.ir'])
def test_cloud_url_rules(url):
    with pytest.raises(config.ConfigError):
        config.check_cloud_url(url)


def test_cloud_url_ok():
    assert config.check_cloud_url('https://avid.example.ir/') == 'https://avid.example.ir'
    assert config.check_cloud_url('http://127.0.0.1:8080') == 'http://127.0.0.1:8080'


# ---------------------------------------------------------------- keep-orders flag (audit 44)
def test_fresh_keep_flag_keeps_orders(home):
    config.write_keep_flag()
    called = []

    class A:
        def safe_shutdown(self):
            called.append(1)
            return {'ok': True}
    res = cli.on_stop(A(), home)
    assert not called and res['cancelled'] is False and not (home / config.KEEP_FLAG).exists()


def test_keep_flag_with_old_timestamp_inside_is_ignored(home):
    config.write_keep_flag(now=time.time() - 7200)              # file is new, content says two hours ago
    assert not config.keep_flag_fresh(home / config.KEEP_FLAG)


# ---------------------------------------------------------------- resolve CLI (audit 45)
def test_resolve_cli_requires_typed_client_order_id(home, monkeypatch):
    ex = FakeExchange()
    ex.faults.append(('add', 'network_before'))
    try:
        cycle(ex, Store(home), make_cfg(), live=True, now=T0)
    except Exception:
        pass
    cid = Store(home).load()['pending']['cid']
    monkeypatch.setattr(cli, 'make_exchange', lambda: ex)
    answers = iter(['CHECKED', 'wrong-cid'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
    assert cli.main(['resolve', '--not-placed']) == 2
    assert Store(home).load()['pending']
    answers = iter(['CHECKED', cid])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
    assert cli.main(['resolve', '--not-placed']) == 0
    st = Store(home).load()
    assert st['pending'] is None and st['orders_today'] == 1          # caps stay counted
    assert any(e['event'] == 'pending_resolved_manually' and e.get('operator') for e in events(home))


def test_resolve_refuses_too_early(home):
    from avid_core.cycle import ManualResolution, resolve_manually
    now = time.time()
    ex = FakeExchange(now=now)
    ex.faults.append(('add', 'network_before'))
    try:
        cycle(ex, Store(home), make_cfg(), live=True, now=now)
    except Exception:
        pass
    with pytest.raises(ManualResolution, match='too early'):
        resolve_manually(Store(home), pending_not_placed=True, client=ex, now=now + 60)


def test_ceilings_cli_refuses_bad_values(home, monkeypatch):
    assert cli.main(['ceilings', '--max-daily', 'nan']) == 2
    assert cli.main(['ceilings', '--max-order', '10']) == 2                       # below the exchange minimum
    assert cli.main(['ceilings', '--max-order', '30', '--max-daily', '20']) == 2  # daily below one order
    assert cli.main(['ceilings', '--max-spread', '0.005']) == 0
    assert config.load_ceilings()['max_spread'] == 0.005
