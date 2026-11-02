#!/usr/bin/env python3
"""Pre-publication secret scan (report 10.4). Exit 1 when anything that looks like a credential, a real
configuration or runtime data is found in the tree. Run before every release:

    python3 -I scripts/check-secrets.py .

It does not replace rotating keys: a key that was EVER committed or shared must be revoked in the
Nobitex panel / regenerated, deleting it from the latest version is not enough. Also scan the git
history (e.g. `git log -p | python3 -I scripts/check-secrets.py -`).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', 'node_modules', '.venv', 'venv'}
BAD_NAMES = {'api_key', 'secret', 'token', 'agent_key', 'mfa.key', '.env', 'id_ed25519', 'id_rsa',
             'state.json', 'outbox.sqlite3', 'agent_state.json', 'submissions.jsonl', 'log.jsonl'}
BAD_SUFFIXES = ('.pem', '.key', '.sqlite3', '.p12', '.pfx', '.jsonl.1')
PATTERNS = [
    ('private key block', re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----\s*\n\s*[A-Za-z0-9+/=]{40,}')),
    ('github token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}')),
    ('aws key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('slack token', re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}')),
    ('nobitex token assignment', re.compile(r'(?i)(nobitex[_-]?(token|key|secret))\s*[:=]\s*["\']?[A-Za-z0-9+/=_-]{20,}')),
    ('Authorization header with a token', re.compile(r'Authorization:\s*Token\s+[A-Za-z0-9]{20,}')),
]
# public test vectors (RFC 8032) and the fakes are allowed in tests/
ALLOWED_DIRS = ('tests',)


def scan_text(name, text, findings):
    for what, rx in PATTERNS:
        if rx.search(text):
            findings.append(f'{name}: {what}')


def main(argv):
    if len(argv) > 1 and argv[1] == '-':
        findings = []
        scan_text('<stdin>', sys.stdin.read(), findings)
    else:
        root = Path(argv[1] if len(argv) > 1 else '.').resolve()
        findings = []
        for p in sorted(root.rglob('*')):
            rel = p.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts) or not p.is_file():
                continue
            if p.name in BAD_NAMES or p.name.endswith(BAD_SUFFIXES) or (p.name.startswith('.env') and
                                                                        p.name != '.env.example'):
                findings.append(f'{rel}: file that should never be published')
                continue
            if rel.parts and rel.parts[0] in ALLOWED_DIRS:
                continue
            if any(part == 'data' for part in rel.parts[:-1]):
                findings.append(f'{rel}: runtime data folder')
                continue
            try:
                text = p.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            if rel.as_posix() == 'scripts/check-secrets.py':
                continue
            scan_text(str(rel), text, findings)
    for f in findings:
        print('SECRET?', f)
    print(f'{len(findings)} finding(s)')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
