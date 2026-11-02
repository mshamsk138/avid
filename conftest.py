"""Test path setup. AVID_SRC=<dir> runs the same tests against another copy of the code (used to show
that security/test_findings.py fails on v1.2 and passes on this version)."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.environ.get('AVID_SRC') or _HERE)
for _p in (os.path.join(SRC, 'agent'), SRC, os.path.join(_HERE, 'tests')):
    while _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)


def pytest_report_header(config):
    import avid_core
    return f'AVID source under test: {os.path.dirname(os.path.dirname(os.path.abspath(avid_core.__file__)))}'


# Test categories (report Part 9). Files keep their history; the category is a marker, so
# `pytest -m fuzz`, `pytest -m "not slow"`, `pytest -m security` select a kind of test.
CATEGORIES = {
    'test_rebalance.py': ('unit',), 'test_validation.py': ('unit',), 'test_ed25519.py': ('unit',),
    'test_client.py': ('unit', 'exchange_client'), 'test_order_state.py': ('state_machine', 'integration'),
    'test_reconciliation.py': ('integration', 'reconciliation'), 'test_control_config_cli.py': ('integration', 'cli'),
    'test_agent.py': ('integration', 'agent'), 'test_cloud.py': ('unit', 'cloud_protocol'),
    'test_outbox.py': ('unit', 'agent'), 'test_performance_and_ops.py': ('failure_injection', 'crash_recovery'),
    'test_fuzz_invariants.py': ('fuzz', 'invariants'), 'test_agent_fuzz.py': ('fuzz', 'agent'),
    'test_properties.py': ('property',), 'test_sim_cli.py': ('cli', 'simulation'),
    'test_service.py': ('installer', 'agent'), 'test_packaging.py': ('packaging',), 'test_docs.py': ('docs',), 'test_fsio_and_events.py': ('unit', 'security'),
    'test_findings.py': ('security', 'regression'), 'test_nobitex_testnet.py': ('testnet',),
}


def pytest_configure(config):
    for m in sorted({m for ms in CATEGORIES.values() for m in ms} | {'slow'}):
        config.addinivalue_line('markers', f'{m}: AVID test category (conftest.CATEGORIES)')


def pytest_collection_modifyitems(config, items):
    import pytest
    for item in items:
        for m in CATEGORIES.get(os.path.basename(str(item.fspath)), ()):
            item.add_marker(getattr(pytest.mark, m))
