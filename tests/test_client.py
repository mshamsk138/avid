"""Nobitex client transport and response validation (audit 40, 42, 43), with an injected opener."""
import io
import json
import urllib.error

import pytest

from avid_core import client as C
from avid_core.client import NobitexClient, NobitexError


class Resp:
    def __init__(self, body, status=200, url='https://apiv2.nobitex.ir/x', headers=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status, self.url = status, url
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body[:n] if n and n > 0 else self.body

    def geturl(self):
        return self.url


def opener_for(*responses):
    calls = []
    seq = list(responses)

    def op(req, timeout=None):
        calls.append(req)
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(r, BaseException):
            raise r
        return r
    op.calls = calls
    return op


def mk(*responses, **kw):
    return NobitexClient(token='t' * 10, opener=opener_for(*responses), sleep=lambda s: None, **kw)


def test_network_names(monkeypatch):
    for name, url in (('', C.BASE_URL), ('mainnet', C.BASE_URL), ('TESTNET', C.TESTNET_URL)):
        monkeypatch.setenv('NOBITEX_NETWORK', name)
        c = NobitexClient(token='x')
        assert c.base_url == url and c.network == ('testnet' if url == C.TESTNET_URL else 'mainnet')
    monkeypatch.setenv('NOBITEX_NETWORK', 'prod')
    with pytest.raises(NobitexError):
        NobitexClient(token='x')


def test_only_official_hosts():
    with pytest.raises(ValueError):
        NobitexClient(token='x', base_url='https://evil.example')


@pytest.mark.parametrize('body', [b'{"status": "ok", "x": NaN}', b'{"status": "ok", "x": 1e400}', b'<html>',
                                  b'{"status":"ok","a":1,"a":2}', b'x' * (C.MAX_RESPONSE_BYTES + 5)])
def test_unusable_responses_are_bad_response(body):
    c = mk(Resp(body))
    with pytest.raises(NobitexError) as ei:
        c.balances()
    assert ei.value.code == 'BAD_RESPONSE'


def test_redirect_is_refused():
    c = mk(Resp({'status': 'ok', 'wallets': []}, url='https://other.example/x'))
    with pytest.raises(NobitexError) as ei:
        c.balances()
    assert ei.value.code == 'REDIRECT'
    err = urllib.error.HTTPError('https://apiv2.nobitex.ir/x', 302, 'Found', {}, io.BytesIO(b''))
    with pytest.raises(NobitexError) as ei:
        mk(err).balances()
    assert ei.value.code == 'REDIRECT'


@pytest.mark.parametrize('wallets', [
    [{'currency': 'btc', 'balance': 'NaN', 'blockedBalance': '0'}],
    [{'currency': 'btc', 'balance': '-1', 'blockedBalance': '0'}],
    [{'currency': 'btc', 'balance': '1', 'blockedBalance': '2'}],
    [{'currency': 'btc', 'balance': '1', 'activeBalance': '3'}],
    [{'currency': 'btc'}],
])
def test_inconsistent_balances_are_refused(wallets):
    with pytest.raises(NobitexError) as ei:
        mk(Resp({'status': 'ok', 'wallets': wallets})).balances(currencies=('btc', 'usdt'))
    assert ei.value.code == 'BAD_RESPONSE'


def test_wallets_total_and_available():
    w = mk(Resp({'status': 'ok', 'wallets': [{'currency': 'btc', 'balance': '0.5', 'blockedBalance': '0.2'},
                                              {'currency': 'usdt', 'balance': '10', 'activeBalance': '7'}]}))
    out = w.wallets(currencies=('btc', 'usdt', 'paxg'))
    assert out['btc'] == {'total': 0.5, 'available': pytest.approx(0.3)}
    assert out['usdt'] == {'total': 10.0, 'available': 7.0}
    assert out['paxg'] == {'total': 0.0, 'available': 0.0}


@pytest.mark.parametrize('book', [{'bids': [['100', '1']], 'asks': [['99', '1']]},
                                  {'bids': [['0', '1']], 'asks': [['1', '1']]},
                                  {'bids': [['NaN', '1']], 'asks': [['1', '1']]},
                                  {'bids': [], 'asks': [['1', '1']]}])
def test_bad_order_books_are_refused(book):
    with pytest.raises(NobitexError):
        mk(Resp({'status': 'ok', **book})).best_bid_ask('BTCUSDT')


def test_add_order_refuses_zero_and_bad_client_order_ids():
    c = mk(Resp({'status': 'ok', 'order': {'id': 1}}))
    with pytest.raises(ValueError):
        c.add_limit_order('buy', 0.0000001, 60000, amount_decimals=6)      # rounds to zero: never sent
    with pytest.raises(ValueError):
        c.add_limit_order('buy', float('nan'), 60000)
    with pytest.raises(ValueError):
        c.add_limit_order('buy', 0.001, 60000, client_order_id='bad id with spaces')
    assert not c.opener.calls
    c.add_limit_order('buy', 0.001, 60000, client_order_id='av12340000019f')
    body = json.loads(c.opener.calls[0].data)
    assert body['clientOrderId'] == 'av12340000019f' and body['amount'] == '0.001000'


def test_order_status_and_cancel_need_exactly_one_identifier():
    c = mk(Resp({'status': 'ok', 'order': {'id': 1}}))
    for fn in (c.order_status, c.cancel):
        with pytest.raises(ValueError):
            fn()
        with pytest.raises(ValueError):
            fn(1, client_order_id='x')


def test_backoff_is_clamped_and_reads_are_retried():
    err = urllib.error.HTTPError('https://apiv2.nobitex.ir/x', 429, 'Too Many', {}, io.BytesIO(b'{"backOff": -5}'))
    slept = []
    c = NobitexClient(token='x' * 8, opener=opener_for(err, Resp({'status': 'ok', 'wallets': []})),
                      sleep=slept.append)
    c.balances()
    assert slept and all(0 <= s <= 60 for s in slept)


def test_placement_is_never_retried():
    err = urllib.error.HTTPError('https://apiv2.nobitex.ir/x', 503, 'Down', {}, io.BytesIO(b'{}'))
    c = mk(err)
    with pytest.raises(NobitexError):
        c.add_limit_order('buy', 0.001, 60000)
    assert len(c.opener.calls) == 1


def test_server_time_from_date_header():
    c = mk(Resp({'status': 'ok', 'wallets': []}, headers={'Date': 'Tue, 14 Nov 2023 22:13:20 GMT'}))
    assert c.server_time() is None
    c.balances()
    assert abs(c.server_time() - 1_700_000_000) < 5


def test_repr_hides_credentials():
    assert 'tttt' not in repr(mk(Resp({})))


def test_deadline_stops_sending_and_says_not_sent():
    now = [100.0]
    c = NobitexClient(token='x' * 8, opener=opener_for(Resp({'status': 'ok', 'wallets': []})), sleep=lambda s: None,
                      monotonic=lambda: now[0])
    c.deadline = 150.0
    c.balances()
    assert len(c.opener.calls) == 1
    now[0] = 151.0
    with pytest.raises(NobitexError) as ei:
        c.add_limit_order('buy', 0.001, 60000)
    assert ei.value.code == 'NOT_SENT' and len(c.opener.calls) == 1     # nothing left the machine
    from avid_core.cycle import _definitely_rejected
    assert _definitely_rejected(ei.value)                               # the engine treats it as not placed


def test_no_retry_sleep_past_the_deadline():
    now = [0.0]
    err = urllib.error.HTTPError('https://apiv2.nobitex.ir/x', 503, 'Down', {}, io.BytesIO(b'{}'))
    slept = []
    c = NobitexClient(token='x' * 8, opener=opener_for(err), sleep=slept.append, monotonic=lambda: now[0])
    c.deadline = 1.0
    with pytest.raises(NobitexError):
        c.balances()
    assert not slept and len(c.opener.calls) == 1


def test_signer_and_market_specs():
    assert NobitexClient(token='x' * 8).signer == 'none'
    seed = bytes(range(32))
    c = NobitexClient(api_key='k' * 10, seed=seed)
    from avid_core import ed25519
    assert c.signer == ed25519.backend()
    specs = c.market_specs()
    assert specs['markets']['btc']['symbol'] == 'BTCUSDT' and 'source' in specs['meta']
