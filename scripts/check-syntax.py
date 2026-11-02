#!/usr/bin/env python3
"""Parse every Python file with the grammar of the oldest supported Python (3.10).

    python3 -I scripts/check-syntax.py [ROOT]

ast.parse(feature_version=(3, 10)) rejects syntax that 3.10 cannot read (match is fine; e.g. `except*`,
PEP 695 generics, f-string nesting of 3.12 are not). It does NOT detect newer standard-library APIs:
that is what the 3.10 job of the CI test matrix is for.
"""
import ast
import sys
from pathlib import Path

SKIP = {'.git', '__pycache__', '.venv', 'venv', 'build', 'dist', 'node_modules'}


def main(argv):
    root = Path(argv[1] if len(argv) > 1 else '.').resolve()
    bad, n = [], 0
    for p in sorted(root.rglob('*.py')):
        if any(part in SKIP for part in p.relative_to(root).parts):
            continue
        n += 1
        try:
            ast.parse(p.read_text(encoding='utf-8'), filename=str(p), feature_version=(3, 10))
        except SyntaxError as e:
            bad.append(f'{p.relative_to(root)}:{e.lineno}: {e.msg}')
    print(f'{n} Python files parsed with the Python 3.10 grammar')
    for b in bad:
        print('SYNTAX', b)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
