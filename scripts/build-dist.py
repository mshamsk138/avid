#!/usr/bin/env python3
"""Build the release files into OUT (default dist/): sdist, wheel, source archive, SHA256SUMS, SBOM.

    python3 scripts/build-dist.py [--out dist] [--git-archive]

Uses the PEP 517 hooks of setuptools directly (no `build` package needed; setuptools >= 77). The
source archive is `git archive` of HEAD when --git-archive is given inside a git checkout, otherwise a
reproducible tar.gz of the working tree without .git, caches and build output. Nothing is uploaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


SKIP_PARTS = {'.git', '__pycache__', 'build', 'dist', '.venv', 'venv', '.pytest_cache', 'node_modules'}


def source_tar(dest: Path, prefix: str, out: Path) -> None:
    """The repository tree as a tar.gz (without .git, caches and build output), sorted, with a fixed
    mtime (SOURCE_DATE_EPOCH, default 0) and owner, so the same tree gives the same archive bytes."""
    import gzip
    import tarfile
    mtime = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    paths = sorted(p for p in ROOT.rglob('*') if p.is_file()
                   and not any(part in SKIP_PARTS or part.endswith('.egg-info') for part in p.relative_to(ROOT).parts)
                   and out not in p.parents)
    with open(dest, 'wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=mtime) as gz, \
            tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for p in paths:
            info = tar.gettarinfo(str(p), arcname=f'{prefix}/{p.relative_to(ROOT).as_posix()}')
            info.mtime, info.uid, info.gid, info.uname, info.gname = mtime, 0, 0, '', ''
            info.mode = 0o755 if os.access(p, os.X_OK) else 0o644
            with open(p, 'rb') as f:
                tar.addfile(info, f)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(ROOT / 'dist'))
    ap.add_argument('--git-archive', action='store_true')
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    version = re.search(r'^version = "([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.M).group(1)
    code = ('import setuptools.build_meta as b, json; s = b.build_sdist({o!r}); w = b.build_wheel({o!r}); '
            'print("AVID-DIST " + json.dumps([s, w]))').format(o=str(out))
    r = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-4000:])
        return 1
    sdist, wheel = json.loads([x for x in r.stdout.splitlines() if x.startswith('AVID-DIST ')][-1][10:])
    files = [out / sdist, out / wheel]
    src = out / f'avid-rebalancer-{version}-source.tar.gz'
    if a.git_archive and (ROOT / '.git').exists():
        subprocess.run(['git', 'archive', '--format=tar.gz', f'--prefix=avid-{version}/', '-o', str(src), 'HEAD'],
                       cwd=ROOT, check=True)
    else:
        source_tar(src, f'avid-{version}', out)
    files.append(src)
    sbom = out / f'avid-rebalancer-{version}.cdx.json'
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'make-sbom.py'), '--out', str(sbom),
                    *[str(f) for f in files]], check=True)
    files.append(sbom)
    sums = out / 'SHA256SUMS'
    sums.write_text(''.join(f'{sha256(f)}  {f.name}\n' for f in files))
    for f in files + [sums]:
        print(f'{f.name}\t{f.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    os.umask(0o022)
    sys.exit(main())
