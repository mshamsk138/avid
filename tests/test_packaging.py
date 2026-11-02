"""Packaging: the wheel and sdist build, install into a CLEAN virtual environment, and both commands
work there without PYTHONPATH (PRA-01). Uses only what is installed locally (no package index):
the wheel is installed with --no-index --no-deps, which is possible because AVID has no runtime
dependencies. Marked slow: it creates virtual environments."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.slow


def _setuptools_ok():
    r = subprocess.run([sys.executable, '-c', 'import setuptools,sys; v=tuple(int(x) for x in '
                        'setuptools.__version__.split(".")[:2]); sys.exit(0 if v >= (77, 0) else 1)'],
                       capture_output=True)
    return r.returncode == 0


def _version():
    return re.search(r'^version = "([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.M).group(1)


@pytest.fixture(scope='module')
def dist(tmp_path_factory):
    if not _setuptools_ok():
        pytest.skip('setuptools >= 77 is needed to build (PEP 639 license metadata)')
    src = tmp_path_factory.mktemp('src') / 'avid src'
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns('__pycache__', '*.egg-info', 'build', 'dist', '.git'))
    out = tmp_path_factory.mktemp('dist')
    code = ('import setuptools.build_meta as b; '
            f's = b.build_sdist({str(out)!r}); w = b.build_wheel({str(out)!r}); print("AVID-DIST", s, w)')
    r = subprocess.run([sys.executable, '-c', code], cwd=src, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    sdist, wheel = [x for x in r.stdout.splitlines() if x.startswith('AVID-DIST ')][-1].split()[1:]
    return {'src': src, 'sdist': out / sdist, 'wheel': out / wheel}


def _venv(path: Path, *, system=False) -> Path:
    args = [sys.executable, '-m', 'venv', str(path)]
    if system:
        args.insert(3, '--system-site-packages')
    subprocess.run(args, check=True, capture_output=True, timeout=300)
    return path / 'bin'


def _clean_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'AVID_', 'NOBITEX_', 'VIRTUAL_ENV'))}
    env.update(HOME=str(home), PATH=os.environ.get('PATH', '/usr/bin:/bin'))
    return env


def _run(args, env, cwd, timeout=300):
    return subprocess.run([str(a) for a in args], env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def test_versions_are_consistent():
    import avid_agent
    import avid_core
    from avid_agent import cloud, engine
    from avid_core import client
    v = _version()
    assert avid_core.__version__ == avid_agent.__version__ == v
    assert client.UA.endswith(v) and cloud.UA.endswith(v) and engine.VERSION.endswith(v)
    assert f'## [{v}]' in (ROOT / 'CHANGELOG.md').read_text()


def test_wheel_and_sdist_contents(dist):
    v = _version()
    assert dist['wheel'].name == f'avid_rebalancer-{v}-py3-none-any.whl'
    names = zipfile.ZipFile(dist['wheel']).namelist()
    for need in ('avid_core/cli.py', 'avid_core/sim.py', 'avid_core/examples/btc-usdt.json', 'avid_core/py.typed',
                 'avid_agent/service.py', f'avid_rebalancer-{v}.dist-info/licenses/LICENSE'):
        assert need in names, need
    assert not [n for n in names if n.startswith(('tests/', 'security/')) or '__pycache__' in n]
    meta = zipfile.ZipFile(dist['wheel']).read(f'avid_rebalancer-{v}.dist-info/METADATA').decode()
    assert 'License-Expression: MIT' in meta and 'Requires-Python: >=3.10' in meta
    assert 'Provides-Extra: live' in meta
    eps = zipfile.ZipFile(dist['wheel']).read(f'avid_rebalancer-{v}.dist-info/entry_points.txt').decode()
    assert 'avid-engine = avid_core.cli:main' in eps and 'avid-agent = avid_agent.__main__:main' in eps
    with tarfile.open(dist['sdist']) as t:
        members = t.getnames()
    for need in ('LICENSE', 'pyproject.toml', 'tests/test_sim_cli.py', 'security/test_findings.py',
                 'scripts/avid-agent', 'avid_core/examples/btc-usdt.json'):
        assert any(m.endswith('/' + need) for m in members), need


def test_clean_venv_install_runs_both_commands_without_pythonpath(dist, tmp_path):
    home = tmp_path / 'home dir'
    home.mkdir()
    bin_ = _venv(tmp_path / 'venv with space')
    env = _clean_env(home)
    r = _run([bin_ / 'python', '-m', 'pip', 'install', '--no-index', '--no-deps', dist['wheel']], env, tmp_path)
    assert r.returncode == 0, r.stderr
    v = _version()
    r = _run([bin_ / 'avid-engine', '--version'], env, home)
    assert r.returncode == 0 and r.stdout.strip() == f'avid-engine {v}', r.stderr
    r = _run([bin_ / 'avid-engine', 'simulate', '--example', 'btc-usdt', '--steps', '6', '--json'], env, home)
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    assert rep['kind'] == 'SIMULATION' and rep['first_decision']['order']['amount'] == 0.000333
    r = _run([bin_ / 'avid-engine', 'status', '--root', home], env, home)
    assert r.returncode == 0 and 'no engine data' in r.stdout
    r = _run([bin_ / 'avid-agent', '--version'], env, home)
    assert r.returncode == 0 and r.stdout.strip() == f'avid-agent/{v}', r.stderr
    r = _run([bin_ / 'avid-agent', 'help'], env, home)
    assert r.returncode == 0 and 'avid-agent install' in r.stdout
    r = _run([bin_ / 'avid-agent', 'status', '--json'], env, home)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)['cloud'] is None
    r = _run([bin_ / 'avid-engine', 'check'], env, home)            # no credentials: clear error, exit 2
    assert r.returncode == 2 and 'no credentials' in r.stderr and 'Traceback' not in r.stderr
    # reinstall, then uninstall: the commands go away, the user's data (none here) is untouched
    r = _run([bin_ / 'python', '-m', 'pip', 'install', '--no-index', '--no-deps', '--force-reinstall', dist['wheel']],
             env, tmp_path)
    assert r.returncode == 0, r.stderr
    r = _run([bin_ / 'python', '-m', 'pip', 'uninstall', '-y', 'avid-rebalancer'], env, tmp_path)
    assert r.returncode == 0 and not (bin_ / 'avid-engine').exists() and not (bin_ / 'avid-agent').exists()


def test_sdist_installs_too(dist, tmp_path):
    if not _setuptools_ok():
        pytest.skip('setuptools >= 77 needed')
    bin_ = _venv(tmp_path / 'venv', system=True)                     # system site: the local setuptools
    env = _clean_env(tmp_path)
    r = _run([bin_ / 'python', '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-build-isolation',
              dist['sdist']], env, tmp_path)
    assert r.returncode == 0, r.stderr[-2000:]
    r = _run([bin_ / 'avid-engine', 'simulate', '--steps', '2', '--json'], env, tmp_path)
    assert r.returncode == 0, r.stderr


def test_editable_install(dist, tmp_path):
    bin_ = _venv(tmp_path / 'venv', system=True)
    env = _clean_env(tmp_path)
    r = _run([bin_ / 'python', '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-build-isolation', '-e',
              dist['src']], env, tmp_path)
    assert r.returncode == 0, r.stderr[-2000:]
    r = _run([bin_ / 'python', '-c', 'import avid_core, avid_agent; print(avid_core.__file__)'], env, tmp_path)
    assert r.returncode == 0 and str(dist['src']) in r.stdout, r.stdout + r.stderr
    r = _run([bin_ / 'avid-agent', 'help'], env, tmp_path)
    assert r.returncode == 0


def test_live_extra_resolves_when_cryptography_is_present(dist, tmp_path):
    try:
        import cryptography  # noqa: F401
    except Exception:
        pytest.skip('cryptography is not installed here; the [live] extra needs a package index')
    bin_ = _venv(tmp_path / 'venv', system=True)
    env = _clean_env(tmp_path)
    r = _run([bin_ / 'python', '-m', 'pip', 'install', '--no-index', f'{dist["wheel"]}[live]'], env, tmp_path)
    if r.returncode != 0 and 'No matching distribution' in r.stderr:
        pytest.skip('the local cryptography does not satisfy the extra without an index')
    assert r.returncode == 0, r.stderr[-1500:]
    r = _run([bin_ / 'python', '-c', 'from avid_core import ed25519; print(ed25519.backend())'], env, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() in ('cryptography', 'pure'), r.stderr
