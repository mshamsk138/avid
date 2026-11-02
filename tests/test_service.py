"""avid-agent install / uninstall / start / stop / restart against a STUB systemctl (PRA-01, PRA-05).

The real systemd user manager is not available in CI containers; the stubs record every call and play
the service: `stop` writes the agent's last_stop.json the way the real agent does (or not, to model a
kill after TimeoutStopSec). The unit text itself is checked with the real `systemd-analyze verify`
when it is installed. A last test runs the agent loop in a REAL child process and sends it SIGTERM."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from avid_agent import config, service

STUB = r'''#!/usr/bin/env bash
# stub for {name}: log the call, play the service from files in $STUB_STATE
echo "{name} $*" >> "$STUB_STATE/calls"
args=" $* "
case "{name}" in
  systemctl)
    case "$args" in
      *" show-environment "*) exit "${STUB_MANAGER_RC:-0}" ;;
      *" daemon-reload "*) exit "${STUB_RELOAD_RC:-0}" ;;
      *" is-active "*) [ -f "$STUB_STATE/active" ] && exit 0 || exit 3 ;;
      *" is-enabled "*) [ -f "$STUB_STATE/enabled" ] && exit 0 || exit 1 ;;
      *" enable --now "*) touch "$STUB_STATE/active" "$STUB_STATE/enabled"; exit 0 ;;
      *" disable "*) rm -f "$STUB_STATE/enabled"; exit 0 ;;
      *" stop "*)
        [ -f "$AVID_AGENT_HOME/keep-orders-on-next-stop" ] && touch "$STUB_STATE/keep_flag_seen_at_stop"
        rm -f "$STUB_STATE/active"
        if [ -f "$STUB_STATE/last_stop" ]; then cp "$STUB_STATE/last_stop" "$AVID_AGENT_HOME/last_stop.json"; fi
        exit 0 ;;
      *" restart "*)
        [ -f "$AVID_AGENT_HOME/keep-orders-on-next-stop" ] && touch "$STUB_STATE/keep_flag_seen_at_restart"
        exit 0 ;;
    esac
    exit 0 ;;
  loginctl) exit "${STUB_LINGER_RC:-0}" ;;
  journalctl) echo '{"event": "no_trade"}'; exit 0 ;;
esac
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home with spaces $HOME 100%'
    home.mkdir()
    stubs, state = tmp_path / 'stubs', tmp_path / 'state'
    stubs.mkdir()
    state.mkdir()
    for name in ('systemctl', 'loginctl', 'journalctl'):
        p = stubs / name
        p.write_text(STUB.replace('{name}', name))
        p.chmod(0o755)
    agent_home = home / '.avid-agent'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.setenv('PATH', f'{stubs}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.setenv('STUB_STATE', str(state))
    monkeypatch.setenv('AVID_AGENT_HOME', str(agent_home))
    monkeypatch.setenv('USER', 'tester')
    monkeypatch.delenv('AVID_AGENT_WRAPPER', raising=False)
    monkeypatch.delenv('NOBITEX_SECRETS_DIR', raising=False)
    monkeypatch.setattr(config, 'HOME', agent_home)
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)          # CI containers often run as root
    return type('Env', (), {'home': home, 'state': state, 'agent_home': agent_home, 'out': []})


def calls(e):
    p = e.state / 'calls'
    return p.read_text().splitlines() if p.exists() else []


def _install(e, **kw):
    out = []
    code = service.install(out=out.append, version='avid-agent/test', **kw)
    return code, out


def test_escape_for_systemd():
    assert service.escape('/a b/c') == '"/a b/c"'
    assert service.escape('100%') == '"100%%"'
    assert service.escape('$HOME') == '"$$HOME"'
    assert service.escape('K=$HOME', command=False) == '"K=$HOME"'      # Environment= does not expand $
    assert service.escape('say "hi" \\ there') == '"say \\"hi\\" \\\\ there"'
    with pytest.raises(service.ServiceError):
        service.escape('two\nlines')


def test_install_is_idempotent_and_backs_up_a_changed_unit(env):
    code, out = _install(env)
    up = service.unit_path()
    assert code == 0 and up.is_file() and str(env.home) in str(up)
    text = up.read_text()
    assert service.MARKER in text and 'TimeoutStopSec=300' in text and 'KillMode=mixed' in text
    assert f'ExecStart={service.escape(sys.executable)} "-m" "avid_agent" "run"' in text
    assert 'systemctl --user daemon-reload' in calls(env)
    assert 'loginctl enable-linger tester' in calls(env)
    n_reload = calls(env).count('systemctl --user daemon-reload')
    code, out = _install(env)                                 # second time: nothing changes
    assert code == 0 and any('unchanged' in x for x in out)
    assert calls(env).count('systemctl --user daemon-reload') == n_reload
    assert not list(up.parent.glob('*.bak-*'))
    up.write_text(text + '# local edit\n')                    # a changed unit is backed up, not lost
    _install(env)
    baks = list(up.parent.glob('*.bak-*'))
    assert len(baks) == 1 and baks[0].read_text().endswith('# local edit\n')
    assert up.read_text() == text


def test_unit_passes_systemd_analyze_verify(env):
    sa = shutil.which('systemd-analyze')
    if sa is None:
        pytest.skip('systemd-analyze is not installed')
    _install(env)
    r = subprocess.run([sa, 'verify', str(service.unit_path())], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert 'Unknown' not in r.stderr and 'Invalid' not in r.stderr, r.stderr


def test_install_checks_before_modifying(env, monkeypatch):
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    with pytest.raises(service.ServiceError, match='root'):
        _install(env)
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(service, 'service_command',
                        lambda: ([sys.executable, '-m', 'avid_agent', 'run'], {'PYTHONPATH': '/nonexistent'}))
    with pytest.raises(service.ServiceError, match='cannot import'):
        _install(env)
    assert not service.unit_path().exists() and calls(env) == []


def test_install_without_a_user_manager(env, monkeypatch):
    monkeypatch.setenv('STUB_MANAGER_RC', '1')
    with pytest.raises(service.ServiceError, match='not reachable'):
        _install(env)
    assert not service.unit_path().exists()
    code, out = _install(env, unit_only=True)
    assert code == 0 and service.unit_path().exists()
    assert 'systemctl --user daemon-reload' not in calls(env)


def test_failed_daemon_reload_restores_the_previous_unit(env, monkeypatch):
    _install(env)
    up = service.unit_path()
    before = up.read_text()
    monkeypatch.setattr(service, 'unit_text', lambda *a, **k: before + '# new\n')
    monkeypatch.setenv('STUB_RELOAD_RC', '1')
    with pytest.raises(service.ServiceError, match='restored'):
        _install(env)
    assert up.read_text() == before
    up.unlink()
    with pytest.raises(service.ServiceError, match='restored'):
        _install(env)
    assert not up.exists()                                     # nothing existed before: nothing left


def test_linger_failure_is_a_note_not_an_error(env, monkeypatch):
    monkeypatch.setenv('STUB_LINGER_RC', '1')
    code, out = _install(env)
    assert code == 0 and any('lingering' in x for x in out)


def test_source_checkout_wrapper_is_linked_on_the_users_path(env, monkeypatch, tmp_path):
    wrapper = tmp_path / 'checkout' / 'scripts' / 'avid-agent'
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/sh\n')
    monkeypatch.setenv('AVID_AGENT_WRAPPER', str(wrapper))
    _install(env)
    link = env.home / '.local' / 'bin' / 'avid-agent'
    assert link.is_symlink() and link.resolve() == wrapper.resolve()
    _install(env)                                              # idempotent
    assert link.resolve() == wrapper.resolve()


def _stop_result(env, res):
    (env.state / 'last_stop').write_text(json.dumps(res))


def test_stop_reports_only_what_the_agent_confirmed(env):
    _install(env)
    (env.state / 'active').touch()
    _stop_result(env, {'ok': True, 'cancelled': True, 'open_bot_orders': [], 'message': 'no bot order left open'})
    out = []
    assert service.stop(out=out.append) == service.EXIT_OK
    assert 'no bot order is left open' in out[-1]

    (env.state / 'active').touch()
    _stop_result(env, {'ok': False, 'cancelled': True, 'open_bot_orders': ['1001'], 'message': 'check the panel'})
    out = []
    assert service.stop(out=out.append) == service.EXIT_NOT_CONFIRMED
    assert 'NOT confirmed' in out[-1] and '1001' in out[-1] and 'no bot order' not in out[-1]

    (env.state / 'active').touch()
    (env.state / 'last_stop').unlink()                         # killed after TimeoutStopSec: no verdict
    out = []
    assert service.stop(out=out.append) == service.EXIT_UNKNOWN
    assert 'MAY still be open' in out[-1]
    assert 'systemctl --user disable avid-agent.service' in calls(env)


def test_stop_keep_orders_sets_the_one_shot_flag(env):
    _install(env)
    (env.state / 'active').touch()
    _stop_result(env, {'ok': True, 'cancelled': False, 'message': 'kept'})
    assert service.stop(keep_orders=True, out=lambda *_: None) == 0
    assert (env.state / 'keep_flag_seen_at_stop').exists()
    assert not (env.agent_home / config.KEEP_FLAG).exists()   # consumed / removed afterwards


def test_stop_when_not_running_cancels_in_process(env):
    _install(env)
    seen = []

    def safe_stop():
        seen.append(1)
        return {'ok': False, 'open_bot_orders': ['77'], 'message': 'exchange unreachable'}
    assert service.stop(safe_stop=safe_stop, out=lambda *_: None) == service.EXIT_NOT_CONFIRMED
    assert seen == [1]
    assert service.stop(keep_orders=True, safe_stop=safe_stop, out=lambda *_: None) == 0
    assert seen == [1]                                          # --keep-orders never cancels


def test_restart_keeps_orders_and_start_needs_pairing(env):
    _install(env)
    with pytest.raises(config.ConfigError):
        service.start(out=lambda *_: None)                      # not paired yet
    config.save_agent('https://avid.example.ir', 'agent-1', b'\x01' * 32)
    assert service.start(out=lambda *_: None) == 0
    assert (env.state / 'active').exists()
    assert service.restart(out=lambda *_: None) == 0
    assert (env.state / 'keep_flag_seen_at_restart').exists()
    assert not (env.agent_home / config.KEEP_FLAG).exists()


def test_start_requires_the_unit(env):
    with pytest.raises(service.ServiceError, match='not installed'):
        service.start(out=lambda *_: None)


def test_uninstall_keeps_user_data_and_refuses_foreign_units(env):
    nob = env.home / '.nobitex'
    nob.mkdir()
    (nob / 'api_key').write_text('k')
    _install(env)
    config.save_agent('https://avid.example.ir', 'agent-1', b'\x01' * 32)
    (env.state / 'active').touch()
    _stop_result(env, {'ok': False, 'open_bot_orders': ['5'], 'message': 'x'})
    out = []
    assert service.uninstall(out=out.append) == service.EXIT_NOT_CONFIRMED
    assert service.unit_path().exists()                          # not removed while orders are unconfirmed
    (env.state / 'active').touch()
    _stop_result(env, {'ok': True, 'cancelled': True, 'open_bot_orders': []})
    out = []
    assert service.uninstall(out=out.append) == 0
    assert not service.unit_path().exists()
    assert (env.agent_home / 'agent.json').exists() or any(env.agent_home.iterdir())
    assert (nob / 'api_key').read_text() == 'k'
    assert any('kept your data' in x for x in out)
    service.unit_path().write_text('[Service]\nExecStart=/bin/true\n')
    with pytest.raises(service.ServiceError, match='not written by avid-agent'):
        service.uninstall(out=lambda *_: None)
    assert service.unit_path().exists()


def test_agent_cli_has_the_whole_command_set(env, capsys):
    from avid_agent.__main__ import main
    assert main(['help']) == 0
    text = capsys.readouterr().out
    for cmd in ('install', 'uninstall', 'pair', 'once', 'start', 'stop', 'restart', 'status', 'logs', 'ceilings',
                'resolve', 'help'):
        assert f'avid-agent {cmd}' in text, cmd
    assert main(['status', '--json']) == 0                     # works before pairing
    st = json.loads(capsys.readouterr().out)
    assert st['cloud'] is None and st['service']['installed'] is False
    assert main(['logs', '-n', '5']) == 0
    assert main(['start']) == 2                                # not installed -> clear error, exit 2


# ------------------------------------------------------------------------------- a real SIGTERM
CHILD = textwrap.dedent('''
    import json, sys, time
    sys.path[:0] = {paths!r}
    from fakes import FakeCloud, FakeExchange, LIVE_CEILINGS, directive
    from avid_agent.engine import Agent
    from avid_agent.__main__ import serve
    from pathlib import Path
    home = Path({home!r})
    ex = FakeExchange(usdt=100.0)
    ex.now = time.time()
    agent = Agent(FakeCloud(directive=directive('live')), ex, home, lambda: dict(LIVE_CEILINGS))
    code = serve(agent, 3600, home, out=lambda t: print(t, flush=True))
    print(json.dumps({{'exit': code, 'open': len(ex.open_bot_orders()), 'placed': ex.add_attempts}}), flush=True)
    sys.exit(code)
''')


def test_sigterm_is_a_controlled_stop_in_a_real_process(tmp_path, monkeypatch):
    home = tmp_path / 'agent home'
    monkeypatch.setattr(config, 'HOME', home)
    root = Path(__file__).resolve().parent.parent
    paths = [str(root / 'tests'), str(root / 'agent'), str(root)]
    env = dict(os.environ, AVID_AGENT_HOME=str(home))
    p = subprocess.Popen([sys.executable, '-c', CHILD.format(paths=paths, home=str(home))], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        first = p.stdout.readline()                               # the first step is done (an order is open)
        assert '"event"' in first, (first, p.stderr.read() if p.poll() is not None else '')
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    final = json.loads(out.strip().splitlines()[-1])
    assert p.returncode == 0 and final == {'exit': 0, 'open': 0, 'placed': 1}, (out, err)
    last = json.loads((home / service.LAST_STOP).read_text())
    assert last['ok'] is True and last['cancelled'] is True and last['open_bot_orders'] == []
