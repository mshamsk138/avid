"""Secret files are checked and read through ONE descriptor (PRA-06, PRA-15), state.json vanishing
mid-read is a recoverable 'missing' state (PRA-07), and every logged event carries a severity (PRA-08)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from avid_core import client as nclient
from avid_core import events, fsio, sim
from avid_core.store import Store


def _secret(tmp_path, name='api_key', text='k' * 32, mode=0o600):
    p = tmp_path / name
    p.write_text(text)
    os.chmod(p, mode)
    return p


@pytest.mark.parametrize('mode', [0o600, 0o400])
def test_private_files_are_read(tmp_path, mode):
    assert fsio.read_private(_secret(tmp_path, mode=mode), 'api_key') == 'k' * 32


@pytest.mark.parametrize('mode, shared_ok', [(0o640, False), (0o644, False), (0o620, False), (0o602, False),
                                             (0o666, True), (0o660, True)])
def test_loose_permissions_are_refused(tmp_path, mode, shared_ok):
    p = _secret(tmp_path, mode=mode)
    with pytest.raises(fsio.InsecureFile):
        fsio.read_private(p, 'api_key', allow_shared_read=shared_ok)


def test_shared_read_relaxes_only_the_read_bits(tmp_path):
    assert fsio.read_private(_secret(tmp_path, mode=0o644), 'token', allow_shared_read=True)
    with pytest.raises(fsio.InsecureFile, match='writable'):
        fsio.read_private(_secret(tmp_path, 'token2', mode=0o646), 'token', allow_shared_read=True)


def test_symlinks_directories_and_huge_files_are_refused(tmp_path):
    real = _secret(tmp_path, 'real')
    link = tmp_path / 'link'
    link.symlink_to(real)
    with pytest.raises(fsio.InsecureFile, match='symlink'):
        fsio.read_private(link, 'secret')
    d = tmp_path / 'dir'
    d.mkdir(mode=0o700)
    with pytest.raises((fsio.InsecureFile, IsADirectoryError, OSError)):
        fsio.read_private(d, 'secret')
    with pytest.raises(fsio.InsecureFile, match='large'):
        fsio.read_private(_secret(tmp_path, 'big', text='x' * 70_000), 'secret')


def test_check_and_read_use_one_descriptor(tmp_path, monkeypatch):
    """PRA-06: swapping the path for a symlink to another file AFTER the check cannot change what is
    read - there is no second open by name."""
    good = _secret(tmp_path, 'api_key', text='the-real-key')
    evil = _secret(tmp_path, 'evil', text='attacker', mode=0o644)
    real_fstat = os.fstat

    def swap_then_fstat(fd):
        st = real_fstat(fd)
        os.replace(good, tmp_path / 'moved')
        (tmp_path / 'api_key').symlink_to(evil)
        return st
    monkeypatch.setattr(os, 'fstat', swap_then_fstat)
    assert fsio.read_private(tmp_path / 'api_key', 'api_key') == 'the-real-key'


def test_client_secret_reader_honours_the_container_override(tmp_path, monkeypatch):
    p = _secret(tmp_path, 'token', text='t' * 40, mode=0o444)
    with pytest.raises(nclient.NobitexError):
        nclient._read_secret_file(p, 'token')
    monkeypatch.setenv('NOBITEX_ALLOW_LOOSE_TOKEN_PERMS', '1')
    assert nclient._read_secret_file(p, 'token') == 't' * 40
    os.chmod(p, 0o666)
    with pytest.raises(nclient.NobitexError):                 # writable by others: never, override or not
        nclient._read_secret_file(p, 'token')


def test_state_file_vanishing_mid_read_is_a_missing_state(tmp_path, monkeypatch):
    """PRA-07: 0.1.0a1 checked exists() and then read; a file deleted in between raised. Now the read
    itself decides: missing + a ledger = 'state_missing' (recovered from the ledger, day locked)."""
    s = Store(tmp_path)
    s.save(Store.fresh())
    real_read = Path.read_text

    def vanish(self, *a, **k):
        if self == s.state_path:
            raise FileNotFoundError(str(self))
        return real_read(self, *a, **k)
    monkeypatch.setattr(Path, 'read_text', vanish)
    st = s.load()
    assert st['bot_orders'] == {} and not st.get('state_missing')     # no ledger yet: a first run
    s.ledger_append({'t': 'submit', 'cid': 'c' * 32, 'day': '2026-01-05', 'asset': 'btc', 'side': 'buy',
                     'amount': 0.0003, 'price': 60000.0, 'notional': 18.0, 'at': 1.0})
    st = s.load()
    assert st.get('state_missing') is True


def test_every_logged_event_has_a_severity(tmp_path):
    sim.run(sim.load_example('btc-usdt'), root=tmp_path, steps=40, keep=True)
    lines = (tmp_path / 'data' / 'bot' / 'log.jsonl').read_text().splitlines()
    recs = [json.loads(x) for x in lines if x.strip()]
    assert recs
    for r in recs:
        assert r.get('level') in ('info', 'warning', 'error'), r
    assert events.level_of('emergency_cancel') == 'error'
    assert events.level_of('order_placed') == 'info'
    assert events.level_of('order_placed', 'warning') == 'warning'
