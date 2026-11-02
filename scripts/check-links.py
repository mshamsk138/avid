#!/usr/bin/env python3
"""Check the links inside the repository's Markdown files (offline).

    python3 -I scripts/check-links.py [ROOT]

Checks every relative link ([text](path), [text](path#anchor), <img src="...">) points at a file that
exists, and every #anchor at a heading of that file (GitHub's anchor rules). External http(s) links are
listed but not fetched: CI may run without network, and a dead external link is not a release blocker.
Exit 1 when a relative link or an anchor is broken.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

SKIP = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'build', 'dist'}
LINK = re.compile(r'(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)|!\[[^\]]*\]\(([^)\s]+)\)|<img[^>]+src="([^"]+)"')
HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
FENCE = re.compile(r'^\s*(```|~~~)')


def slug(text: str) -> str:
    """GitHub heading anchor: lower case, drop punctuation except - and _, spaces -> -."""
    text = re.sub(r'`([^`]*)`', lambda m: m.group(1).replace('<', '').replace('>', ''), text)   # code: literal
    text = re.sub(r'<[^>]+>', '', text)                                                        # HTML tags
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    out = []
    for ch in text.strip().lower():
        cat = unicodedata.category(ch)
        if ch in '-_' or cat[0] in 'LN' or cat in ('Mn', 'Mc'):
            out.append(ch)
        elif ch == ' ':
            out.append('-')
    return ''.join(out)


def anchors(path: Path) -> set:
    seen, result, fenced = {}, set(), False
    for line in path.read_text(encoding='utf-8').splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(line)
        if m:
            s = slug(m.group(2))
            n = seen.get(s, 0)
            seen[s] = n + 1
            result.add(s if n == 0 else f'{s}-{n}')
    for m in re.finditer(r'<a\s+(?:name|id)="([^"]+)"', path.read_text(encoding='utf-8')):
        result.add(m.group(1))
    return result


def links(path: Path):
    fenced = False
    for no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = re.sub(r'`[^`]*`', '', line)
        for m in LINK.finditer(line):
            yield no, next(g for g in m.groups() if g)


def main(argv):
    root = Path(argv[1] if len(argv) > 1 else '.').resolve()
    broken, external, checked = [], set(), 0
    files = [p for p in sorted(root.rglob('*.md')) if not any(part in SKIP for part in p.relative_to(root).parts)]
    cache = {}
    for md in files:
        for no, target in links(md):
            checked += 1
            if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I):
                if target.startswith(('http://', 'https://')):
                    external.add(target)
                continue
            path_part, _, frag = target.partition('#')
            dest = md if not path_part else (md.parent / path_part).resolve()
            where = f'{md.relative_to(root)}:{no}'
            if not dest.exists():
                broken.append(f'{where}: {target} -> missing file')
                continue
            if frag and dest.suffix == '.md':
                if dest not in cache:
                    cache[dest] = anchors(dest)
                if frag not in cache[dest]:
                    broken.append(f'{where}: {target} -> no heading #{frag}')
    print(f'{len(files)} Markdown files, {checked} links checked, {len(external)} external links not fetched')
    for b in broken:
        print('BROKEN', b)
    return 1 if broken else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
