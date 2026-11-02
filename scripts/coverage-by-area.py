#!/usr/bin/env python3
"""Line coverage per area, measured with the standard library only (`trace`), no `coverage` package.

    python3 scripts/coverage-by-area.py [--json out.json] [-- pytest args]

Runs pytest in this process under trace.Trace (default args: -q -m "not slow" -p no:cacheprovider;
the slow packaging tests run in subprocesses, which trace cannot see). Executable lines are the line
numbers of the compiled code objects (what `python -m trace --missing` uses). Docstring-only lines and
`if __name__ == '__main__'` blocks count like any other line, so the numbers are slightly conservative.
"""
from __future__ import annotations

import json
import sys
import threading
import trace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AREAS = {
    'Risk (risk, control, validation)': ['avid_core/risk.py', 'avid_core/control.py', 'avid_core/validation.py'],
    'Order State (cycle, orderstate, records)': ['avid_core/cycle.py', 'avid_core/orderstate.py',
                                                 'avid_core/records.py'],
    'Reconciliation': ['avid_core/reconcile.py'],
    'Ledger and files (store, fsio, events)': ['avid_core/store.py', 'avid_core/fsio.py', 'avid_core/events.py'],
    'Strategy (rebalance, performance)': ['avid_core/rebalance.py', 'avid_core/performance.py'],
    'Exchange Client (client, ed25519)': ['avid_core/client.py', 'avid_core/ed25519.py'],
    'Agent (engine, outbox, config, service, __main__)': ['agent/avid_agent/engine.py', 'agent/avid_agent/outbox.py',
                                                          'agent/avid_agent/config.py', 'agent/avid_agent/service.py',
                                                          'agent/avid_agent/__main__.py'],
    'Cloud Protocol (cloud, directive)': ['agent/avid_agent/cloud.py', 'agent/avid_agent/directive.py'],
    'CLI and simulator (cli, sim, models)': ['avid_core/cli.py', 'avid_core/sim.py', 'avid_core/models.py'],
}


def main(argv):
    out_json = None
    if '--json' in argv:
        i = argv.index('--json')
        out_json = argv[i + 1]
        del argv[i:i + 2]
    pytest_args = argv[argv.index('--') + 1:] if '--' in argv else ['-q', '-m', 'not slow', '-p', 'no:cacheprovider']
    sys.path[:0] = [str(ROOT / 'agent'), str(ROOT), str(ROOT / 'tests')]
    import pytest
    tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])
    threading.settrace(tracer.globaltrace)
    rc = tracer.runfunc(pytest.main, [str(ROOT / 'tests'), str(ROOT / 'security'), *pytest_args])
    threading.settrace(None)
    counts = tracer.results().counts
    hit: dict[str, set] = {}
    for (fname, line), n in counts.items():
        if n:
            hit.setdefault(str(Path(fname).resolve()), set()).add(line)
    report = {'pytest_exit_code': int(rc), 'pytest_args': pytest_args, 'areas': {}, 'files': {}}
    for area, files in AREAS.items():
        a_exec = a_hit = 0
        for rel in files:
            path = ROOT / rel
            executable = set(trace._find_executable_linenos(str(path)))   # noqa: SLF001 (stdlib helper)
            covered = executable & hit.get(str(path.resolve()), set())
            report['files'][rel] = {'lines': len(executable), 'covered': len(covered),
                                    'percent': round(100 * len(covered) / max(1, len(executable)), 1)}
            a_exec += len(executable)
            a_hit += len(covered)
        report['areas'][area] = {'lines': a_exec, 'covered': a_hit, 'percent': round(100 * a_hit / max(1, a_exec), 1)}
    tot_e = sum(v['lines'] for v in report['areas'].values())
    tot_h = sum(v['covered'] for v in report['areas'].values())
    report['total'] = {'lines': tot_e, 'covered': tot_h, 'percent': round(100 * tot_h / max(1, tot_e), 1)}
    print(f'\npytest exit code {rc}\n')
    print(f'{"area":<52} {"lines":>6} {"covered":>8} {"%":>6}')
    for area, v in report['areas'].items():
        print(f'{area:<52} {v["lines"]:>6} {v["covered"]:>8} {v["percent"]:>6}')
    print(f'{"TOTAL":<52} {tot_e:>6} {tot_h:>8} {report["total"]["percent"]:>6}')
    if out_json:
        Path(out_json).write_text(json.dumps(report, indent=2))
    return int(rc)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
