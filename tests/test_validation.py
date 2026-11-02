"""Central numeric validation and strict JSON (AVID-02, Invariant C)."""
import json
import math

import pytest

from avid_core.validation import ValidationError, caps, loads_strict, number, sanitize

BAD = [float('nan'), float('inf'), -float('inf'), True, False, '20', None, [], {}, -1, 0, 10 ** 20]


@pytest.mark.parametrize('v', BAD)
def test_money_caps_refuse_bad_values(v):
    for name in ('max_order_usdt', 'max_daily_usdt'):
        with pytest.raises(ValidationError):
            number(name, v)


@pytest.mark.parametrize('v', [6.7, 0.5, float('nan'), '6', True, 0, -2, 1001])
def test_order_count_must_be_a_positive_whole_number(v):
    with pytest.raises(ValidationError):
        number('max_orders_per_day', v)


def test_good_values_pass_and_counts_become_int():
    assert number('max_orders_per_day', 6.0) == 6 and isinstance(number('max_orders_per_day', 6.0), int)
    assert number('max_order_usdt', 20) == 20.0
    assert number('min_cash_usdt', 0) == 0.0                       # inclusive lower bound
    assert number('band_rel', 0.05) == 0.05 and number('band_rel', 0.9) == 0.9
    with pytest.raises(ValidationError):
        number('band_rel', 0.95)
    with pytest.raises(ValidationError):
        number('max_spread', 0.06)


def test_caps_cross_check():
    assert caps({'max_order_usdt': 20, 'max_orders_per_day': 6, 'max_daily_usdt': 100})['max_orders_per_day'] == 6
    with pytest.raises(ValidationError, match='daily'):
        caps({'max_order_usdt': 20, 'max_orders_per_day': 6, 'max_daily_usdt': 15})
    with pytest.raises(ValidationError, match='missing'):
        caps({'max_order_usdt': 20})


@pytest.mark.parametrize('text', ['{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e309}',
                                  '{"a": 1, "a": 2}', '[' * 100 + ']' * 100, '{"a": ' * 3000 + '1' + '}' * 3000,
                                  'not json', '{"a": 1'])
def test_strict_json_refuses(text):
    with pytest.raises(ValidationError):
        loads_strict(text)


def test_strict_json_accepts_normal_documents():
    assert loads_strict('{"a": [1, 2.5, "x"], "b": {"c": null}}') == {'a': [1, 2.5, 'x'], 'b': {'c': None}}
    assert loads_strict(b'{"a": 1}') == {'a': 1}


def test_sanitize_replaces_non_finite_floats():
    out = sanitize({'a': float('nan'), 'b': [1.0, float('inf')], 'c': {'d': -float('inf')}, 'e': 'x'})
    assert out == {'a': None, 'b': [1.0, None], 'c': {'d': None}, 'e': 'x'}
    json.dumps(out, allow_nan=False)
    assert math.isfinite(sanitize(2.5))
