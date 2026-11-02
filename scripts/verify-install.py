#!/usr/bin/env python3
"""Install a built wheel (and sdist) into fresh virtual environments and check that AVID works there.

    python3 scripts/verify-install.py dist/avid_rebalancer-<v>-py3-none-any.whl [dist/avid_rebalancer-<v>.tar.gz]

Offline: wheels are installed with --no-index --no-deps (AVID has no runtime dependencies). Every step
prints the command, its exit code and the first lines of its output, and the expected result; the
script exits 1 if any expectation fails. It uses a temporary HOME (with spaces in its path), so it never
touches your own ~/.avid-agent, ~/.nobitex or systemd units. `avid-agent install --unit-only` writes
the unit into that temporary HOME only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FAILED = []


def step(title, args, *, env, cwd, expect_rc=0, expect_in=None, expect_not_in=None, show=6):
    r = subprocess.run([str(a) for a in args], env=env, cwd=cwd, capture_output=True, text=True, timeout=600)
    out = (r.stdout + r.stderr).strip()
    ok = (r.returncode == expect_rc if expect_rc is not None else True) and \
        (expect_in is None or expect_in in out) and (expect_not_in is None or expect_not_in not in out)
    shown = ' '.join(str(a) for a in args).replace(str(cwd), '.')
    print(f'\n[{"PASS" if ok else "FAIL"}] {title}\n  $ {shown}\n  exit {r.returncode} (expected {expect_rc})'
          + (f'; expected output containing {expect_in!r}' if expect_in else ''))
    for line in out.splitlines()[:show]:
        print('  | ' + line[:150])
    if not ok:
        FAILED.append(title)
    return r


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    wheel = Path(argv[1]).resolve()
    sdist = Path(argv[2]).resolve() if len(argv) > 2 else None
    work = Path(tempfile.mkdtemp(prefix='avid verify '))
    home = work / 'home dir'
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'AVID_', 'NOBITEX_', 'VIRTUAL_ENV'))}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'))
    print(f'python {sys.version.split()[0]}; wheel {wheel.name}; temporary HOME with spaces: {home}')
    try:
        venv = work / 'venv with space'
        step('create a clean virtual environment (no system site packages)', [sys.executable, '-m', 'venv', venv],
             env=env, cwd=work)
        py, bin_ = venv / 'bin' / 'python', venv / 'bin'
        step('install the wheel offline, no dependencies needed', [py, '-m', 'pip', 'install', '--no-index',
             '--no-deps', '--disable-pip-version-check', wheel], env=env, cwd=work, expect_in='Successfully installed')
        step('avid-engine is on the venv PATH without PYTHONPATH', [bin_ / 'avid-engine', '--version'], env=env,
             cwd=home, expect_in='avid-engine 0.')
        step('avid-agent --version', [bin_ / 'avid-agent', '--version'], env=env, cwd=home, expect_in='avid-agent/')
        step('avid-engine help', [bin_ / 'avid-engine', 'help'], env=env, cwd=home, expect_in='simulate')
        step('avid-agent help lists the service commands', [bin_ / 'avid-agent', 'help'], env=env, cwd=home,
             expect_in='avid-agent uninstall')
        step('offline simulation of the bundled example (package data present)',
             [bin_ / 'avid-engine', 'simulate', '--example', 'btc-usdt'], env=env, cwd=home,
             expect_in='Portfolio Value  200.00 USDT', show=16)
        step('simulation as JSON', [bin_ / 'avid-engine', 'simulate', '--example', 'btc-paxg-usdt', '--json',
             '--steps', '5'], env=env, cwd=home, expect_in='"kind": "SIMULATION"', show=3)
        step('status of an empty folder (no network, no files created)', [bin_ / 'avid-engine', 'status', '--root',
             home / 'nothing'], env=env, cwd=home, expect_in='no engine data')
        step('check without credentials: clear error, exit 2, no traceback', [bin_ / 'avid-engine', 'check'],
             env=env, cwd=home, expect_rc=2, expect_not_in='Traceback')
        step('agent status before pairing', [bin_ / 'avid-agent', 'status'], env=env, cwd=home,
             expect_in='not paired')
        root_flag = ['--allow-root'] if os.geteuid() == 0 else []
        step('agent install --unit-only into the temporary HOME' + (' (root container: --allow-root)' if root_flag else ''),
             [bin_ / 'avid-agent', 'install', '--unit-only', '--no-linger', *root_flag], env=env, cwd=home,
             expect_in='service unit written', show=8)
        unit = home / '.config' / 'systemd' / 'user' / 'avid-agent.service'
        if unit.exists() and shutil.which('systemd-analyze'):
            step('the unit passes systemd-analyze verify', ['systemd-analyze', 'verify', unit], env=env, cwd=home)
            print('  | ExecStart: ' + next(x for x in unit.read_text().splitlines() if x.startswith('ExecStart=')))
        step('install again: idempotent', [bin_ / 'avid-agent', 'install', '--unit-only', '--no-linger', *root_flag],
             env=env, cwd=home, expect_in='unchanged')
        step('agent start before pairing: refused, exit 2', [bin_ / 'avid-agent', 'start'], env=env, cwd=home,
             expect_rc=2)
        step('agent uninstall: removes the unit, keeps user data', [bin_ / 'avid-agent', 'uninstall'], env=env,
             cwd=home, expect_in='kept your data', show=8)
        if unit.exists():
            FAILED.append('unit still present after uninstall')
        step('reinstall the same wheel (force)', [py, '-m', 'pip', 'install', '--no-index', '--no-deps',
             '--force-reinstall', '--disable-pip-version-check', wheel], env=env, cwd=work,
             expect_in='Successfully installed')
        step('pip uninstall', [py, '-m', 'pip', 'uninstall', '-y', 'avid-rebalancer'], env=env, cwd=work,
             expect_in='Successfully uninstalled')
        print(f'\n[{"PASS" if not (bin_ / "avid-engine").exists() else "FAIL"}] commands removed by pip uninstall')
        if (bin_ / 'avid-engine').exists():
            FAILED.append('commands left after uninstall')
        if sdist:
            venv2 = work / 'venv-sdist'
            step('venv with system site packages (local setuptools) for the sdist build',
                 [sys.executable, '-m', 'venv', '--system-site-packages', venv2], env=env, cwd=work)
            step('install from the sdist (builds a wheel locally)', [venv2 / 'bin' / 'python', '-m', 'pip', 'install',
                 '--no-index', '--no-deps', '--no-build-isolation', '--disable-pip-version-check', sdist], env=env,
                 cwd=work, expect_in='Successfully installed')
            step('avid-engine from the sdist install', [venv2 / 'bin' / 'avid-engine', 'simulate', '--steps', '2'],
                 env=env, cwd=home, expect_in='SIMULATION')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f'\n{len(FAILED)} failed expectation(s)' + (': ' + '; '.join(FAILED) if FAILED else ''))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
