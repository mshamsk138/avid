"""Contract tests against the OFFICIAL Nobitex TESTNET (AVID-07). Never run against production.

They establish, on a real server, the behaviour the engine depends on and that the documentation calls
experimental: clientOrderId echo, lookup and cancel by clientOrderId, duplicate clientOrderId refusal,
the lag of the order lists, status words and amounts of open/cancelled orders, and the trades list.

Skipped unless ALL of these are set:
    AVID_CONTRACT_TESTNET=1
    NOBITEX_NETWORK=testnet
    NOBITEX_SECRETS_DIR=<folder with the TESTNET api_key + secret (or token)>

    AVID_CONTRACT_TESTNET=1 NOBITEX_NETWORK=testnet NOBITEX_SECRETS_DIR=~/.nobitex-testnet \\
        python -m pytest -q tests/contract -rs

Orders are placed far below the market (buy at 50 % of the best bid) so they rest and never fill, and
every test cancels what it placed. Record the outcome of each run in docs/audits/TEST_EVIDENCE.md (AVID-07).
"""
from __future__ import annotations

import os
import secrets
import time

import pytest

from avid_core.client import TESTNET_URL, NobitexClient, NobitexError, read_credentials
from avid_core.cycle import FINAL_CLOSED, OPEN_STATES, order_numbers

pytestmark = pytest.mark.skipif(
    not (os.environ.get('AVID_CONTRACT_TESTNET') == '1' and os.environ.get('NOBITEX_NETWORK') == 'testnet'
         and os.environ.get('NOBITEX_SECRETS_DIR')),
    reason='contract tests need AVID_CONTRACT_TESTNET=1, NOBITEX_NETWORK=testnet and NOBITEX_SECRETS_DIR')


@pytest.fixture(scope='module')
def client():
    cred = read_credentials(os.environ['NOBITEX_SECRETS_DIR'])
    c = (NobitexClient(api_key=cred['api_key'], seed=cred['seed']) if cred['mode'] == 'api_key'
         else NobitexClient(cred['token']))
    assert c.base_url == TESTNET_URL, 'refusing to run contract tests outside the testnet'
    return c


def _cid():
    return 'avct' + secrets.token_hex(14)


def _resting_buy(client, cid):
    bid, _ = client.best_bid_ask('BTCUSDT')
    price = round(bid * 0.5, 2)
    amount = round(max(12.5 / price, 1e-6) + 1e-6, 6)
    return client.add_limit_order('buy', amount, price, src='btc', dst='usdt', client_order_id=cid), amount, price


def _wait(pred, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(1.0)
    return None


def test_client_order_id_round_trip(client):
    cid = _cid()
    order, amount, price = _resting_buy(client, cid)
    try:
        assert order.get('id') is not None
        assert order.get('clientOrderId') in (None, cid), 'the exchange echoed another clientOrderId'
        st = client.order_status(client_order_id=cid)
        assert str(st['id']) == str(order['id'])
        assert str(st.get('status', '')).lower() in OPEN_STATES
        nums, why = order_numbers(st, {'amount': amount, 'asset': 'btc', 'matched': 0.0})
        assert nums is not None, why
        listed = _wait(lambda: [o for o in client.open_orders('btc', 'usdt') if o.get('clientOrderId') == cid])
        assert listed, 'order never appeared in the open list (lag > 20 s?)'
    finally:
        client.cancel(client_order_id=cid)
    final = _wait(lambda: (lambda s: s if str(s.get('status', '')).lower() in FINAL_CLOSED else None)(
        client.order_status(client_order_id=cid)))
    assert final, 'cancel by clientOrderId accepted but no final status within 20 s'
    assert FINAL_CLOSED[str(final['status']).lower()] == 'canceled'


def test_duplicate_client_order_id_is_refused_while_open(client):
    cid = _cid()
    _resting_buy(client, cid)
    try:
        with pytest.raises(NobitexError) as ei:
            _resting_buy(client, cid)
        from avid_core.cycle import _definitely_rejected
        assert not _definitely_rejected(ei.value) or 'uplicate' not in str(ei.value), \
            'a duplicate answer must never be treated as a definite rejection'
    finally:
        client.cancel(client_order_id=cid)


def test_unknown_client_order_id_lookup_fails_cleanly(client):
    with pytest.raises(NobitexError):
        client.order_status(client_order_id=_cid())


def test_full_list_contains_recent_closed_order(client):
    cid = _cid()
    _resting_buy(client, cid)
    client.cancel(client_order_id=cid)
    found = _wait(lambda: [o for o in client.list_orders('btc', 'usdt', status='all') if o.get('clientOrderId') == cid])
    assert found, 'a cancelled order did not show in list(all) within 20 s'


def test_trades_page_shape(client):
    p = client.trades_page('btc', 'usdt', page=1)
    assert isinstance(p['trades'], list) and isinstance(p['hasNext'], bool)
    for t in p['trades'][:5]:
        assert {'id', 'orderId', 'amount', 'price'} <= set(t)
