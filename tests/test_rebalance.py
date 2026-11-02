"""Planning arithmetic, managed assets and market specs (AVID-03, report 10.5)."""
from decimal import Decimal

import pytest

from avid_core import rebalance as R
from avid_core.rebalance import ValuationIncomplete, exact_order, plan_multi

BOOKS = {'btc': (59990.0, 60010.0), 'paxg': (2399.0, 2401.0)}


def test_three_to_two_asset_switch_sells_the_dropped_asset_and_keeps_nav():
    p = plan_multi(balances={'btc': 0.001, 'paxg': 0.025, 'usdt': 60.0}, books=BOOKS,
                   targets={'btc': 0.5, 'usdt': 0.5}, min_order_usdt=11.5, max_order_usdt=20.0)
    assert p.value_usdt == pytest.approx(180.0, abs=0.5)
    assert p.action == 'sell' and p.asset == 'paxg' and p.weights['paxg'] > 0.3


def test_zero_target_dust_is_valued_but_not_traded():
    p = plan_multi(balances={'btc': 0.001, 'paxg': 0.0001, 'usdt': 60.0}, books=BOOKS,
                   targets={'btc': 0.5, 'usdt': 0.5}, min_order_usdt=11.5, max_order_usdt=20.0)
    assert p.action == 'none' and 'below minimum' in p.reason
    assert p.value_usdt == pytest.approx(60 + 60 + 0.24, abs=0.1)


def test_held_asset_without_price_makes_valuation_incomplete():
    with pytest.raises(ValuationIncomplete) as ei:
        plan_multi(balances={'btc': 0.001, 'paxg': 0.02, 'usdt': 60.0}, books={'btc': BOOKS['btc']},
                   targets={'btc': 0.5, 'usdt': 0.5})
    assert ei.value.assets == ['paxg']
    with pytest.raises(ValuationIncomplete):
        plan_multi(balances={'btc': 0.001, 'usdt': 60.0}, books={'btc': (60000.0, 59000.0), 'paxg': BOOKS['paxg']},
                   targets={'btc': 0.5, 'usdt': 0.5})


def test_unheld_untargeted_asset_needs_no_price():
    p = plan_multi(balances={'btc': 0.001, 'usdt': 60.0}, books={'btc': BOOKS['btc']}, targets={'btc': 0.5, 'usdt': 0.5})
    assert p.value_usdt == pytest.approx(120.0, abs=0.5)


def test_orders_are_sized_on_available_not_total():
    p = plan_multi(balances={'btc': 0.003, 'usdt': 0.0}, available={'btc': 0.0005, 'usdt': 0.0}, books=BOOKS,
                   targets={'btc': 0.5, 'usdt': 0.5}, min_order_usdt=11.5, max_order_usdt=100.0)
    assert p.value_usdt == pytest.approx(180.0, abs=0.5)                # valued on the total holding
    assert p.action == 'sell' and p.amount <= 0.0005 + 1e-12            # sized on what is free


@pytest.mark.parametrize('targets', [{'btc': 0.6, 'usdt': 0.6}, {'doge': 1.0}, {'btc': float('nan'), 'usdt': 1.0},
                                     {'btc': -0.1, 'usdt': 1.1}])
def test_bad_targets_are_refused(targets):
    with pytest.raises(ValueError):
        plan_multi(balances={'usdt': 10.0}, books=BOOKS, targets=targets)


@pytest.mark.parametrize('kw', [dict(bid=0.001, ask=0.002), dict(want_usdt=float('nan')), dict(max_order_usdt=-1),
                                dict(price_tick=0.0), dict(balance_usdt=float('inf'))])
def test_exact_order_degenerate_inputs_give_no_order(kw):
    base = dict(bid=60000.0, ask=60010.0, want_usdt=20.0, max_order_usdt=20.0, balance_asset=1.0, balance_usdt=100.0,
                amount_step=1e-6, price_tick=0.01)
    amount, price, notional = exact_order('buy', **{**base, **kw})
    assert notional == Decimal(0) or (kw.get('bid') and amount == 0)


def test_exact_order_respects_fee_reserve_and_caps():
    amount, price, notional = exact_order('buy', bid=60000.0, ask=60010.0, want_usdt=50.0, max_order_usdt=20.0,
                                          balance_asset=0.0, balance_usdt=15.0, amount_step=1e-6, price_tick=0.01,
                                          fee_reserve=0.01)
    assert notional <= Decimal('15') / Decimal('1.01') and notional <= 20


def test_parse_weights_fills_unnamed_assets_with_zero():
    w = R.parse_weights('btc=0.5,usdt=0.5')
    assert w['paxg'] == 0.0
    with pytest.raises(ValueError):
        R.parse_weights('btc=nan')


def test_market_overrides_are_validated_and_resettable():
    try:
        R.apply_market_overrides({'btc': {'min_notional': 12.0, 'amount_step': 1e-5}, 'as_of': '2026-11-01'})
        assert R.MARKETS['btc']['min_notional'] == 12.0 and R.MARKET_SPECS_META['as_of'] == '2026-11-01'
        for bad in ({'doge': {}}, {'btc': {'amount_step': 3e-6}}, {'btc': {'min_notional': float('nan')}},
                    {'btc': {'fee': 0.1}}):
            with pytest.raises(ValueError):
                R.apply_market_overrides(bad)
    finally:
        R.reset_markets()
    assert R.MARKETS['btc']['min_notional'] == 11.0


def test_manual_order_on_the_market_of_a_removed_asset_blocks_trading(tmp_path):
    from fakes import FakeExchange, make_cfg
    from avid_core.cycle import Store, cycle
    ex = FakeExchange(usdt=100.0, paxg=0.01)
    ex.place_foreign('sell', 'paxg', 0.005, 3000.0)               # the customer trades PAXG by hand
    r = cycle(ex, Store(tmp_path), make_cfg(weights={'btc': 0.5, 'paxg': 0.0, 'usdt': 0.5}), live=True,
              now=1_700_000_000.0)
    assert r['event'] == 'waiting' and 'PAXGUSDT' in r['reason'] and ex.add_attempts == 0
