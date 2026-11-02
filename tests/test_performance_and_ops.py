"""Performance honesty (report 10.6), crash consistency (10.1), installer (audit 54), secrets scan (10.4)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fakes import FakeExchange, make_cfg
from avid_core import performance
from avid_core.cycle import Store, cycle

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_700_000_000.0


def snap(t, btc, usdt, mid, mode):
    return {'at': f'2023-11-{14 + t:02d}T00:00:00+00:00', 'balances': {'btc': btc, 'usdt': usdt},
            'books': {'btc': [mid - 1, mid + 1]}, 'mode': mode}


def test_basis_and_fees_are_reported():
    ev = [snap(0, 0.001, 50, 60000, 'DRY_RUN'), snap(1, 0.001, 50, 61000, 'DRY_RUN')]
    out = performance.compute(ev)
    assert out['basis'] == 'simulation' and out['realized'] is False
    live = [snap(0, 0.001, 50, 60000, 'LIVE'), snap(1, 0.001, 50, 61000, 'LIVE'),
            {'event': 'trade', 'fee': 0.000001, 'price': 60000, 'fee_asset': 'btc'},
            {'event': 'trade', 'fee': 0.05, 'price': 60000, 'fee_asset': 'usdt'},
            {'event': 'fill_aggregate', 'id': '1'}]
    out = performance.compute(live)
    assert out['basis'] == 'live' and out['realized'] is True
    assert out['fees_usdt'] == pytest.approx(0.06 + 0.05)
    assert out['data_quality'] == {'aggregate_only_orders': 1, 'incomplete_orders': 0, 'exact': False}
    assert performance.compute(ev[:1] + live[1:2])['basis'] == 'mixed'


class Crash(BaseException):
    pass


def test_crash_right_after_sending_is_recovered_by_client_order_id(tmp_path):
    """The intent, the cap use and the clientOrderId are on disk before the request leaves; a process
    killed while the request is in flight finds the order again by its clientOrderId."""
    ex = FakeExchange()
    real = ex.add_limit_order

    def add_then_die(*a, **kw):
        real(*a, **kw)
        raise Crash()                                   # power cut / SIGKILL after the exchange took it
    ex.add_limit_order = add_then_die
    with pytest.raises(Crash):
        cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0)
    st = Store(tmp_path).load()
    assert st['pending'] and st['orders_today'] == 1 and st['notional_today'] > 0
    assert any(json.loads(x)['t'] == 'submit' for x in Store(tmp_path).ledger_path.read_text().splitlines())
    ex.add_limit_order = real
    cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0 + 60)
    st = Store(tmp_path).load()
    assert st['pending'] is None and list(st['bot_orders']) == [str(max(ex.orders))]
    assert ex.add_attempts == 1


def test_torn_log_line_does_not_swallow_the_next_record(tmp_path):
    s = Store(tmp_path)
    s.log('first')
    with open(s.log_path, 'ab') as f:
        f.write(b'{"event": "half-writ')                # crash in the middle of a write
    s2 = Store(tmp_path)
    s2.log('second')
    lines = s2.log_path.read_text().splitlines()
    assert json.loads(lines[-1])['event'] == 'second'


def test_leftover_temp_files_do_not_matter(tmp_path):
    s = Store(tmp_path)
    s.save(s.fresh())
    (s.dir / '.state.json.abc.tmp').write_text('garbage')
    assert s.load()['bot_orders'] == {}


def test_installer_runs_without_sudo_into_the_users_path(tmp_path):
    home = tmp_path / 'home'
    stubs = tmp_path / 'bin'
    stubs.mkdir()
    for name in ('systemctl', 'loginctl', 'sudo'):
        (stubs / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp_path}/calls"\n')
        os.chmod(stubs / name, 0o755)
    env = {**os.environ, 'HOME': str(home), 'PATH': f'{stubs}:{os.environ["PATH"]}', 'USER': 'tester'}
    home.mkdir()
    env.pop('XDG_CONFIG_HOME', None)
    env.pop('AVID_AGENT_HOME', None)
    # CI containers often run as root; the installer refuses root unless told it is a container
    extra = ['--allow-root'] if os.geteuid() == 0 else []
    r = subprocess.run(['bash', str(ROOT / 'scripts/avid-agent'), 'install', *extra], env=env, capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    link = home / '.local/bin/avid-agent'
    assert link.is_symlink() and Path(os.readlink(link)) == (ROOT / 'scripts/avid-agent').resolve()
    unit = (home / '.config/systemd/user/avid-agent.service').read_text()
    assert 'NoNewPrivileges=yes' in unit and 'UMask=0077' in unit and 'TimeoutStopSec=300' in unit
    assert 'PYTHONPATH=' in unit and str(ROOT) in unit        # a source checkout runs without pip install
    calls = (tmp_path / 'calls').read_text() if (tmp_path / 'calls').exists() else ''
    assert 'sudo' not in calls


def test_restart_flag_is_timestamped(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    stubs = tmp_path / 'bin'
    stubs.mkdir()
    (stubs / 'systemctl').write_text('#!/bin/sh\nexit 0\n')
    os.chmod(stubs / 'systemctl', 0o755)
    env = {**os.environ, 'HOME': str(home), 'PATH': f'{stubs}:{os.environ["PATH"]}'}
    text = (ROOT / 'scripts/avid-agent').read_text()
    import re
    runs_sudo = [l for l in text.splitlines()
                 if re.search(r'(^|[;&|]\s*|\bif\s+|\bthen\s+)sudo\s', l.split('#')[0]) and 'echo' not in l]
    assert not runs_sudo
    r = subprocess.run(['bash', '-n', str(ROOT / 'scripts/avid-agent')], env=env)
    assert r.returncode == 0
    # the keep-orders flag (written by `avid-agent restart` / `stop --keep-orders`) carries its time and
    # stops counting after 10 minutes (audit 44)
    from avid_agent import config
    old_home = config.HOME
    config.HOME = home / '.avid-agent'
    try:
        now = 1_800_000_000
        config.write_keep_flag(now)
        flag = config.HOME / config.KEEP_FLAG
        assert flag.read_text() == str(now)
        os.utime(flag, (now, now))
        assert config.keep_flag_fresh(flag, now + 60)
        assert not config.keep_flag_fresh(flag, now + config.KEEP_FLAG_MAX_AGE_S + 1)
    finally:
        config.HOME = old_home


def test_repository_has_no_secrets():
    script = ROOT / 'scripts/check-secrets.py'
    if not script.exists():
        pytest.skip('secret scanner not present')
    r = subprocess.run([sys.executable, '-I', str(script), str(ROOT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_disk_full_still_lets_stop_cancel_and_changes_nothing(tmp_path, monkeypatch):
    """PRA-09: with the disk full the step used to raise before the cycle, so STOP could not cancel."""
    import errno
    from fakes import LIVE_CEILINGS, FakeCloud, directive
    from avid_agent.engine import Agent
    from avid_core import fsio
    ex = FakeExchange()
    a = Agent(FakeCloud(directive=directive('live')), ex, tmp_path, lambda: dict(LIVE_CEILINGS), clock=lambda: T0)
    a.step()
    assert len(ex.open_bot_orders()) == 1
    (a.store.dir / 'STOP').write_text('x')
    before = Store(tmp_path).state_path.read_text()

    def full(*args, **kw):
        raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(fsio, 'atomic_write', full)
    monkeypatch.setattr(fsio, 'append_line', full)
    res = a.step()                                        # must not raise
    assert ex.open_bot_orders() == []                     # the cancel request went out without writing
    assert res['record']['event'] == 'emergency_cancel'
    assert Store(tmp_path).state_path.read_text() == before   # nothing released, nothing rewritten
    monkeypatch.undo()                                    # space is back: the normal cycle settles it
    a.step()
    st = Store(tmp_path).load()
    assert not st['bot_orders'] and not st['closing']


def test_disk_full_never_sends_an_order(tmp_path, monkeypatch):
    import errno
    from avid_core import fsio
    ex = FakeExchange()
    real = fsio.atomic_write

    def full_on_state(path, *a, **kw):
        if str(path).endswith('state.json'):
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real(path, *a, **kw)
    monkeypatch.setattr(fsio, 'atomic_write', full_on_state)
    with pytest.raises(OSError):
        cycle(ex, Store(tmp_path), make_cfg(), live=True, now=T0)
    assert ex.add_attempts == 0                           # the intent could not be made durable: nothing sent


def test_shutdown_respects_its_time_budget(tmp_path):
    from fakes import LIVE_CEILINGS, FakeCloud, directive
    from avid_agent.engine import Agent
    ex = FakeExchange()
    ex.deadline = None
    cloud = FakeCloud(directive=directive('live'))
    a = Agent(cloud, ex, tmp_path, lambda: dict(LIVE_CEILINGS), clock=lambda: T0)
    a.step()
    ex.cancel_lag.add(ex.open_bot_orders()[0]['id'])         # the cancel is never applied
    n = len(cloud.reports)
    res = a.safe_shutdown(budget_s=0.0)
    assert not res['ok'] and res['open_bot_orders']          # honest: not confirmed
    assert len(cloud.reports) == n                           # no time left: no final sync attempted
    assert ex.deadline is None                               # the deadline is cleared afterwards
