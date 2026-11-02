"""The agent as a background service: systemd --user unit management (no root, no system folders).

    avid-agent install [--no-linger] [--unit-only]   write ~/.config/systemd/user/avid-agent.service
    avid-agent uninstall [--keep-orders] [--force]   controlled stop, remove the unit; keeps your data
    avid-agent start | stop [--keep-orders] | restart | logs [-f] [-n N]

Design (report Part 6):
- nothing runs as root and nothing is written outside the user's home; `install` refuses to run as root
- every check happens BEFORE anything is modified (Python can import the agent with the unit's
  environment, the systemd user manager answers); a differing unit is backed up, never lost; a failed
  daemon-reload restores the previous unit
- idempotent: installing twice leaves one identical unit and no extra backup
- paths with spaces, %, $, quotes and backslashes are escaped for systemd
- stop = SIGTERM = controlled stop: no new orders, the bot's own open orders are cancelled, the result is
  written to ~/.avid-agent/last_stop.json and `stop` reports exactly that - never "stopped cleanly" when
  the cancellation was not confirmed (exit 3) or the result is unknown (exit 4)
- restart keeps the bot's orders (one-shot keep flag, honoured for 10 minutes)
- uninstall never deletes ~/.avid-agent (agent identity, state, outbox) or ~/.nobitex (API key)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import site
import subprocess
import sys
import time
from pathlib import Path

from avid_core import fsio
from avid_core.validation import loads_strict

from . import config

UNIT_NAME = 'avid-agent.service'
MARKER = '# Managed by `avid-agent install`.'
LAST_STOP = 'last_stop.json'
TIMEOUT_STOP_S = 300          # >= drain of the current step (60 s) + one call (20 s) + shutdown budget (150 s)
                              #    + final report (20 s), see engine.SHUTDOWN_BUDGET_S
STEP_DRAIN_S = 60             # after SIGTERM the current step may still talk to the exchange this long
ENV_PASSTHROUGH = ('AVID_AGENT_HOME', 'NOBITEX_SECRETS_DIR', 'NOBITEX_NETWORK', 'NOBITEX_ALLOW_LOOSE_TOKEN_PERMS')
EXIT_OK, EXIT_USAGE, EXIT_NOT_CONFIRMED, EXIT_UNKNOWN = 0, 2, 3, 4


class ServiceError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------ paths
def unit_path() -> Path:
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'systemd' / 'user' / UNIT_NAME


def _tool(name):
    return shutil.which(name)


def _run(args, *, timeout=30, check=False):
    exe = _tool(args[0])
    if exe is None:
        raise ServiceError(f'{args[0]} not found: this command needs systemd (a systemd --user session)')
    r = subprocess.run([exe, *args[1:]], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise ServiceError(f'{" ".join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}')
    return r


def systemctl(*args, timeout=30, check=False):
    return _run(['systemctl', '--user', *args], timeout=timeout, check=check)


def is_active() -> bool:
    try:
        return systemctl('is-active', '--quiet', UNIT_NAME).returncode == 0
    except ServiceError:
        return False


def is_enabled() -> bool:
    try:
        return systemctl('is-enabled', '--quiet', UNIT_NAME).returncode == 0
    except ServiceError:
        return False


# ------------------------------------------------------------------------------------------ unit text
def escape(arg: str, *, command: bool = True) -> str:
    """One double-quoted systemd word. Backslash and quote are C-escaped and % (specifiers) doubled
    everywhere; $ is doubled only on command lines (ExecStart= expands $VAR, Environment= does not -
    systemd.service "Command lines", systemd.exec "Environment=")."""
    if any(c in arg for c in '\n\r\0'):
        raise ServiceError('a path or value with a line break or NUL cannot go into a systemd unit')
    s = arg.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        s = s.replace('$', '$$')
    return f'"{s}"'


def _package_roots() -> list[str]:
    """Folders that must be on PYTHONPATH for the unit's python to import avid_agent and avid_core
    (empty for a pip install, the source folders for a checkout)."""
    import avid_agent
    import avid_core
    std = set()
    for p in list(site.getsitepackages()) + [site.getusersitepackages()]:
        try:
            std.add(str(Path(p).resolve()))
        except OSError:
            pass
    roots = []
    for mod in (avid_core, avid_agent):
        parent = str(Path(mod.__file__).resolve().parent.parent)
        if parent not in std and parent not in roots:
            roots.append(parent)
    return roots


def service_command() -> tuple[list[str], dict]:
    """(ExecStart argv, Environment) for the unit: this very interpreter, `-m avid_agent run`."""
    env = {'PYTHONUNBUFFERED': '1'}
    roots = _package_roots()
    if roots:
        env['PYTHONPATH'] = os.pathsep.join(roots)
    for k in ENV_PASSTHROUGH:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return [sys.executable, '-m', 'avid_agent', 'run'], env


def unit_text(argv: list[str], env: dict, *, version: str) -> str:
    lines = [MARKER + f' AVID {version}. Re-run it to update; a changed unit is backed up first.',
             '[Unit]',
             'Description=AVID Private Agent (Nobitex rebalancer)',
             'After=network-online.target',
             'Wants=network-online.target',
             'StartLimitIntervalSec=600',
             'StartLimitBurst=5',
             '',
             '[Service]',
             'Type=simple',
             'ExecStart=' + ' '.join(escape(a) for a in argv)]
    lines += ['Environment=' + escape(f'{k}={v}', command=False) for k, v in sorted(env.items())]
    lines += ['# SIGTERM = controlled stop: no new orders, cancel the bot\'s own open orders, report.',
              f'# It is time-bounded inside the agent; {TIMEOUT_STOP_S} s covers the worst case.',
              f'TimeoutStopSec={TIMEOUT_STOP_S}',
              'KillSignal=SIGTERM',
              'KillMode=mixed',
              'Restart=always',
              'RestartSec=30',
              '# exit 2 = configuration error (not paired, bad ceilings): restarting would not help',
              'RestartPreventExitStatus=2',
              'UMask=0077',
              'NoNewPrivileges=yes',
              '',
              '[Install]',
              'WantedBy=default.target',
              '']
    return '\n'.join(lines)


# ------------------------------------------------------------------------------------------ install
def _check_import(argv, env):
    """The unit's python must import the agent with the unit's environment - checked before writing."""
    full = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    full.update(env)
    r = subprocess.run([argv[0], '-c', 'import avid_agent, avid_core'], env=full, capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise ServiceError(f'{argv[0]} cannot import the agent with the service environment: '
                           f'{(r.stderr or "").strip()[-300:]}')


def _link_command(out) -> None:
    """A source checkout: put its wrapper on the user's own PATH (~/.local/bin), never system-wide."""
    wrapper = os.environ.get('AVID_AGENT_WRAPPER')
    if not wrapper:
        return
    target = Path(wrapper).resolve()
    link = Path.home() / '.local' / 'bin' / 'avid-agent'
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or not link.exists():
        if not (link.is_symlink() and link.resolve() == target):
            tmp = link.with_name('.avid-agent.link.tmp')
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(target)
            os.replace(tmp, link)
        out(f'command: {link} -> {target}')
    else:
        out(f'note: {link} exists and is not a link; left as it is')
    if str(link.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        out(f'note: add {link.parent} to your PATH (e.g. in ~/.profile)')


def install(*, linger=True, unit_only=False, allow_root=False, out=print, version='') -> int:
    if hasattr(os, 'geteuid') and os.geteuid() == 0 and not allow_root:
        raise ServiceError('do not install the agent as root: it is a per-user service and needs no privileges '
                           '(use your normal account; --allow-root exists only for single-user containers)')
    argv, env = service_command()
    _check_import(argv, env)
    manager_ok = _tool('systemctl') is not None and systemctl('show-environment').returncode == 0
    if not manager_ok and not unit_only:
        raise ServiceError('the systemd user manager is not reachable (no `systemctl --user`). Run the agent in a '
                           'terminal instead (avid-agent run), or write the unit anyway with --unit-only')
    text = unit_text(argv, env, version=version)
    up = unit_path()
    up.parent.mkdir(parents=True, exist_ok=True)
    old = up.read_text() if up.exists() else None
    backup = None
    if old == text:
        out(f'service unit unchanged: {up}')
    else:
        if old is not None:
            backup = up.with_name(f'{UNIT_NAME}.bak-{time.strftime("%Y%m%dT%H%M%S")}')
            shutil.copy2(up, backup)
            out(f'previous unit backed up: {backup}')
        fsio.atomic_write(up, text, mode=0o644)
        out(f'service unit written: {up}')
        if manager_ok:
            r = systemctl('daemon-reload')
            if r.returncode != 0:                       # recovery: put the previous state back
                if backup is not None:
                    shutil.copy2(backup, up)
                else:
                    up.unlink(missing_ok=True)
                raise ServiceError(f'systemctl --user daemon-reload failed ({r.stderr.strip()[:200]}); the previous '
                                   'unit was restored')
    if manager_ok and linger:
        user = os.environ.get('USER') or os.environ.get('LOGNAME') or ''
        try:
            r = _run(['loginctl', 'enable-linger', user]) if user else None
            if r is None or r.returncode != 0:
                out('note: could not enable lingering; the agent runs only while you are logged in '
                    '(an administrator can run: loginctl enable-linger ' + (user or '<user>') + ')')
        except ServiceError:
            out('note: loginctl not found; the agent runs only while you are logged in')
    _link_command(out)
    try:
        import cryptography  # noqa: F401
    except Exception:
        out('note: live orders need python3-cryptography (sudo apt install -y python3-cryptography, or '
            'pip install "avid-rebalancer[live]")')
    secrets = Path(os.environ.get('NOBITEX_SECRETS_DIR') or Path.home() / '.nobitex')
    if not ((secrets / 'api_key').exists() or (secrets / 'token').exists()):
        out(f'note: no Nobitex key in {secrets} yet')
    out('next: avid-agent pair --cloud <your AVID Cloud URL> --code <code>, then avid-agent start')
    return EXIT_OK


# ------------------------------------------------------------------------------------------ stop / start
def _last_stop_path() -> Path:
    return config.home() / LAST_STOP


def write_last_stop(res: dict, now: float | None = None) -> None:
    """Written by the running agent when its controlled stop is over (the service's own verdict)."""
    rec = {'at': dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc)
           .isoformat(timespec='seconds'), 'pid': os.getpid(), **res}
    fsio.atomic_write(_last_stop_path(), json.dumps(rec, ensure_ascii=False, default=str), mode=0o600)


