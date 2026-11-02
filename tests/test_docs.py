"""The documentation matches the code where a mismatch would mislead: the order state diagram, the
command lists, the version, and the links (no contradictory or broken documents)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from avid_core import orderstate as ost

ROOT = Path(__file__).resolve().parent.parent


def _section(text, start, end):
    return text[text.index(start) + len(start):text.index(end)]


def test_order_state_diagram_matches_the_code():
    doc = (ROOT / 'docs' / 'ORDER_LIFECYCLE.md').read_text()
    diagram = re.search(r'```mermaid\n(stateDiagram-v2.*?)```', doc, re.S).group(1)
    drawn = set()
    for a, b in re.findall(r'^\s*(\[\*\]|[A-Z_]+)\s*-->\s*(\[\*\]|[A-Z_]+)', diagram, re.M):
        if a == '[*]' or b == '[*]':
            continue
        drawn.add((a, b))
        assert b in ost.ALLOWED.get(a, set()), f'diagram draws {a} -> {b}, which orderstate.ALLOWED refuses'
    listed = set(re.findall(r'`([A-Z_]+) -> ([A-Z_]+)`',
                            _section(doc, '<!-- not-drawn-transitions -->', '<!-- /not-drawn-transitions -->')))
    for a, targets in ost.ALLOWED.items():
        if a is None:
            continue
        for b in targets:
            if a == b:                           # transition() treats a same-state move as a no-op
                continue
            assert (a, b) in drawn | listed, f'{a} -> {b} is allowed in code but neither drawn nor listed'
    for t in ost.TERMINAL:                       # terminal states end in the diagram and nothing leaves them
        assert f'{t} --> [*]' in diagram
        assert t not in ost.ALLOWED


def test_cli_reference_lists_every_command():
    from avid_core.cli import COMMANDS
    ref = (ROOT / 'docs' / 'CLI_REFERENCE.md').read_text()
    for cmd in COMMANDS:
        assert re.search(rf'^\| `{cmd}', ref, re.M), f'avid-engine {cmd} missing from CLI_REFERENCE.md'
    import argparse

    from avid_agent import __main__ as agent_main
    seen = []
    orig = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        for a in self._actions:
            if isinstance(a, argparse._SubParsersAction):
                seen.extend(a.choices)
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = capture
    try:
        try:
            agent_main.main(['help'])
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = orig
    assert {'install', 'uninstall', 'pair', 'once', 'start', 'stop', 'restart', 'status', 'logs', 'ceilings',
            'resolve', 'help'} <= set(seen)
    for cmd in seen:
        assert re.search(rf'^\| `{cmd}[ `]', ref, re.M), f'avid-agent {cmd} missing from CLI_REFERENCE.md'


def test_readmes_state_the_current_version_and_status():
    v = re.search(r'^version = "([^"]+)"', (ROOT / 'pyproject.toml').read_text(), re.M).group(1)
    for name in ('README.md', 'README_FA.md', 'KNOWN_ISSUES.md'):
        text = (ROOT / name).read_text()
        assert v in text, f'{name} does not mention {v}'
    en = (ROOT / 'README.md').read_text()
    assert 'Not production-ready' in en and 'Alpha' in en
    assert 'production-ready' not in en.replace('Not production-ready', '').replace('not production-ready', '')


def test_markdown_links_resolve():
    r = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts' / 'check-links.py'), str(ROOT)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_no_superseded_documents_remain():
    for old in ('docs/PROTOCOL.md', 'docs/THREAT_MODEL.md', 'docs/STRATEGY_MATH_FA.md', 'docs/AUDIT_RESPONSE_FA.md'):
        assert not (ROOT / old).exists(), f'{old} was replaced; a second copy would contradict the new one'
