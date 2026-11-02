#!/usr/bin/env python3
"""Write a CycloneDX 1.5 JSON SBOM for the release (stdlib only).

    python3 scripts/make-sbom.py --out avid.cdx.json dist/*.whl dist/*.tar.gz

Contents, taken from pyproject.toml and requirements-dev.txt (nothing is guessed):
  - the package itself, with the SHA-256 of every artifact given on the command line
  - runtime dependencies: none (AVID uses only the Python standard library)
  - optional dependency of the [live] extra (cryptography, version range as declared)
  - build requirement (setuptools) and the pinned test dependencies, scope "excluded"
The serial number is derived from the artifact hashes, so the same inputs give the same SBOM; the
timestamp is SOURCE_DATE_EPOCH when set.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _toml_list(text: str, key: str) -> list[str]:
    m = re.search(rf'^{re.escape(key)}\s*=\s*\[(.*?)\]', text, re.M | re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def _req(spec: str) -> tuple[str, str]:
    m = re.match(r'\s*([A-Za-z0-9_.-]+)\s*(.*)', spec)
    return m.group(1).lower(), m.group(2).strip()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('artifacts', nargs='*')
    a = ap.parse_args(argv)
    py = (ROOT / 'pyproject.toml').read_text()
    name = re.search(r'^name = "([^"]+)"', py, re.M).group(1)
    version = re.search(r'^version = "([^"]+)"', py, re.M).group(1)
    arts = [Path(x) for x in a.artifacts]
    hashes = {p.name: sha256(p) for p in arts}
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f'avid-sbom:{name}:{version}:' + ','.join(sorted(hashes.values())))
    epoch = os.environ.get('SOURCE_DATE_EPOCH')
    when = dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc) if epoch else dt.datetime.now(dt.timezone.utc)
    root_ref = f'pkg:pypi/{name}@{version}'
    components, deps = [], []
    runtime = _toml_list(py, 'dependencies')
    for spec in runtime:
        n, rng = _req(spec)
        components.append({'type': 'library', 'name': n, 'bom-ref': f'runtime:{n}', 'purl': f'pkg:pypi/{n}',
                           'scope': 'required', 'description': f'runtime dependency {rng}'.strip()})
        deps.append(f'runtime:{n}')
    for spec in _toml_list(py, 'live'):
        n, rng = _req(spec)
        components.append({'type': 'library', 'name': n, 'bom-ref': f'extra-live:{n}', 'purl': f'pkg:pypi/{n}',
                           'scope': 'optional',
                           'description': f'optional, extra [live] {rng}: constant-time Ed25519 for live orders'})
        deps.append(f'extra-live:{n}')
    for spec in _toml_list(py, 'requires'):
        n, rng = _req(spec)
        components.append({'type': 'library', 'name': n, 'bom-ref': f'build:{n}', 'purl': f'pkg:pypi/{n}',
                           'scope': 'excluded', 'description': f'build requirement {rng}'.strip()})
    dev = ROOT / 'requirements-dev.txt'
    if dev.exists():
        for line in dev.read_text().splitlines():
            line = line.split('#')[0].strip()
            m = re.match(r'([A-Za-z0-9_.-]+)==([^\s;]+)$', line)
            if m:
                n, v = m.group(1).lower(), m.group(2)
                components.append({'type': 'library', 'name': n, 'version': v, 'bom-ref': f'dev:{n}@{v}',
                                   'purl': f'pkg:pypi/{n}@{v}', 'scope': 'excluded',
                                   'description': 'pinned test dependency (requirements-dev.txt)'})
    bom = {
        'bomFormat': 'CycloneDX', 'specVersion': '1.5', 'serialNumber': f'urn:uuid:{serial}', 'version': 1,
        'metadata': {
            'timestamp': when.isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'tools': {'components': [{'type': 'application', 'name': 'avid scripts/make-sbom.py'}]},
            'component': {
                'type': 'application', 'name': name, 'version': version, 'bom-ref': root_ref, 'purl': root_ref,
                'licenses': [{'license': {'id': 'MIT'}}],
                'description': 'AVID rebalancing engine and private agent for Nobitex (alpha)',
                'externalReferences': [{'type': 'distribution', 'url': p.name,
                                        'hashes': [{'alg': 'SHA-256', 'content': hashes[p.name]}]} for p in arts]},
        },
        'components': components,
        'dependencies': [{'ref': root_ref, 'dependsOn': deps}],
    }
    Path(a.out).write_text(json.dumps(bom, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