def read_last_stop() -> dict | None:
    try:
        d = loads_strict(_last_stop_path().read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def stop_verdict(res: dict | None, *, keep_orders: bool) -> tuple[int, str]:
    """(exit code, message) for what the agent reported - never a success it did not confirm."""
    if res is None:
        return EXIT_UNKNOWN, ('the service stopped but did not report how its stop ended (killed after '
                              f'{TIMEOUT_STOP_S} s, or crashed). Bot orders MAY still be open: check the Nobitex '
                              'panel, then run  avid-agent status')
    if res.get('cancelled') is False and res.get('ok', True):
        if keep_orders:
            return EXIT_OK, 'agent stopped; open bot orders were kept (as requested)'
        return EXIT_OK, f'agent stopped; {res.get("message") or "nothing was cancelled"}'
    if res.get('ok'):
        return EXIT_OK, 'agent stopped; no bot order is left open'
    left = res.get('open_bot_orders') or []
    return EXIT_NOT_CONFIRMED, ('agent stopped, but the cancellation of these bot orders was NOT confirmed: '
                                + (', '.join(map(str, left)) or 'unknown') + '. Check them in the Nobitex panel. '
                                + str(res.get('message') or ''))


def _require_unit():
    if not unit_path().exists():
        raise ServiceError('the service is not installed: run  avid-agent install')


def start(out=print) -> int:
    _require_unit()
    config.load_agent()                                  # raises ConfigError when not paired
    systemctl('enable', '--now', UNIT_NAME, check=True)
    out('agent started (avid-agent status, avid-agent logs)')
    return EXIT_OK


def stop(*, keep_orders=False, safe_stop=None, out=print) -> int:
    """Controlled stop of the service (or, if it is not running, of anything the bot left open)."""
    flag = config.home() / config.KEEP_FLAG
    if is_active():
        if keep_orders:
            config.write_keep_flag()
        _last_stop_path().unlink(missing_ok=True)
        out('stopping: the agent finishes its step, cancels the bot\'s own open orders and reports '
            f'(up to {TIMEOUT_STOP_S // 60} minutes)' if not keep_orders else 'stopping (orders kept)')
        r = systemctl('stop', UNIT_NAME, timeout=TIMEOUT_STOP_S + 60)
        res = read_last_stop()
        if r.returncode != 0 and res is None:
            out(f'warning: systemctl stop answered {r.returncode}: {(r.stderr or "").strip()[:200]}')
    elif keep_orders:
        res = {'ok': True, 'cancelled': False, 'message': 'the service was not running; nothing was done'}
    else:
        if safe_stop is None:
            res = {'ok': True, 'cancelled': False, 'message': 'the service was not running'}
        else:
            out('the service is not running: cancelling what the bot may have left open')
            res = {'cancelled': True, **safe_stop()}
    flag.unlink(missing_ok=True)
    if unit_path().exists():
        systemctl('disable', UNIT_NAME)                  # do not start again at boot
    code, msg = stop_verdict(res, keep_orders=keep_orders)
    out(msg)
    return code


def restart(out=print) -> int:
    _require_unit()
    if not is_active():
        return start(out)
    config.write_keep_flag()                             # a restart keeps (and keeps managing) open orders
    try:
        systemctl('restart', UNIT_NAME, timeout=TIMEOUT_STOP_S + 60, check=True)
    finally:
        (config.home() / config.KEEP_FLAG).unlink(missing_ok=True)
    out('agent restarted (open bot orders kept and still managed)')
    return EXIT_OK


def uninstall(*, keep_orders=False, force=False, safe_stop=None, out=print) -> int:
    up = unit_path()
    if up.exists() and MARKER not in up.read_text() and not force:
        raise ServiceError(f'{up} was not written by avid-agent install; not removed (use --force)')
    code = EXIT_OK
    if up.exists() or is_active():
        code = stop(keep_orders=keep_orders, safe_stop=safe_stop, out=out)
        if code != EXIT_OK and not force:
            out('uninstall stopped here: settle the orders above first, then run  avid-agent uninstall  again '
                '(or --force to remove the service anyway)')
            return code
    if up.exists():
        up.unlink()
        out(f'removed {up}')
        try:
            systemctl('daemon-reload')
        except ServiceError:
            pass
    link = Path.home() / '.local' / 'bin' / 'avid-agent'
    if link.is_symlink() and link.resolve().name == 'avid-agent' and link.resolve().parent.name == 'scripts':
        link.unlink()
        out(f'removed {link}')
    out(f'kept your data: {config.HOME} (agent identity, state, outbox) and '
        f'{os.environ.get("NOBITEX_SECRETS_DIR") or Path.home() / ".nobitex"} (Nobitex API key). To remove them for '
        'good: revoke the agent in AVID Cloud and the API key in the Nobitex panel first, then delete the folders.')
    return code


def logs(*, follow=False, lines=50) -> int:
    exe = _tool('journalctl')
    if exe is None:
        raise ServiceError('journalctl not found')
    args = [exe, '--user', '-u', UNIT_NAME, '-n', str(int(lines)), '--no-pager', '-o', 'cat']
    if follow:
        args.append('-f')
    return subprocess.call(args)


def service_state() -> dict:
    return {'installed': unit_path().exists(), 'active': is_active(), 'enabled': is_enabled(),
            'unit': str(unit_path()), 'last_stop': read_last_stop()}
